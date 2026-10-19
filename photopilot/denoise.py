"""AI 降噪（NAFNet SIDD）推理模块。

模型：NAFNet（"Simple Baselines for Image Restoration"，ECCV 2022，
Megvii Research），SIDD 真实噪声去噪权重（width32），ONNX 格式。

来源与许可：
- 架构与权重：github.com/megvii-research/NAFNet，MIT（代码 + 权重），
  训练管线部分移植自 BasicSR（Apache-2.0）。
- 权重镜像：huggingface.co/nyanko7/nafnet-models（NAFNet-SIDD-width32.pth，
  官方权重原样托管），由 scripts/convert_nafnet.py 一次性转 ONNX。
- SIDD 训练集（Smartphone Image Denoising Dataset）本身 MIT。

设计原则（与 aesthetic.py 一致）：
- 懒加载 + 线程本地 onnxruntime 会话（并行 session 更快）。
- 模型缺失时 available() 返回 False，上层优雅降级，不抛异常。
- 大图 tile 分块推理（重叠 + 加权融合），避免 U-Net 全图 OOM，
  也避免分块接缝。
- 默认关闭、手动触发：AI 降噪是可选增强，不在批量导出默认流程中。

性能（M1/CPU 实测参考）：width32 单张 1080p 约数秒；分辨率越高越慢。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import numpy as np


def _model_path() -> Path:
    import os
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "models" / "nafnet_sidd_width32.onnx"
    return Path(__file__).resolve().parent.parent / "models" / "nafnet_sidd_width32.onnx"


MODEL_PATH = _model_path()

# NAFNet 输出尺寸需对齐 8 的倍数（3 次下采样，padder_size=8）
_PADDER = 8

_tls = threading.local()
_available: bool | None = None
MODEL_LICENSE_VERIFIED = True


def available() -> bool:
    """模型文件是否存在且 onnxruntime 可导入（结果缓存）。"""
    global _available
    if not MODEL_LICENSE_VERIFIED:
        return False
    if _available is None:
        try:
            import onnxruntime  # noqa: F401
            _available = MODEL_PATH.is_file()
        except ImportError:
            _available = False
    return _available


def _session():
    sess = getattr(_tls, "sess", None)
    if sess is None:
        import onnxruntime as ort
        sess = ort.InferenceSession(
            str(MODEL_PATH), providers=["CPUExecutionProvider"])
        _tls.sess = sess
    return sess


def _run_tile(tile: np.ndarray) -> np.ndarray:
    """对单个已 pad 到 8 倍数的 RGB tile 推理（float32 [0,1] → [0,1]）。

    输入 tile 为 HWC；模型输入为 NCHW（高宽动态，需 8 的倍数），内部转 CHW。
    """
    x = np.transpose(tile.astype(np.float32), (2, 0, 1))  # HWC → CHW
    x = np.expand_dims(x, 0)                              # NCHW
    out = _session().run(None, {"input": x})[0][0]   # CHW
    out = np.transpose(out, (1, 2, 0))               # HWC
    return np.clip(out, 0.0, 1.0)


def _pad_to(tile: np.ndarray) -> np.ndarray:
    """右侧/下侧 pad 到 8 的倍数（reflect 边界，与 NAFNet check_image_size 一致）。"""
    h, w = tile.shape[:2]
    ph = (_PADDER - h % _PADDER) % _PADDER
    pw = (_PADDER - w % _PADDER) % _PADDER
    if ph or pw:
        return np.pad(tile, ((0, ph), (0, pw), (0, 0)), mode="reflect")
    return tile


def denoise(img: np.ndarray, tile_size: int = 512, overlap: int = 32) -> np.ndarray:
    """对 RGB uint8 图像做 AI 降噪，返回 RGB uint8。

    大图按 tile_size 分块（带 overlap 重叠），逐块推理后用线性加权
    融合，消除分块接缝。模型/运行时缺失时返回 None（调用方降级）。

    参数：
      tile_size: 分块边长（像素），NAFNet 全卷积，任意尺寸可推理；
                 默认 512 兼顾内存与速度。
      overlap:   相邻块重叠像素（用于羽化融合消接缝）。
    """
    if not available():
        return None
    if img.dtype != np.uint8 or img.ndim != 3 or img.shape[-1] != 3:
        raise ValueError("denoise 需要 RGB uint8 图像")
    if (isinstance(tile_size, (bool, np.bool_))
            or not isinstance(tile_size, (int, np.integer)) or tile_size <= 0):
        raise ValueError("tile_size 必须为正整数")
    if (isinstance(overlap, (bool, np.bool_))
            or not isinstance(overlap, (int, np.integer))
            or not 0 <= overlap < tile_size):
        raise ValueError("overlap 必须为满足 0 <= overlap < tile_size 的整数")

    h, w = img.shape[:2]
    f = img.astype(np.float32) / 255.0

    if h <= tile_size and w <= tile_size:
        # 小图直接整图推理
        padded = _pad_to(f)
        out = _run_tile(padded)
        return np.clip(out[:h, :w] * 255.0 + 0.5, 0, 255).astype(np.uint8)

    # 大图分块，重叠加权融合
    out = np.zeros_like(f)
    weight = np.zeros((h, w), np.float32)
    step = tile_size - overlap
    # 重叠区的线性羽化权重（沿 x/y 独立）
    for y0 in range(0, h, step):
        for x0 in range(0, w, step):
            y1 = min(y0 + tile_size, h)
            x1 = min(x0 + tile_size, w)
            tile = f[y0:y1, x0:x1]
            padded = _pad_to(tile)
            res = _run_tile(padded)[: y1 - y0, : x1 - x0]

            # 计算羽化掩码：块边缘向中心线性过渡
            th, tw = y1 - y0, x1 - x0
            wy = np.ones(th, np.float32)
            wx = np.ones(tw, np.float32)
            if overlap > 0:
                # 最后一块可能比 overlap 更窄；羽化长度必须匹配实际尺寸。
                # 权重保持正数，单像素边界及大重叠也不会留下未覆盖像素。
                fy, fx = min(overlap, th), min(overlap, tw)
                ry = np.linspace(0, 1, fy + 2, dtype=np.float32)[1:-1]
                rx = np.linspace(0, 1, fx + 2, dtype=np.float32)[1:-1]
                if y0 > 0:
                    wy[:fy] *= ry
                if y1 < h:
                    wy[-fy:] *= ry[::-1]
                if x0 > 0:
                    wx[:fx] *= rx
                if x1 < w:
                    wx[-fx:] *= rx[::-1]
            wmask = wy[:, None] * wx[None, :]

            out[y0:y1, x0:x1] += res * wmask[..., None]
            weight[y0:y1, x0:x1] += wmask

    out = out / np.maximum(weight[..., None], 1e-6)
    return np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8)


def denoise_strength(img: np.ndarray, strength: float = 1.0,
                     tile_size: int = 512, overlap: int = 32) -> np.ndarray:
    """带强度混合的降噪入口：strength 0 返回原图，1 返回全量降噪。

    用于前端滑杆：AI 降噪强度 0-1，混合原图与降噪结果。
    """
    if strength <= 0:
        return img
    cleaned = denoise(img, tile_size=tile_size, overlap=overlap)
    if cleaned is None:
        return None
    if strength >= 1.0:
        return cleaned
    alpha = float(np.clip(strength, 0.0, 1.0))
    out = img.astype(np.float32) * (1 - alpha) + cleaned.astype(np.float32) * alpha
    return np.clip(out, 0, 255).astype(np.uint8)
