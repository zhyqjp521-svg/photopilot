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
                    rgb = raw.postprocess(use_camera_wb=True,
                                          half_size=max_size is not None and max_size <= 2560,
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


def extract_exif(path: str | Path) -> bytes | None:
    """提取源图的 EXIF 原始字节，供导出时原样透传。

    - RAW 源不适用（rawpy 不透出完整 EXIF），返回 None；
    - ``load_image`` 已按 EXIF 方向转正成品，这里同步把 Orientation 归一为 1，
      避免下游软件（Lightroom 等）对已转正的图再次旋转；
    - 任一环节失败返回 None（宁可丢 EXIF 也不让导出失败）。
    """
    path = Path(path)
    if path.suffix.lower() in RAW_EXTS or not path.is_file():
        return None
    try:
        with Image.open(path) as im:
            raw = im.info.get("exif")
            if not raw:
                return None
            exif = im.getexif()
            if exif.get(0x0112, 1) not in (None, 1):
                exif[0x0112] = 1
                return exif.tobytes()
            return raw
    except Exception:
        return None


def _rational_to_float(value) -> float | None:
    """把 IFDRational / (num, den) 元组转 float，失败返回 None。"""
    try:
        if hasattr(value, "numerator") and hasattr(value, "denominator"):
            return float(value.numerator) / float(value.denominator)
        if isinstance(value, (tuple, list)) and len(value) == 2:
            return float(value[0]) / float(value[1])
    except (TypeError, ZeroDivisionError, ValueError):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _gps_to_decimal(ref: str, coord) -> float | None:
    """GPS 度分秒 → 十进制度；ref 为 S/W 时取负。"""
    try:
        parts = [float(x) if not hasattr(x, "numerator") else float(x)
                 for x in coord]
        d, m, s = parts[0], parts[1], parts[2]
        dec = d + m / 60.0 + s / 3600.0
        if ref in ("S", "W"):
            dec = -dec
        return round(dec, 6)
    except (TypeError, IndexError, ValueError):
        return None


def exif_summary(path: str | Path) -> dict | None:
    """提取拍摄参数，返回前端可读的 dict；无 EXIF / 读取失败返回 None。

    字段（存在才返回，缺失省略）：
      - camera: "Canon EOS R5"（Make + Model 去重拼接）
      - lens: 镜头型号
      - exposure: "1/125 s"（快门）
      - aperture: "f/2.8"
      - iso: 6400
      - focal: "85mm" / "等效 127mm"
      - datetime: "2025-06-01 14:23:05"
      - resolution: "6000 x 4000"
      - gps: (lat, lon) 十进制度
    """
    path = Path(path)
    if path.suffix.lower() in RAW_EXTS or not path.is_file():
        return None
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            if not exif:
                return None
            tags = dict(exif.items())

            def g(tag):
                v = tags.get(tag)
                return v

            make = str(tags.get(0x010F, "")).strip()
            model = str(tags.get(0x0110, "")).strip()
            camera = " ".join(x for x in (make, model) if x and x != " ")
            if make and model and model.startswith(make):
                camera = model  # 避免 "Canon Canon EOS R5"

            lens = str(tags.get(0xA434, "")).strip() or None

            exposure = None
            et = _rational_to_float(g(0x829A))
            if et is not None and et > 0:
                if et >= 1:
                    exposure = f"{et:g} s"
                else:
                    exposure = f"1/{round(1 / et):g} s"

            aperture = None
            fn = _rational_to_float(g(0x829D))
            if fn is not None and fn > 0:
                aperture = f"f/{fn:g}"

            iso = None
            iso_val = tags.get(0x8827)
            if isinstance(iso_val, (tuple, list)) and iso_val:
                iso = int(iso_val[0])
            else:
                iso_int = _rational_to_float(iso_val)
                iso = int(iso_int) if iso_int is not None else None

            focal = None
            focal_equiv = None
            fl = _rational_to_float(g(0x920A))
            fl35 = _rational_to_float(g(0xA405))
            if fl is not None and fl > 0:
                focal = f"{fl:g}mm"
                if fl35 is not None and fl35 > 0 and abs(fl35 - fl) > 0.5:
                    focal_equiv = f"等效 {fl35:g}mm"
            elif fl35 is not None and fl35 > 0:
                focal = f"{fl35:g}mm"

            dt = tags.get(0x9003) or tags.get(0x0132)
            datetime_str = str(dt).strip() if dt else None

            resolution = None
            if im.width and im.height:
                resolution = f"{im.width} x {im.height}"

            gps = None
            try:
                ifd = exif.get_ifd(0x8825)  # GPS IFD
                if ifd:
                    lat = _gps_to_decimal(str(ifd.get(1, "")), ifd.get(2))
                    lon = _gps_to_decimal(str(ifd.get(3, "")), ifd.get(4))
                    if lat is not None and lon is not None:
                        gps = (lat, lon)
            except Exception:
                gps = None

            result: dict = {}
            if camera:
                result["camera"] = camera
            if lens:
                result["lens"] = lens
            if exposure:
                result["exposure"] = exposure
            if aperture:
                result["aperture"] = aperture
            if iso is not None:
                result["iso"] = iso
            if focal:
                result["focal"] = focal
            if focal_equiv:
                result["focal_equiv"] = focal_equiv
            if datetime_str:
                result["datetime"] = datetime_str
            if resolution:
                result["resolution"] = resolution
            if gps:
                result["gps"] = gps
            return result or None
    except Exception:
        return None


def extract_icc(path: str | Path) -> bytes | None:
    """提取源图内嵌的 ICC profile 字节；无 / 读不了返回 None。"""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        with Image.open(path) as im:
            return im.info.get("icc_profile")
    except Exception:
        return None


_SRGB_ICC: bytes | None = None
_SRGB_ICC_LOCK = threading.Lock()


def srgb_icc_profile() -> bytes:
    """标准 sRGB ICC profile（内存缓存）；成品缺源 profile 时兜底嵌入。"""
    global _SRGB_ICC
    with _SRGB_ICC_LOCK:
        if _SRGB_ICC is None:
            from PIL import ImageCms
            _SRGB_ICC = ImageCms.ImageCmsProfile(
                ImageCms.createProfile("sRGB")).tobytes()
        return _SRGB_ICC


def save_image(img: np.ndarray, path: str | Path, quality: int = 95,
               exif: bytes | None = None,
               icc_profile: bytes | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pil = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))
    fmt = path.suffix.lower()
    kwargs: dict = {}
    if icc_profile:
        kwargs["icc_profile"] = icc_profile
    if exif and fmt in {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}:
        kwargs["exif"] = exif
    if fmt in {".jpg", ".jpeg"}:
        pil.save(path, quality=quality, subsampling=1, **kwargs)
    elif fmt in {".tif", ".tiff"}:
        pil.save(path, format="TIFF", compression="tiff_deflate", **kwargs)
    else:
        pil.save(path, **kwargs)
    return path


def iter_images(folder: str | Path, recursive: bool = False) -> Iterator[Path]:
    """遍历目录下的受支持图片（默认不递归，跳过隐藏文件）。"""
    folder = Path(folder)
    if folder.is_file():
        if is_image(folder):
            yield folder
        return
    pattern = "**/*" if recursive else "*"
    for p in sorted(folder.glob(pattern)):
        relative = p.relative_to(folder)
        if (p.is_file() and is_image(p)
                and not any(part.startswith('.') for part in relative.parts)
                and 'photopilot_out' not in relative.parts[:-1]):
            yield p
