"""XMP sidecar 写入：评分结果与 Lightroom / darktable / Bridge 互通。

写 <原图名>.xmp（RAW 与 JPEG 同名规则），含 xmp:Rating(0-5) 与 xmp:Label。
这些软件导入时若已有同目录 sidecar 会自动读取评分。
"""
from __future__ import annotations

from pathlib import Path
import os
import tempfile
import threading
import xml.etree.ElementTree as ET

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
XMP_NS = "http://ns.adobe.com/xap/1.0/"
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
PP_NS = "http://photopilot.local/ns/1.0/"
for _prefix, _uri in (("x", "adobe:ns:meta/"), ("rdf", RDF_NS),
                      ("xmp", XMP_NS), ("photoPilot", PP_NS)):
    ET.register_namespace(_prefix, _uri)
_WRITE_LOCK = threading.RLock()


def _can_preserve_sidecar_mode() -> bool:
    """Only POSIX supports fchmod; Windows keeps its ACL-derived file mode."""
    return os.name == 'posix' and hasattr(os, 'fchmod')


def _property(root: ET.Element, ns: str, name: str) -> str | None:
    tag = f"{{{ns}}}{name}"
    for node in root.iter():
        if tag in node.attrib:
            return node.attrib[tag]
        if node.tag == tag:
            return node.text
    return None


def _rating(root: ET.Element) -> int | None:
    value = _property(root, XMP_NS, "Rating")
    try:
        rating = int(value)
        return rating if 0 <= rating <= 5 else None
    except (TypeError, ValueError):
        return None


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


def read_rating(image_path: str | Path, *, manual_only: bool = False) -> int | None:
    """读取同名 XMP 星级；不存在或内容损坏时返回 ``None``。

    manual_only 用于 UI 收藏筛选，排除明确标记为 PhotoPilot 自动映射的
    星级；没有此标记的外部软件 XMP 按人工评级处理。
    """
    xmp_path = Path(image_path).with_suffix(".xmp")
    try:
        root = ET.parse(xmp_path).getroot()
        if manual_only and _property(root, PP_NS, "manualRating") == "false":
            return None
        return _rating(root)
    except (OSError, UnicodeError, ET.ParseError):
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
    # Merge rather than replace: a sidecar may contain an entire Lightroom edit.
    with _WRITE_LOCK:
        if xmp_path.exists():
            try:
                parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
                root = ET.parse(xmp_path, parser=parser).getroot()
            except (ET.ParseError, UnicodeError) as exc:
                raise ValueError("现有 XMP 无法解析，已保留原文件；请先修复或备份它") from exc
        else:
            root = ET.Element("{adobe:ns:meta/}xmpmeta")
            rdf = ET.SubElement(root, f"{{{RDF_NS}}}RDF")
            ET.SubElement(rdf, f"{{{RDF_NS}}}Description", {f"{{{RDF_NS}}}about": ""})
        desc = root.find(f".//{{{RDF_NS}}}Description")
        if desc is None:
            raise ValueError("现有 XMP 缺少 RDF Description，已保留原文件")
        old_rating = _rating(root)
        manual = rating is not None or (old_rating is not None
                    and _property(root, PP_NS, "manualRating") != "false")
        r = (max(0, min(5, int(rating))) if rating is not None else
             old_rating if manual else rating_for(score))
        if label is None:
            label = (_property(root, XMP_NS, "Label") if rating is None and manual else None)
            label = label or (LABEL_KEEP if r >= 4 else LABEL_REVIEW if r >= 2 else LABEL_REJECT)
        values = {
            (XMP_NS, "Rating"): str(r), (XMP_NS, "Label"): label,
            (PP_NS, "score"): f"{score:.4f}",
            (PP_NS, "sharpness"): f"{sharpness:.4f}",
            (PP_NS, "exposure"): f"{exposure:.4f}",
            (PP_NS, "faces"): str(faces), (PP_NS, "flags"): ";".join(flags),
            (PP_NS, "software"): f"PhotoPilot {__version__}",
            (PP_NS, "manualRating"): "true" if manual else "false",
        }
        for (ns, name), value in values.items():
            tag = f"{{{ns}}}{name}"
            for node in root.iter():
                node.attrib.pop(tag, None)
                for child in list(node):
                    if child.tag == tag:
                        node.remove(child)
            ET.SubElement(desc, tag).text = value
        data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=xmp_path.parent, prefix=f".{xmp_path.name}.",
                                             suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                if _can_preserve_sidecar_mode() and xmp_path.exists():
                    os.fchmod(handle.fileno(), xmp_path.stat().st_mode & 0o777)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, xmp_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return xmp_path
