"""完整流水线：筛选 → 追色 → 美化 → 导出 + 报告。

- 参考来源三选一：--ref 参考图 / --preset 风格预设 / 都不给则 auto
  （auto = 把本批已选照片的平均色彩统计作为参考，整批统一色调——
   即"批量追色"，这对活动/婚礼整组出片最实用）
- 导出尺寸 EXPORT_SIZE 控制处理成本；评分与连拍分组在 WORK_SIZE 完成
- sidecar 写在原图旁（Lightroom/darktable 导入即读），导出图写 out_dir
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace as dataclasses_replace, field
from pathlib import Path

import cv2
import numpy as np

from .io import (load_image, save_image, resize_max,
                 collision_safe_output_names)
from .cull import cull_folder, CullReport, PhotoScore, WORK_SIZE
from .face_analysis import analyze_faces, scale_faces
from .color import (ColorReference, match_color, reference_from_image,
                    reference_from_preset, reference_from_stats, oklab_stats)
from .polish import polish_image, PolishParams
from .xmp import write_sidecar

EXPORT_SIZE = 2560  # 成品长边
SHEET_COLS = 360    # 对比小样列宽


@dataclass
class PipelineResult:
    report: CullReport
    keepers: list[PhotoScore]
    outputs: list[tuple[str, str]] = field(default_factory=list)  # (src, dst)
    out_dir: Path | None = None
    sheet: Path | None = None
    report_json: Path | None = None


def _resolve_reference(ref_path, preset, keeper_works):
    if ref_path:
        return reference_from_image(ref_path), "参考图"
    if preset:
        return reference_from_preset(preset), f"预设 {preset}"
    if not keeper_works:
        raise SystemExit("没有可用照片，无法 auto 追色")
    mus, sds = zip(*(oklab_stats(w) for w in keeper_works))
    mean = np.mean(mus, axis=0)
    std = np.mean(sds, axis=0)
    return reference_from_stats(mean, std), "auto（本批平均色调）"


def _contact_sheet(rows: list[list[np.ndarray]], labels: list[str]) -> np.ndarray:
    """rows: 每行 [原图, 追色, 美化]（尺寸可不同），统一缩放到列宽后拼图。"""
    def col_resize(img):
        h, w = img.shape[:2]
        nh = max(1, int(round(h * SHEET_COLS / w)))
        return cv2.resize(img, (SHEET_COLS, nh), interpolation=cv2.INTER_AREA)

    header = np.full((30, SHEET_COLS * len(labels) + (len(labels) - 1) * 4, 3), 245, np.uint8)
    for i, lab in enumerate(labels):
        x = i * (SHEET_COLS + 4) + SHEET_COLS // 2 - 9 * len(lab)
        cv2.putText(header, lab, (max(2, x), 21), cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, (60, 60, 60), 1, cv2.LINE_AA)

    grid_rows = []
    for row in rows:
        cells = [col_resize(im) for im in row]
        h = max(c.shape[0] for c in cells)
        padded = [np.pad(c, ((0, h - c.shape[0]), (0, 0), (0, 0)),
                         constant_values=245) for c in cells]
        grid_rows.append(np.hstack([
            np.pad(p, ((0, 0),
                       ((0, 0) if i == len(padded) - 1 else (0, 4)),
                       (0, 0)), constant_values=245)
            for i, p in enumerate(padded)]))
    body_h = sum(r.shape[0] for r in grid_rows) + 4 * (len(grid_rows) - 1)
    body = np.full((body_h, grid_rows[0].shape[1], 3), 245, np.uint8)
    y = 0
    for r in grid_rows:
        body[y:y + r.shape[0]] = r
        y += r.shape[0] + 4
    return np.vstack([header, body])


def run_pipeline(input_dir: str | Path,
                 out_dir: str | Path | None = None,
                 ref: str | Path | None = None,
                 preset: str | None = None,
                 algo: str = "oklab",
                 top: int | None = None,
                 min_score: float | None = None,
                 color_strength: float = 1.0,
                 preserve_luma: bool = False,
                 skin_protect: bool | None = None,
                 polish_params: PolishParams | None = None,
                 write_xmp: bool = False,
                 export_quality: int = 95,
                 contact: bool = True,
                 recursive: bool = False,
                 jobs: int | None = None,
                 faces: bool = True,
                 ai: bool = False,
                 wb_mode: str = "single",
                 scene_ref: bool = False) -> PipelineResult:
    p = polish_params or PolishParams()
    input_dir = Path(input_dir)
    out = Path(out_dir) if out_dir else input_dir / "photopilot_out"
    out.mkdir(parents=True, exist_ok=True)

    print(f"[1/4] 扫描并打分：{input_dir}")
    report = cull_folder(input_dir, recursive=recursive, jobs=jobs, faces=faces, ai=ai)
    if not report.photos:
        raise SystemExit(f"{input_dir} 下没有可识别的图片")
    keepers = report.keepers(top=top, min_score=min_score)
    if not keepers:
        raise SystemExit("筛选后没有保留照片，放宽 --top / --min-score 试试")
    print(f"      共 {len(report.photos)} 张，入选 {len(keepers)} 张")

    print("[2/4] 解析追色参考 …")
    works = {k.path: load_image(k.path, max_size=WORK_SIZE) for k in keepers}

    # 白平衡：single=逐张（polish 内做）；batch=整批统一（追色前先校正）
    wb_corr: dict[str, np.ndarray] = {}
    if wb_mode == "batch":
        from .polish import estimate_wb_gains, batch_target_gains, apply_wb_gains
        gains = {k.path: estimate_wb_gains(w) for k, w in works.items()}
        target = batch_target_gains(list(gains.values()))
        wb_corr = {k: target / g for k, g in gains.items()}
        p = dataclasses_replace(p, wb=0.0)   # polish 内单张 WB 让位
        print(f"      批量白平衡：整批目标增益 {target.round(3).tolist()}")
    elif wb_mode == "off":
        p = dataclasses_replace(p, wb=0.0)

    scene_groups = None
    if scene_ref and not ref and not preset and len(keepers) >= 4:
        # 多参考追色：按 OKLab 均值聚类（≤4 簇），每簇内部独立求参考
        from .color import oklab_stats as _ost, cluster_scenes, reference_from_stats as _rfs
        mus = np.stack([_ost(works[k.path])[0] for k in keepers])
        labels, centers = cluster_scenes(mus, k=min(4, max(2, len(keepers) // 3)))
        scene_groups = []            # (member_paths, ColorReference)
        for j in range(len(centers)):
            mem = [k.path for k, lb in zip(keepers, labels) if lb == j]
            if not mem:
                continue
            m_mus, m_sds = zip(*(_ost(works[p]) for p in mem))
            scene_groups.append((mem, _rfs(np.mean(m_mus, 0), np.mean(m_sds, 0))))
        ref_desc = f"多参考（{len(scene_groups)} 个场景组）"
        reference = scene_groups[0][1] if scene_groups else None   # 兜底：组外的照片
    else:
        reference, ref_desc = _resolve_reference(ref, preset, [works[k.path] for k in keepers])
    print(f"      参考来源：{ref_desc}，算法：{algo}")

    print("[3/4] 追色 + 美化 + 导出 …")
    result = PipelineResult(report=report, keepers=keepers, out_dir=out)
    sheet_rows: list[list[np.ndarray]] = []
    output_names = collision_safe_output_names(
        (k.path for k in keepers), preserve_raster=True)

    for i, k in enumerate(keepers, 1):
        work = works[k.path]
        if k.path in wb_corr:
            from .polish import apply_wb_gains
            work = apply_wb_gains(work, wb_corr[k.path], 0.95)
            works[k.path] = work
        faces_w = analyze_faces(work)
        boxes_w = [f.box for f in faces_w]
        cur_ref = reference
        if scene_groups:
            cur_ref = next((r for mem, r in scene_groups if k.path in mem), reference)
        graded = match_color(work, cur_ref, algo=algo, strength=color_strength,
                             preserve_luma=preserve_luma, skin_protect=skin_protect,
                             faces=boxes_w)
        polished = polish_image(graded, p, faces=faces_w)

        src = Path(k.path)
        # 成品按导出分辨率重新处理：人脸框/关键点一起映射过去
        final_src = load_image(src, max_size=EXPORT_SIZE)
        scale = final_src.shape[1] / work.shape[1]
        if k.path in wb_corr:
            from .polish import apply_wb_gains
            final_src = apply_wb_gains(final_src, wb_corr[k.path], 0.95)
        if scale > 1.05:
            faces_final = scale_faces(faces_w, scale)
            final_graded = match_color(final_src, cur_ref, algo=algo,
                                       strength=color_strength,
                                       preserve_luma=preserve_luma,
                                       skin_protect=skin_protect,
                                       faces=[f.box for f in faces_final])
            final = polish_image(final_graded, p, faces=faces_final)
        else:
            final = polished

        dst = out / output_names[k.path]
        save_image(final, dst, quality=export_quality)
        result.outputs.append((str(src), str(dst)))
        if write_xmp:
            write_sidecar(src, k.score, k.flags, k.sharpness, k.exposure, k.faces)
        if contact and len(sheet_rows) < 8:
            sheet_rows.append([work, graded, polished])
        print(f"      [{i}/{len(keepers)}] {src.name}  score={k.score:.2f} → {dst.name}")

    if contact and sheet_rows:
        print("[4/4] 生成对比小样 …")
        sheet = _contact_sheet(sheet_rows, ["Original", "Color", "Polish"])
        result.sheet = save_image(sheet, out / "compare_sheet.jpg", quality=90)
        print(f"      {result.sheet}")

    result.report_json = out / "report.json"
    result.report_json.write_text(json.dumps({
        "reference": ref_desc, "algo": algo,
        "settings": {"top": top, "min_score": min_score,
                     "color_strength": color_strength, "polish": vars(p),
                     "wb_mode": wb_mode, "scene_ref": scene_ref,
                     "scene_groups": len(scene_groups) if scene_groups else 0},
        "outputs": result.outputs,
        "photos": [ph.to_dict() for ph in report.ranked()],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"      报告：{result.report_json}")
    return result
