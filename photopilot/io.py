"""图像读写层。

JPEG/PNG/TIFF 走 Pillow（EXIF 方向自动纠正）；RAW 走 rawpy（LibRaw 绑定），
解码时使用相机白平衡，输出 8bit sRGB。
"""
from __future__ import annotations

import threading
import hashlib
import os
from collections import Counter
from io import BytesIO
from pathlib import Path
from typing import Iterator

import cv2
import numpy as np
from PIL import Image, ImageOps

try:
    import rawpy
    HAS_RAWPY = True
except ImportError:  # 仅在需要处理 RAW 时才强制
    HAS_RAWPY = False

RAW_EXTS = {".arw", ".cr2", ".cr3", ".nef", ".nrw", ".dng", ".raf",
            ".orf", ".rw2", ".pef", ".srw", ".x3f"}
IMG_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"} | RAW_EXTS


def unique_paths(paths) -> list[str]:
    """按实际文件路径去重，保留首次出现顺序。"""
    result: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        path = Path(raw).expanduser()
        try:
            identity = os.path.normcase(str(path.resolve()))
        except OSError:
            identity = os.path.normcase(str(path.absolute()))
        if identity in seen:
            continue
        seen.add(identity)
        result.append(str(path))
    return result


def output_suffix(path: str | Path, *, preserve_raster: bool = False) -> str:
    """返回 Pillow 可写的处理后缀；RAW 与未知格式使用 JPEG。"""
    suffix = Path(path).suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return ".jpg"
    if suffix == ".png":
        return ".png"
    if preserve_raster and suffix in {".tif", ".tiff", ".bmp", ".webp"}:
        return suffix
    return ".jpg"


def collision_safe_output_names(paths, *, preserve_raster: bool = False,
                                marker: str = "_pp") -> dict[str, str]:
    """为同名源图生成稳定且互不覆盖的输出文件名。"""
    source_paths = [Path(p).expanduser() for p in paths]
    candidates = {
        str(path): f"{path.stem}{marker}{output_suffix(path, preserve_raster=preserve_raster)}"
        for path in source_paths
    }
    counts = Counter(name.casefold() for name in candidates.values())
    result: dict[str, str] = {}
    used: set[str] = set()
    for path in source_paths:
        key = str(path)
        name = candidates[key]
        if counts[name.casefold()] > 1:
            try:
                identity = str(path.resolve())
            except OSError:
                identity = str(path.absolute())
            token = hashlib.sha1(os.path.normcase(identity).encode("utf-8")).hexdigest()
            suffix = output_suffix(path, preserve_raster=preserve_raster)
            name = f"{path.stem}_{token[:10]}{marker}{suffix}"
            width = 10
            while name.casefold() in used and width < len(token):
                width += 2
                name = f"{path.stem}_{token[:width]}{marker}{suffix}"
        result[key] = name
        used.add(name.casefold())
    return result

# ≤该尺寸的加载（打分/缩略图）用 JPEG DCT 降采样解码，比全解码再缩小快数倍；
# 更大尺寸（成品导出）仍全解码保画质
DRAFT_MAX = 1600

# LibRaw 的 dcraw_process 在同一进程内并发调用并不稳定：多张 RAW 同时
# 解码时可能长时间占住线程池，表现为扫描一直停在 0/N。只锁住 RAW
# 解码阶段，后续的指标计算和缩略图生成仍可由扫描线程并行执行。
RAW_DECODE_LOCK = threading.Lock()


def is_image(path: str | Path) -> bool:
    return Path(path).suffix.lower() in IMG_EXTS


def is_raw(path: str | Path) -> bool:
    return Path(path).suffix.lower() in RAW_EXTS


def resize_max(img: np.ndarray, max_size: int) -> np.ndarray:
    """长边超过 max_size 时按比例缩小（INTER_AREA，适合缩小）。"""
    h, w = img.shape[:2]
    m = max(h, w)
    if m <= max_size:
        return img
    s = max_size / m
    return cv2.resize(img, (max(1, int(round(w * s))), max(1, int(round(h * s)))),
                      interpolation=cv2.INTER_AREA)


def load_image(path: str | Path, max_size: int | None = 2048,
               *, raw_preview: bool = False) -> np.ndarray:
    """读取为 RGB uint8 ndarray；max_size 限制长边（None 表示不缩放）。

    ``raw_preview`` 仅用于快速筛选：优先取 RAW 文件内嵌的 JPEG 预览，
    避免为每张照片做完整 LibRaw 显影；预览不可用时自动回退到完整显影。
    """
    path = Path(path)
    ext = path.suffix.lower()
    if ext in RAW_EXTS:
        if not HAS_RAWPY:
            raise RuntimeError(f"读取 RAW 需要 rawpy：pip install rawpy（文件：{path}）")
        with RAW_DECODE_LOCK:
            with rawpy.imread(str(path)) as raw:
                if raw_preview:
                    try:
                        thumb = raw.extract_thumb()
                        if thumb.format == rawpy.ThumbFormat.JPEG:
                            with Image.open(BytesIO(thumb.data)) as im:
                                im = ImageOps.exif_transpose(im)
                                rgb = np.asarray(im.convert("RGB"), dtype=np.uint8)
                        else:
                            raise ValueError("RAW 没有 JPEG 内置预览")
                    except Exception:
                        # 少数相机不嵌入 JPEG，仍保证扫描可用。
                        rgb = raw.postprocess(use_camera_wb=True, half_size=True,
                                              no_auto_bright=False, output_bps=8)
                else:
                    rgb = raw.postprocess(use_camera_wb=True, half_size=True,
                                          no_auto_bright=False, output_bps=8)
    else:
        with Image.open(path) as im:
            if (max_size is not None and max_size <= DRAFT_MAX
                    and getattr(im, "format", "") in {"JPEG", "MPO"}):
                im.draft("RGB", (max_size, max_size))  # DCT 域降采样解码
            im = ImageOps.exif_transpose(im)
            rgb = np.asarray(im.convert("RGB"), dtype=np.uint8)
    if max_size is not None:
        rgb = resize_max(rgb, max_size)
    return rgb


def save_image(img: np.ndarray, path: str | Path, quality: int = 95) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pil = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))
    if path.suffix.lower() in {".jpg", ".jpeg"}:
        pil.save(path, quality=quality, subsampling=1)
    else:
        pil.save(path)
    return path


def iter_images(folder: str | Path, recursive: bool = False) -> Iterator[Path]:
    """遍历目录下的受支持图片（默认不递归，跳过隐藏文件）。"""
    folder = Path(folder)
    if folder.is_file():
        yield folder
        return
    pattern = "**/*" if recursive else "*"
    for p in sorted(folder.glob(pattern)):
        if p.is_file() and not p.name.startswith(".") and is_image(p):
            yield p
