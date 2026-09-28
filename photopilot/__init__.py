"""PhotoPilot —— 照片筛选 / 追色 / 美化 流水线（纯本地运行）。

技术来源：
- 筛选引擎：参考 Facet（ncoevoet/facet）的多维度打分思路，改为轻量传统视觉实现
- RAW 解码：rawpy(LibRaw)，XMP sidecar 兼容 Lightroom/darktable（参考 QuickRawPicker）
- 追色：自研实现 color-matcher(hahnec) / colortrans(dstein64) 中的
  Reinhard、直方图匹配、最优传输线性映射(MKL)，并扩展 OKLab 与肤色保护
- 美化：引导滤波(He et al.) 频率分离磨皮 + 自动白平衡 + 局部清晰度
"""

__version__ = "0.2.1"

from .io import load_image, save_image, resize_max, iter_images, RAW_EXTS, IMG_EXTS
from .cull import cull_folder, CullReport, PhotoScore, smart_rank
from .color import (ColorReference, match_color, PRESETS, reference_from_image,
                    reference_from_preset, reference_from_stats)
from .polish import polish_image, PolishParams, auto_tone, repair_face, repair_blemishes
from .face_analysis import (analyze_faces, FaceInfo, scale_faces,
                            face_region_mask, eye_region_mask)
from .pipeline import run_pipeline
from . import pipeline

__all__ = [
    "load_image", "save_image", "resize_max", "iter_images", "RAW_EXTS", "IMG_EXTS",
    "cull_folder", "CullReport", "PhotoScore", "smart_rank",
    "ColorReference", "match_color", "PRESETS",
    "reference_from_image", "reference_from_preset", "reference_from_stats",
    "polish_image", "PolishParams", "auto_tone", "repair_face", "repair_blemishes",
    "run_pipeline", "pipeline", "analyze_faces", "FaceInfo", "scale_faces",
    "face_region_mask", "eye_region_mask", "__version__",
]
