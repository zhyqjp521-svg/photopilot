"""NIMA 神经网络美学评分（Neural Image Assessment, Talebi & Milanfar 2018）。

模型：MobileNet 骨干 + 10 bins 评分分布头（AVA 数据集训练的美学分），
ONNX 格式 ~12.9MB，CPU 推理约 60-90ms/张（M1 实测）。

来源与许可：
- 方法论出自 Google 论文 NIMA（IEEE CVPR 2018）
- 权重来自 idealo/image-quality-assessment（github.com/idealo/image-quality-assessment），
  明确 Apache-2.0 许可，MobileNet 美学权重（AVA 训练，EMD 0.071 / LCC 0.626 / SRCC 0.609）。
  由本仓库 scripts/convert_nima_apache.py 一次性从 Keras .hdf5 转 ONNX。
- 融合策略参考 Facet（github.com/ncoevoet/facet）与商业筛片软件的实践：
  AI 分与传统指标加权融合，硬伤规则（闭眼/模糊/连拍重复）仍然一票否决

设计：懒加载 + 线程本地会话（onnxruntime 线程安全但并行 session 更快），
模型缺失时返回 None，上层优雅降级为纯传统指标。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import numpy as np

# 定位模型权重：源码运行时在仓库 models/ 目录；PyInstaller 打包后经
# --add-data 收集到 sys._MEIPASS（--onedir 为 dist/PhotoPilot/_internal）。
def _model_path() -> Path:
    import os
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "models" / "nima_mobilenet_aesthetic_apache.onnx"
    return Path(__file__).resolve().parent.parent / "models" / "nima_mobilenet_aesthetic_apache.onnx"

MODEL_PATH = _model_path()

# NIMA 输出 10 个 bins 的评分分布 → 期望分（1-10）
_BINS = np.arange(1, 11, dtype=np.float32)

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


def nima_score(img_work: np.ndarray) -> float | None:
    """对 RGB 图打 NIMA 美学分（1-10）。模型/运行时缺失时返回 None。

    img_work: 任意分辨率的 RGB uint8（内部 resize 到 224）。
    预处理：/127.5-1（MobileNet keras 惯例，等价 /255*2-1，NHWC 布局）。
    输出：dense 层已带 softmax 激活，直接是 10-bin 概率分布，取期望分。
    """
    if not available():
        return None
    import cv2
    x = cv2.resize(img_work, (224, 224), interpolation=cv2.INTER_AREA)
    x = x.astype(np.float32) / 127.5 - 1.0
    p = _session().run(None, {"input": np.expand_dims(x, 0)})[0][0]
    return float((p * _BINS).sum())


def blend_with_traditional(traditional: float, nima: float | None,
                           weight: float = 0.35) -> float:
    """NIMA 1-10 分映射到 0-1 后与传统综合分融合。

    weight: NIMA 权重（0.25-0.4 为商业筛片软件常见区间）。
    nima=None 时原样返回传统分。
    """
    if nima is None:
        return traditional
    ai01 = float(np.clip((nima - 3.5) / 6.0, 0.0, 1.0))   # 3.5→0, 9.5→1
    return (1.0 - weight) * traditional + weight * ai01
