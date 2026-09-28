"""色彩空间与共享掩码工具。

实现 sRGB ↔ OKLab 的向量化转换（ Björn Ottosson 2020 标准矩阵），
以及肤色掩码（YCrCb 经典区间 + 可选人脸区域约束），供追色/美化共用。
"""
from __future__ import annotations

import cv2
import numpy as np

# sRGB -> linear
_SRGB_LO = 0.04045
_SRGB_HI = 0.0031308

# uint8 → linear 查找表（正向转换的大头是 power，LUT 一次索引搞定）
_SRGB_LUT = np.where(
    np.arange(256, dtype=np.float32) / 255.0 <= _SRGB_LO,
    np.arange(256, dtype=np.float32) / 255.0 / 12.92,
    ((np.arange(256, dtype=np.float32) / 255.0 + 0.055) / 1.055) ** 2.4,
).astype(np.float32)


def srgb_to_linear(x: np.ndarray) -> np.ndarray:
    return np.where(x <= _SRGB_LO, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0).astype(np.float32, copy=False)
    # cv2.pow 走 SIMD，比 np.power 的 ** (1/2.4) 快约 3 倍
    p = cv2.pow(x, np.float32(1.0 / 2.4))
    out = 1.055 * p - 0.055
    return np.where(x <= _SRGB_HI, x * 12.92, out)


def rgb_to_oklab(img01: np.ndarray) -> np.ndarray:
    """img01: float32 RGB ∈ [0,1]，形状 (H,W,3) → OKLab (L∈[0,1], a,b 有符号)。"""
    r, g, b = (srgb_to_linear(img01[..., i]) for i in range(3))
    return _linear_to_oklab(r, g, b)


def rgb_u8_to_oklab(img_u8: np.ndarray) -> np.ndarray:
    """uint8 RGB → OKLab（LUT 快速路径，批量处理热路径）。"""
    lin = _SRGB_LUT[img_u8]
    return _linear_to_oklab(lin[..., 0], lin[..., 1], lin[..., 2])


def _linear_to_oklab(r, g, b):
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = np.cbrt(l), np.cbrt(m), np.cbrt(s)
    L = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_
    a = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    b2 = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
    return np.stack([L, a, b2], axis=-1)


def oklab_to_linear_rgb(lab: np.ndarray) -> np.ndarray:
    """OKLab → 线性 RGB（未裁剪，可为负/超 1）。"""
    L, a, b = lab[..., 0], lab[..., 1], lab[..., 2]
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    r = +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    b2 = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    return np.stack([r, g, b2], axis=-1)


def oklab_to_rgb(lab: np.ndarray) -> np.ndarray:
    """OKLab → RGB float32 ∈ [0,1]（已裁剪）。"""
    return linear_to_srgb(oklab_to_linear_rgb(lab))


def gamut_compress(lin: np.ndarray) -> np.ndarray:
    """线性 RGB 色域压缩：出界像素沿色度轴向 Rec.709 亮度轴精确收缩（单步）。

    只对出界像素子集计算（in-gamut 像素恒等），强追色时也只花小比例代价。
    """
    lo = lin.min(-1)
    hi = lin.max(-1)
    bad = (lo < -1e-6) | (hi > 1.0 + 1e-6)
    if not bad.any():
        return lin
    out = lin.copy()
    sub = lin[bad]                                    # (K,3)
    luma = sub @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    chroma = sub - luma[:, None]
    c_min = chroma.min(-1)
    c_max = chroma.max(-1)
    with np.errstate(divide="ignore", invalid="ignore"):
        s_neg = np.where(c_min < 0, luma / np.maximum(luma - c_min, 1e-6), 1.0)
        s_pos = np.where(c_max > 0, (1 - luma) / np.maximum(c_max, 1e-6), 1.0)
    s = np.clip(np.minimum(np.minimum(s_neg, s_pos), 1.0), 0.0, 1.0)
    out[bad] = np.clip(luma[:, None] + chroma * s[:, None], 0.0, 1.0)
    return out


def luma(img_rgb: np.ndarray) -> np.ndarray:
    """Rec.601 亮度，float32 ∈ [0,1]。"""
    if img_rgb.dtype != np.float32:
        img = img_rgb.astype(np.float32) / 255.0
    else:
        img = img_rgb
    return 0.299 * img[..., 0] + 0.587 * img[..., 1] + 0.114 * img[..., 2]


def skin_mask(img_rgb: np.ndarray,
              faces: list | None = None,
              feather: int = 21) -> np.ndarray:
    """肤色概率掩码 float32 ∈ [0,1]。

    YCrCb 经典肤色区间（OpenCV 常用阈值），若提供人脸框（xywh 列表），
    则只在人脸框膨胀区域内生效，避免把木地板/沙滩误判为皮肤。
    """
    img = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2YCrCb).astype(np.int16)
    y, cr, cb = img[..., 0], img[..., 1], img[..., 2]
    m = ((cr >= 135) & (cr <= 180) & (cb >= 85) & (cb <= 135) & (y > 40))
    mask = m.astype(np.float32)

    if faces:
        h, w = mask.shape
        region = np.zeros((h, w), np.uint8)
        for (fx, fy, fw, fh) in faces:
            # 人脸框扩展并下移，覆盖脖子/耳朵
            cx, cy = fx + fw / 2, fy + fh / 2
            rx, ry = int(fw * 0.8), int(fh * 1.05)
            cv2.ellipse(region, (int(cx), int(cy + fh * 0.15)), (max(1, rx), max(1, ry)),
                        0, 0, 360, 255, -1)
        mask = mask * (region > 0)

    k = max(3, feather | 1)
    mask = cv2.GaussianBlur(mask, (k, k), 0)
    return np.clip(mask, 0.0, 1.0)
