"""XMP sidecar 写入：评分结果与 Lightroom / darktable / Bridge 互通。

写 <原图名>.xmp（RAW 与 JPEG 同名规则），含 xmp:Rating(0-5) 与 xmp:Label。
这些软件导入时若已有同目录 sidecar 会自动读取评分。
"""
from __future__ import annotations

from pathlib import Path
import re

from . import __version__

TEMPLATE = """<?xpacket begin="\\uFEFF" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="PhotoPilot">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:photoPilot="http://photopilot.local/ns/1.0/">
   <xmp:Rating>{rating}</xmp:Rating>
   <xmp:Label>{label}</xmp:Label>
   <photoPilot:score>{score}</photoPilot:score>
   <photoPilot:sharpness>{sharpness}</photoPilot:sharpness>
   <photoPilot:exposure>{exposure}</photoPilot:exposure>
   <photoPilot:faces>{faces}</photoPilot:faces>
   <photoPilot:flags>{flags}</photoPilot:flags>
   <photoPilot:software>PhotoPilot {version}</photoPilot:software>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""

# 星级映射：与主流修图软件的"星标挑片"习惯对齐
LABEL_KEEP, LABEL_REVIEW, LABEL_REJECT = "Keeper", "Review", "Reject"


def rating_for(score: float) -> int:
    if score >= 0.80:
        return 5
    if score >= 0.65:
        return 4
    if score >= 0.50:
        return 3
    if score >= 0.35:
        return 2
    return 1


def read_rating(image_path: str | Path) -> int | None:
    """读取同名 XMP 的手动星级；不存在或内容损坏时返回 ``None``。

    只读取 PhotoPilot/Lightroom 约定的整数 Rating，不会把自动综合分当作
    用户星级，从而允许 UI 在重扫后继续按上次人工筛选结果过滤。
    """
    xmp_path = Path(image_path).with_suffix(".xmp")
    try:
        text = xmp_path.read_text(encoding="utf-8")
        m = re.search(r"<xmp:Rating>\s*([0-5])\s*</xmp:Rating>", text)
        return int(m.group(1)) if m else None
    except (OSError, UnicodeError):
        return None


def label_for(score: float) -> str:
    if score >= 0.65:
        return LABEL_KEEP
    if score >= 0.35:
        return LABEL_REVIEW
    return LABEL_REJECT


def write_sidecar(image_path: str | Path, score: float, flags: list[str],
                  sharpness: float = 0, exposure: float = 0, faces: int = 0,
                  rating: int | None = None, label: str | None = None) -> Path:
    """写 XMP sidecar。

    rating/label 缺省时按 score 自动映射；快审模式的手动星级直接覆盖
    （0 = 清除星级，label 落 Reject/Review 档）。
    """
    src = Path(image_path)
    xmp_path = src.with_suffix(".xmp")
    r = rating_for(score) if rating is None else max(0, min(5, int(rating)))
    if label is None:
        label = label_for(score) if rating is None else (
            LABEL_KEEP if r >= 4 else LABEL_REVIEW if r >= 2 else LABEL_REJECT)
    xml = TEMPLATE.format(
        rating=r, label=label, score=f"{score:.4f}",
        sharpness=f"{sharpness:.4f}", exposure=f"{exposure:.4f}", faces=faces,
        flags=";".join(flags), version=__version__,
    )
    xmp_path.write_text(xml, encoding="utf-8")
    return xmp_path
