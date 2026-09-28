"""人脸分析层：MediaPipe FaceMesh（468/478 关键点）+ EAR 眨眼检测。

这是筛选引擎与美化模块共享的人脸理解层：
- 眨眼检测：眼部 6 点算 Eye Aspect Ratio（Soukupová & Čech 2016），
  双眼 EAR < 0.19 判闭眼——这是商业筛选软件（Aftershoot 等）的核心功能；
- 关键点掩码：脸椭圆 - 眉眼 - 嘴唇 = 精确皮肤区域，磨皮不再误伤五官/头发；
- 优雅降级：未安装 mediapipe 时回退 Haar 框（无眨眼信息，其余功能不变）。

mediapipe 1.x 在部分 macOS 上有 Metal 崩溃问题，本模块固定使用 0.10.x
（纯 CPU、模型内置、零下载）。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

import cv2
import numpy as np

try:
    import mediapipe as mp
    HAS_MEDIAPIPE = True
except ImportError:
    HAS_MEDIAPIPE = False

# ---- MediaPipe FaceMesh 关键点索引（468 点网格） ----
EAR_LEFT = [33, 160, 158, 133, 153, 144]    # p1..p6（左右眼角 + 上下各两点）
EAR_RIGHT = [362, 385, 387, 263, 373, 380]
EYE_RING_L = [33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246]
EYE_RING_R = [362, 382, 381, 380, 374, 373, 390, 249, 263, 466, 388, 387, 386, 385, 384, 398]
BROW_L = [70, 63, 105, 66, 107, 55, 65, 52, 53, 46]
BROW_R = [336, 296, 334, 293, 300, 285, 295, 282, 283, 276]
LIPS_OUTER = [61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 409, 270, 269, 267, 0, 37, 39, 40, 185]
FACE_OVAL = [10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365,
             379, 378, 400, 377, 152, 148, 176, 149, 150, 136, 172, 58, 132, 93,
             234, 127, 162, 21, 54, 103, 67, 109]
IRIS_CENTERS = [468, 473]   # refine_landmarks=True 时的左右虹膜中心

BLINK_EAR = 0.19   # EAR 阈值：低于视为闭眼


@dataclass
class FaceInfo:
    box: tuple[int, int, int, int]          # (x, y, w, h) 像素
    mesh: bool = False                      # 是否拿到 FaceMesh 关键点
    ear: tuple[float, float] = (1.0, 1.0)   # 左右眼 EAR
    blink: bool = False                     # 双眼闭合
    blink_one: bool = False                 # 单眼闭合
    points: np.ndarray | None = None        # (N,2) 像素坐标关键点

    @property
    def boxes(self) -> tuple[int, int, int, int]:
        return self.box


def eye_aspect_ratio(pts: np.ndarray) -> float:
    """pts: 6×2，顺序 [p1外角,p2上1,p3上2,p4内角,p5下1,p6下2]。

    EAR = (|p2-p6| + |p3-p5|) / (2|p1-p4|)，睁眼 ≈0.25-0.35，闭眼 <0.15。
    """
    p = pts.astype(np.float64)
    vert = np.linalg.norm(p[1] - p[5]) + np.linalg.norm(p[2] - p[4])
    horiz = np.linalg.norm(p[0] - p[3])
    if horiz < 1e-6:
        return 1.0
    return float(vert / (2.0 * horiz))


# MediaPipe/OpenCV 的 FaceMesh 虽然可以为每个线程保存独立实例，但在
# macOS 上底层 TFLite/OpenCV TLS 容器仍不能并发进入；并发调用会触发
# “Can't fetch data from terminated TLS container” 原生异常，直接杀掉宿主。
# 保留线程本地实例以避免跨线程共享，同时用进程级锁串行化进入 native 层。
_tls = threading.local()
_FACE_LOCK = threading.RLock()


def _face_mesh():
    if not hasattr(_tls, "mesh"):
        _tls.mesh = (mp.solutions.face_mesh.FaceMesh(
            static_image_mode=True, max_num_faces=10, refine_landmarks=True,
            min_detection_confidence=0.4) if HAS_MEDIAPIPE else None)
    return _tls.mesh


def analyze_faces(img_rgb: np.ndarray) -> list[FaceInfo]:
    """检测人脸并分析眨眼。无 mediapipe 或未检出时回退 Haar 框。

    检测器偶发错误（线程竞态/模型打嗝）降级为"无人脸"而不是抛出——
    一张照片不应因为检测器打嗝就从报告里消失。
    """
    try:
        with _FACE_LOCK:
            return _analyze_faces_impl(img_rgb)
    except Exception as e:
        print(f"[face] 人脸检测降级（{type(e).__name__}: {e}）")
        return []


def _analyze_faces_impl(img_rgb: np.ndarray) -> list[FaceInfo]:
    faces: list[FaceInfo] = []
    fm = _face_mesh()
    if fm is not None:
        res = fm.process(img_rgb)
        h, w = img_rgb.shape[:2]
        if res.multi_face_landmarks:
            for fLM in res.multi_face_landmarks:
                pts = np.array([[lm.x * w, lm.y * h] for lm in fLM.landmark],
                               dtype=np.float32)
                x0, y0 = pts.min(0)
                x1, y1 = pts.max(0)
                bx = (max(0, int(x0)), max(0, int(y0)),
                      int(x1 - x0), int(y1 - y0))
                eL = eye_aspect_ratio(pts[EAR_LEFT])
                eR = eye_aspect_ratio(pts[EAR_RIGHT])
                faces.append(FaceInfo(box=bx, mesh=True, ear=(eL, eR),
                                      blink=(eL < BLINK_EAR and eR < BLINK_EAR),
                                      blink_one=(eL < BLINK_EAR) != (eR < BLINK_EAR),
                                      points=pts))
            return faces

    # 回退：Haar 框（无关键点、无眨眼信息）
    from .cull import detect_faces
    gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)
    for (x, y, fw, fh) in detect_faces(gray):
        faces.append(FaceInfo(box=(int(x), int(y), int(fw), int(fh))))
    return faces


# ------------------------------------------------------------ 掩码构建

def _poly_mask(img: np.ndarray, pts: np.ndarray, indices: list[int],
               expand: float = 1.0) -> np.ndarray:
    """按关键点子集画多边形填充掩码（expand>1 从质心向外扩）。"""
    m = np.zeros(img.shape[:2], np.uint8)
    poly = pts[indices].astype(np.float32)
    if expand != 1.0:
        c = poly.mean(0)
        poly = c + (poly - c) * expand
    cv2.fillPoly(m, [poly.astype(np.int32)], 255)
    return m


def skin_mask_from_faces(img_rgb: np.ndarray, faces: list[FaceInfo],
                         feather: int = 21) -> np.ndarray:
    """关键点级皮肤掩码：脸椭圆，去掉眉/眼/嘴唇/虹膜，再与肤色范围相交。

    相比纯颜色掩码：不会把红唇当皮肤磨掉，也不会漏掉阴影里的脸颊。
    """
    from .colorops import skin_mask as _color_skin_mask
    h, w = img_rgb.shape[:2]
    region = np.zeros((h, w), np.uint8)
    holes = np.zeros((h, w), np.uint8)
    boxed_faces = []
    for f in faces:
        if f.mesh and f.points is not None:
            region |= _poly_mask(img_rgb, f.points, FACE_OVAL, expand=1.06)
            for grp in (EYE_RING_L, EYE_RING_R, BROW_L, BROW_R, LIPS_OUTER):
                holes |= _poly_mask(img_rgb, f.points, grp, expand=1.15)
        boxed_faces.append(f.box)

    if region.any():
        color = (_color_skin_mask(img_rgb, boxed_faces, feather=0) > 0.3).astype(np.uint8) * 255
        # 肤色约束放宽：多边形内、且颜色不像皮肤的部分只保留一半强度（阴影/彩妆）
        mask = (region & ~holes).astype(np.float32) / 255.0
        soft = cv2.bitwise_and(region, color).astype(np.float32) / 255.0
        out = np.clip(soft + 0.45 * (mask - soft), 0, 1)
    else:
        out = _color_skin_mask(img_rgb, boxed_faces)

    k = max(3, feather | 1)
    return np.clip(cv2.GaussianBlur(out.astype(np.float32), (k, k), 0), 0, 1)


def face_region_mask(img_rgb: np.ndarray, faces: list[FaceInfo],
                     feather: int = 21) -> np.ndarray:
    """返回人脸区域软掩码（包含额头和下巴，但不扩到整个人体）。

    这是局部修复的安全边界：有 FaceMesh 时使用脸轮廓，没有关键点时
    使用椭圆框回退。返回值范围 0–1，可直接用于局部混合。
    """
    h, w = img_rgb.shape[:2]
    region = np.zeros((h, w), np.uint8)
    for f in faces:
        if f.mesh and f.points is not None:
            region |= _poly_mask(img_rgb, f.points, FACE_OVAL, expand=1.02)
            continue
        x, y, fw, fh = f.box
        cx, cy = x + fw // 2, y + fh // 2
        cv2.ellipse(region, (cx, cy), (max(1, fw // 2), max(1, fh // 2)),
                    0, 0, 360, 255, -1)
    if not region.any():
        return np.zeros((h, w), np.float32)
    k = max(3, int(feather) | 1)
    return np.clip(cv2.GaussianBlur(region.astype(np.float32) / 255.0,
                                    (k, k), 0), 0, 1)


def scale_faces(faces: list[FaceInfo], scale: float) -> list[FaceInfo]:
    """把 FaceInfo（框+关键点）从工作分辨率映射到另一分辨率。"""
    out = []
    for f in faces:
        x, y, w, h = f.box
        pts = f.points * scale if f.points is not None else None
        out.append(FaceInfo(box=(int(x * scale), int(y * scale),
                                 int(w * scale), int(h * scale)),
                            mesh=f.mesh, ear=f.ear, blink=f.blink,
                            blink_one=f.blink_one, points=pts))
    return out


def eye_regions(faces: list[FaceInfo]) -> list[tuple[int, int, int, int]]:
    """从关键点提取每只眼睛的紧凑外接框（含虹膜），供眼部提亮使用。"""
    boxes = []
    for f in faces:
        if f.mesh and f.points is not None:
            for ring, iris in ((EYE_RING_L, IRIS_CENTERS[0]), (EYE_RING_R, IRIS_CENTERS[1])):
                poly = f.points[ring]
                c = f.points[iris] if len(f.points) > iris else poly.mean(0)
                r = float(np.linalg.norm(poly - c, axis=1).max()) * 1.25
                x0, y0 = int(c[0] - r), int(c[1] - r)
                boxes.append((x0, y0, int(2 * r), int(2 * r)))
        else:
            # 无关键点：无从定位眼睛，交给 Haar（调用方自行决定）
            pass
    return boxes


def eye_region_mask(img_rgb: np.ndarray, faces: list[FaceInfo],
                    feather: int = 9) -> np.ndarray:
    """把关键点眼睛框转成软掩码，用于局部眼部/五官增强。"""
    h, w = img_rgb.shape[:2]
    mask = np.zeros((h, w), np.uint8)
    for x, y, ew, eh in eye_regions(faces):
        cx, cy = x + ew // 2, y + eh // 2
        cv2.ellipse(mask, (cx, cy), (max(1, ew // 2), max(1, eh // 2)),
                    0, 0, 360, 255, -1)
    if not mask.any():
        return np.zeros((h, w), np.float32)
    k = max(3, int(feather) | 1)
    return np.clip(cv2.GaussianBlur(mask.astype(np.float32) / 255.0,
                                    (k, k), 0), 0, 1)
