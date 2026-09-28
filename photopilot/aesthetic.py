"""NIMA 神经网络美学评分（Neural Image Assessment, Talebi & Milanfar 2018）。

模型：MobileNetV2 骨干 + 10 bins 评分分布头（AVA 数据集训练的美学分），
ONNX 格式 12.8MB，CPU 推理约 60-90ms/张（M1 实测）。

来源与许可：
- 方法论出自 Google 论文 NIMA（IEEE CVPR 2018）
- 权重源自 AVA 数据集上训练的 MobileNetV2 美学模型（Apache-2.0 系生态的
  ONNX 转换版），经 HuggingFace `cromsc/nima-mobilenet-aesthetic` 分发
- 融合策略参考 Facet（github.com/ncoevoet/facet）与商业筛片软件的实践：
  AI 分与传统指标加权融合，硬伤规则（闭眼/模糊/连拍重复）仍然一票否决

设计：懒加载 + 线程本地会话（onnxruntime 线程安全但并行 session 更快），
模型缺失时返回 None，上层优雅降级为纯传统指标。
"""
from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

MODEL_PATH = Path(__file__).resolve().parent.parent / "models" / "nima_mobilenet_aesthetic.onnx"

# NIMA 输出 10 个 bins 的评分分布 → 期望分（1-10）
_BINS = np.arange(1, 11, dtype=np.float32)

_tls = threading.local()
_available: bool | None = None


def available() -> bool:
    """模型文件是否存在且 onnxruntime 可导入（结果缓存）。"""
    global _available
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
    预处理：/255*2-1（MobileNetV2 keras 惯例，NHWC 布局）。
    """
    if not available():
        return None
    import cv2
    x = cv2.resize(img_work, (224, 224), interpolation=cv2.INTER_AREA)
    x = (x.astype(np.float32) / 255.0) * 2.0 - 1.0
    out = _session().run(None, {"input": np.expand_dims(x, 0)})[0][0]
    e = np.exp(out - out.max())
    p = e / e.sum()
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
