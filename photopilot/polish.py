"""美化模块：自动白平衡 → 频率分离磨皮 → 局部清晰度 → 眼部提亮。

技术选型：
- 磨皮用引导滤波（He et al. ECCV 2010）而非双边滤波：边缘保持更好、O(N) 复杂度，
  并做「频率分离」——把低频（肤色基调）平滑、高频（毛孔纹理）按比例保留，
  避免"塑料脸"，这是商业修图软件的标准做法。
- 无人脸时退化为全图低强度处理，行为可控（--no-global-skin 可关闭）。
- 深度模型修脸（CodeFormer/GFPGAN 等）留作可选后端，见 README 路线图；
  本模块零额外权重下载、CPU 实时。
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .colorops import luma, skin_mask, srgb_to_linear, linear_to_srgb
from .cull import detect_faces
from .face_analysis import (FaceInfo, skin_mask_from_faces, eye_regions,
                            face_region_mask, eye_region_mask)


@dataclass
class PolishParams:
    wb: float = 0.5        # 自动白平衡强度 0-1
    skin: float = 0.6      # 磨皮强度 0-1
    retain: float = 0.40   # 皮肤高频纹理保留比例（0=塑料脸，1=无效）
    clarity: float = 0.25  # 全局清晰度/微对比 0-1
    eyes: bool = True      # 眼部提亮锐化（检测到人脸时）
    global_skin: bool = True  # 无人脸时是否做全图低强度柔肤
    face_repair: float = 0.0  # 人脸保守修复：去噪并恢复局部细节 0-1
    blemish: float = 0.0     # 肤色局部瑕疵抑制 0-1
    local_region: str = "all"  # all | skin | face | eyes | background
    auto_tone: bool = False  # 智能自动曝光/阴影/高光修正


# ------------------------------------------------------------ 引导滤波

def guided_filter(guide: np.ndarray, src: np.ndarray,
                  radius: int, eps: float) -> np.ndarray:
    """He et al. 引导滤波（boxFilter 实现），输入 float32 2D。"""
    r = max(1, int(radius))
    k = 2 * r + 1

    def box(x):
        return cv2.boxFilter(x, -1, (k, k), normalize=True, borderType=cv2.BORDER_REFLECT)

    mean_i, mean_p = box(guide), box(src)
    corr_ii, corr_ip = box(guide * guide), box(guide * src)
    var_i = corr_ii - mean_i * mean_i
    cov_ip = corr_ip - mean_i * mean_p
    a = cov_ip / (var_i + eps)
    b = mean_p - a * mean_i
    return box(a) * guide + box(b)


# ------------------------------------------------------------ 各处理步骤

# ------------------------------------------------------------ 批量白平衡

def estimate_wb_gains(img: np.ndarray) -> np.ndarray:
    """估计单张的偏色（illuminant）：RGB 三通道均值。

    语义约定：校正增益 = 批次目标均值 / 本张均值，直接乘回图像即可把
    本张的通道均值搬到批次目标（"校到灰"的倒数形式，方向不能反）。
    """
    f = img.astype(np.float32)
    return f.reshape(-1, 3).mean(0)


def batch_target_gains(gains_list: list[np.ndarray]) -> np.ndarray:
    """批次目标 = 各张通道均值的中位数（稳健：个别异色场景不拖偏整批）。"""
    return np.median(np.stack(gains_list), axis=0)


def apply_wb_gains(img: np.ndarray, gains: np.ndarray, strength: float = 0.9) -> np.ndarray:
    """按给定增益校正（gains = 批次目标均值 / 本张均值），strength 混合。"""
    g = np.clip(np.asarray(gains, np.float32), 0.5, 2.0)
    out = img.astype(np.float32) * g
    return _blend(img, out, strength)


def auto_white_balance(img: np.ndarray, strength: float = 0.5) -> np.ndarray:
    """灰度世界假设 WB，增益限制在 [0.85, 1.18] 防止极端偏色。"""
    if strength <= 0:
        return img
    f = img.astype(np.float32)
    means = f.reshape(-1, 3).mean(0)
    gain = means.mean() / (means + 1e-6)
    gain = np.clip(gain, 0.85, 1.18).astype(np.float32)
    out = f * gain
    return _blend(img, out, strength)


def auto_tone(img: np.ndarray, strength: float = 1.0) -> np.ndarray:
    """按稳健亮度百分位自动修正曝光，并温和压回高光。

    计算在近似场景线性的 RGB 中进行：以中位亮度估计曝光 EV，限制在
    ±1.5EV；欠曝时轻抬阴影，过曝/增益后出现的高光则用 shoulder 曲线压缩。
    三通道使用同一个亮度比例，尽量保持色相。完全裁剪的 JPEG 白块没有
    原始数据可恢复，RAW 才能在未同时裁剪的通道中保留部分高光信息。
    """
    if strength <= 0:
        return img.copy()
    if img.dtype != np.uint8 or img.ndim != 3 or img.shape[-1] != 3:
        raise ValueError("auto_tone 需要 RGB uint8 图像")

    f = np.clip(img.astype(np.float32) / 255.0, 0.0, 1.0)
    lin = srgb_to_linear(f)
    lum = (lin @ np.array([0.2126, 0.7152, 0.0722], np.float32)).astype(np.float32)
    # 采样降低大图上的百分位成本，同时保留稳定的全局判断。
    step = max(1, int(max(lum.shape[:2]) / 512))
    sample = lum[::step, ::step]
    p02, p50, p98 = np.percentile(sample, [2, 50, 98]).astype(np.float32)

    # 18% 线性灰是摄影曝光的常用中灰目标；百分位而非均值不容易被
    # 天空/婚纱/黑背景等大面积极端区域牵着走。
    target = 0.18
    ev = float(np.clip(np.log2(target / max(float(p50), 1e-4)), -1.5, 1.5))
    clip_hi = float((sample >= 0.995).mean())
    if clip_hi > 0.01 and ev > 0:
        # 画面已有明显白切时不再整体提亮，避免“暗部正常、天空更白”。
        ev = 0.0

    work = lin * np.float32(2.0 ** ev)
    if ev > 0.05:
        # 比单纯乘增益更克制地抬阴影；高光仍交给后面的 shoulder。
        gamma = 1.0 - 0.14 * min(ev / 1.5, 1.0)
        work = np.power(np.clip(work, 0.0, None), gamma).astype(np.float32)

    # 只压亮部，保持中间调和色相。阈值选在线性空间，避免把正常肤色
    # 当作过曝区域；超过白点的数据只能压缩，不能凭空恢复纹理。
    lum2 = (work @ np.array([0.2126, 0.7152, 0.0722], np.float32)).astype(np.float32)
    hi = lum2 > 0.72
    if np.any(hi) and (p98 > 0.72 or ev < -0.05):
        t = np.maximum((lum2 - 0.72) / 0.28, 0.0)
        a = np.float32(1.8)
        compressed = 0.72 + 0.28 * (1.0 - np.exp(-a * t)) / (1.0 - np.exp(-a))
        compressed = np.minimum(compressed, 1.0)
        ratio = np.where(hi, compressed / np.maximum(lum2, 1e-6), 1.0)
        work *= ratio[..., None]

    enhanced = linear_to_srgb(np.clip(work, 0.0, 1.0))
    out = f * (1.0 - float(strength)) + enhanced * float(strength)
    return np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8)


def smooth_skin(img: np.ndarray, strength: float = 0.6, retain: float = 0.4,
                mask: np.ndarray | None = None) -> np.ndarray:
    """频率分离磨皮：低频基调平滑 + 高频纹理按 retain 保留。

    mask: 肤色概率掩码（0-1），None 时全图处理。
    """
    if strength <= 0:
        return img
    h, w = img.shape[:2]
    min_dim = min(h, w)
    radius = max(6, min_dim // 50)
    f = img.astype(np.float32) / 255.0
    guide = luma(f)[..., None]

    base = np.stack([guided_filter(guide[..., 0], f[..., c], radius, 2.5e-3)
                     for c in range(3)], axis=-1)
    detail = f - base
    out = base + retain * detail          # 高频衰减 → 肤色均匀但纹理还在
    out = np.clip(out, 0, 1) * 255.0

    if mask is not None:
        out = mask[..., None] * out + (1 - mask[..., None]) * img
    return _blend(img, out, strength)


def clarity(img: np.ndarray, amount: float = 0.25) -> np.ndarray:
    """LAB 亮度通道大半径反锐化 = 局部对比度/通透感，色度不动。"""
    if amount <= 0:
        return img
    h, w = img.shape[:2]
    lab = cv2.cvtColor(img, cv2.COLOR_RGB2LAB).astype(np.float32)
    sigma = max(3.0, min(h, w) / 60.0)
    low = cv2.GaussianBlur(lab[..., 0], (0, 0), sigma)
    lab[..., 0] = np.clip(lab[..., 0] + amount * 2.0 * (lab[..., 0] - low), 0, 255)
    return cv2.cvtColor(lab.clip(0, 255).astype(np.uint8), cv2.COLOR_LAB2RGB)


def _masked_mix(src: np.ndarray, dst: np.ndarray, mask: np.ndarray,
                strength: float = 1.0) -> np.ndarray:
    """按软掩码混合两个 RGB 图像，避免局部处理产生硬边。"""
    alpha = np.clip(np.asarray(mask, np.float32) * float(strength), 0, 1)[..., None]
    out = src.astype(np.float32) * (1 - alpha) + dst.astype(np.float32) * alpha
    return np.clip(out, 0, 255).astype(np.uint8)


def repair_blemishes(img: np.ndarray, mask: np.ndarray | None = None,
                     strength: float = 0.5) -> np.ndarray:
    """保守抑制皮肤上的孤立明暗斑，不做整脸塑料化模糊。

    只在局部亮度与邻域中值差异明显时修正，毛孔和整体纹理会保留；
    mask 通常来自 FaceMesh 皮肤区域。
    """
    if strength <= 0:
        return img
    lab = cv2.cvtColor(img, cv2.COLOR_RGB2LAB).astype(np.float32)
    lum = lab[..., 0]
    k = max(3, min(9, (min(img.shape[:2]) // 120) * 2 + 3))
    # OpenCV 的 medianBlur 在当前版本只接受 8-bit 单通道输入；亮度本身
    # 已经位于 [0,255]，转换不会影响瑕疵阈值的语义。
    local = cv2.medianBlur(np.clip(lum, 0, 255).astype(np.uint8), k).astype(np.float32)
    delta = local - lum
    spot = np.clip((np.abs(delta) - 4.0) / 22.0, 0, 1)
    if mask is not None:
        spot *= np.clip(mask, 0, 1)
    lab[..., 0] = np.clip(lum + delta * spot * float(strength), 0, 255)
    fixed = cv2.cvtColor(lab.astype(np.uint8), cv2.COLOR_LAB2RGB)
    return _masked_mix(img, fixed, spot, strength=1.0)


def repair_face(img: np.ndarray, mask: np.ndarray | None = None,
                strength: float = 0.5) -> np.ndarray:
    """局部人脸修复：轻度去噪后保留细节，再恢复自然微对比。

    这是可解释的 CPU 本地增强，不凭空生成五官；适合批量婚礼片，
    对闭眼/严重运动模糊等情况仍应使用多张连拍比较后人工选择。
    """
    if strength <= 0:
        return img
    f = img.astype(np.float32) / 255.0
    lum = luma(f)
    r = max(2, min(9, min(img.shape[:2]) // 90))
    base = guided_filter(lum, lum, r, 1.5e-3)
    detail = lum - base
    # 适度压低噪声并抬一点人脸局部细节，强度始终由用户滑杆控制。
    repaired_l = np.clip(base + detail * (0.82 - 0.22 * float(strength))
                         + detail * 0.16 * float(strength), 0, 1)
    ratio = repaired_l / np.maximum(lum, 1e-3)
    fixed = np.clip(f * ratio[..., None], 0, 1) * 255.0
    fixed = fixed.astype(np.uint8)
    if mask is None:
        mask = np.ones(img.shape[:2], np.float32)
    return _masked_mix(img, fixed, mask, strength=float(strength))


def _local_mask(img: np.ndarray, faces: list, region: str) -> np.ndarray:
    """按 UI 的局部区域选项生成 0–1 掩码。"""
    region = str(region or "all").lower()
    if region in {"all", "global", "全图"}:
        return np.ones(img.shape[:2], np.float32)
    infos = [f for f in faces if isinstance(f, FaceInfo)]
    boxes = [f.box if isinstance(f, FaceInfo) else tuple(f) for f in faces]
    if region == "face":
        return face_region_mask(img, infos) if infos else _boxes_mask(img, boxes)
    if region == "eyes":
        return eye_region_mask(img, infos) if infos else np.zeros(img.shape[:2], np.float32)
    if region == "skin":
        if infos and any(f.mesh for f in infos):
            return skin_mask_from_faces(img, infos, feather=max(9, min(img.shape[:2]) // 40))
        return skin_mask(img, boxes, feather=max(9, min(img.shape[:2]) // 40)) if boxes else np.zeros(img.shape[:2], np.float32)
    if region == "background":
        face = face_region_mask(img, infos) if infos else _boxes_mask(img, boxes)
        return 1.0 - face
    return np.ones(img.shape[:2], np.float32)


def _boxes_mask(img: np.ndarray, boxes: list[tuple[int, int, int, int]]) -> np.ndarray:
    m = np.zeros(img.shape[:2], np.uint8)
    for x, y, w, h in boxes:
        cv2.ellipse(m, (int(x + w / 2), int(y + h / 2)),
                    (max(1, int(w / 2)), max(1, int(h / 2))), 0, 0, 360, 255, -1)
    return cv2.GaussianBlur(m.astype(np.float32) / 255.0, (0, 0), 5) if m.any() else m.astype(np.float32)


_EYE_CASCADE_CACHE = None


def _eye_cascade():
    global _EYE_CASCADE_CACHE
    if _EYE_CASCADE_CACHE is None:
        try:
            c = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_eye.xml")
            _EYE_CASCADE_CACHE = c if not c.empty() else False
        except Exception:
            _EYE_CASCADE_CACHE = False
    return _EYE_CASCADE_CACHE or None


def brighten_eyes(img: np.ndarray, faces: list,
                  eye_boxes: list[tuple[int, int, int, int]] | None = None) -> np.ndarray:
    """眼部提亮 + 锐化。优先用关键点定位的 eye_boxes；否则退回 Haar 眼睛级联。"""
    if not faces:
        return img
    if eye_boxes:
        rois = [(max(0, x), max(0, y), w, h) for (x, y, w, h) in eye_boxes]
    else:
        ec = _eye_cascade()
        if ec is None:
            return img
        gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        h_img, w_img = gray.shape
        rois = []
        for (fx, fy, fw, fh) in faces:
            x0, y0 = max(0, fx), max(0, fy)
            x1, y1 = min(w_img, fx + fw), min(h_img, fy + fh)
            roi = gray[y0:y1, x0:x1]
            if roi.size == 0:
                continue
            eyes = ec.detectMultiScale(roi, scaleFactor=1.1, minNeighbors=4,
                                       minSize=(max(8, fw // 8),) * 2)
            rois.extend((x0 + ex, y0 + ey, ew, eh) for (ex, ey, ew, eh) in eyes[:4])

    out = img.copy()
    for (gx0, gy0, ew, eh) in rois:
        gx1, gy1 = min(out.shape[1], gx0 + ew), min(out.shape[0], gy0 + eh)
        patch = out[gy0:gy1, gx0:gx1].astype(np.float32)
        if patch.size == 0:
            continue
        patch = patch + 6.0                                   # 提亮（保守）
        gray = patch @ np.array([0.299, 0.587, 0.114], np.float32)
        blur = cv2.GaussianBlur(gray, (0, 0), 1.2)
        patch = patch + 0.35 * (gray - blur)[..., None]       # 亮度细节锐化（不偏色相）
        patch = np.clip(patch, 0, 255)
        m = _feather_patch(patch.shape).astype(np.float32)[..., None]
        out[gy0:gy1, gx0:gx1] = (
            out[gy0:gy1, gx0:gx1].astype(np.float32) * (1 - m) + patch * m
        )
    return np.clip(out, 0, 255).astype(np.uint8)


def _feather_patch(shape) -> np.ndarray:
    """椭圆羽化掩码，让眼部调整融入周围皮肤。"""
    h, w = shape[:2]
    m = np.zeros((h, w), np.uint8)
    cv2.ellipse(m, (w // 2, h // 2), (max(1, w // 2 - 1), max(1, h // 2 - 1)),
                0, 0, 360, 255, -1)
    return cv2.GaussianBlur(m, (0, 0), max(1.0, min(h, w) / 6)) / 255.0


def _blend(src: np.ndarray, dst: np.ndarray, strength: float) -> np.ndarray:
    out = src.astype(np.float32) * (1 - strength) + dst * strength
    return np.clip(out, 0, 255).astype(np.uint8)


# ------------------------------------------------------------ 主入口

def polish_image(img: np.ndarray, params: PolishParams | None = None,
                 faces: list[FaceInfo] | list | None = None) -> np.ndarray:
    """美化主流程。faces 允许外部传入 FaceInfo 列表（避免重复检测），缺省自动检测。

    顺序：白平衡 → 清晰度 → 磨皮 → 眼部。
    清晰度放在磨皮之前：先建立整体通透感，磨皮再压掉皮肤上的高频噪声，
    避免锐化把刚磨平的皮肤重新"打毛"。
    有 FaceMesh 关键点时用精确皮肤掩码（避开眉眼唇），否则退回 Haar+肤色范围。
    """
    p = params or PolishParams()
    out = auto_white_balance(img, p.wb)
    if p.auto_tone:
        out = auto_tone(out)

    if faces is None:
        from .face_analysis import analyze_faces
        faces = analyze_faces(out)
    face_infos = [f for f in faces if isinstance(f, FaceInfo)]
    boxes = [f.box if isinstance(f, FaceInfo) else tuple(f) for f in faces]
    local = _local_mask(out, faces, p.local_region)

    if p.clarity > 0:
        out = _masked_mix(out, clarity(out, p.clarity), local)

    skin_area = None
    if boxes:
        if face_infos and any(f.mesh for f in face_infos):
            skin_area = skin_mask_from_faces(out, face_infos,
                                             feather=max(9, min(out.shape[:2]) // 40))
        else:
            skin_area = skin_mask(out, boxes, feather=max(9, min(out.shape[:2]) // 40))

    if p.skin > 0:
        if boxes:
            mask = skin_area if skin_area is not None else local
            if p.local_region not in {"all", "global", "全图"}:
                mask = mask * local
            out = smooth_skin(out, p.skin, p.retain, mask)
        elif p.global_skin:
            out = smooth_skin(out, p.skin * 0.5, max(0.6, p.retain), local)

    if p.blemish > 0 and skin_area is not None:
        out = repair_blemishes(out, skin_area * local, p.blemish)

    if p.face_repair > 0 and boxes:
        face_area = face_region_mask(out, face_infos) if face_infos else _boxes_mask(out, boxes)
        out = repair_face(out, face_area * local, p.face_repair)

    if p.eyes and boxes and p.local_region not in {"background", "背景", "skin", "皮肤"}:
        eboxes = eye_regions(face_infos) if face_infos and any(f.mesh for f in face_infos) else None
        out = brighten_eyes(out, boxes, eye_boxes=eboxes)
    return out
