"""PhotoPilot 命令行入口。

用法示例：
  photopilot cull  ./photos --top 30 --write-xmp
  photopilot color ./photos/*.jpg --ref ref.jpg --algo oklab --strength 0.9
  photopilot polish a.jpg b.jpg --skin 0.7
  photopilot run   ./photos --preset film_warm --top 20 --out ./out
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .cull import cull_folder
from .color import (PRESETS, match_color, reference_from_image,
                    reference_from_preset)
from .polish import polish_image, PolishParams
from .io import load_image, save_image, iter_images
from .pipeline import run_pipeline
from .xmp import write_sidecar


def _print_cull(report, write_xmp: bool):
    ranked = report.ranked()
    print(f"\n{'文件':<40} {'综合':>5} {'清晰':>5} {'曝光':>5} {'脸':>4}  标记")
    print("-" * 84)
    for p in ranked:
        stars = {5: "★★★★★", 4: "★★★★", 3: "★★★", 2: "★★", 1: "★"}
        from .xmp import rating_for
        mark = stars[rating_for(p.score)]
        print(f"{Path(p.path).name:<40} {p.score:5.2f} {p.sharpness:5.2f} "
              f"{p.exposure:5.2f} {p.faces:>4}  {mark} {'、'.join(p.flags)}")
    if write_xmp:
        for p in report.photos:
            write_sidecar(p.path, p.score, p.flags, p.sharpness, p.exposure, p.faces)
        print(f"\n已写 XMP sidecar（Lightroom/darktable 导入即读评分）")


def _gather_inputs(inputs: list[str]) -> list[Path]:
    out: list[Path] = []
    for i in inputs:
        p = Path(i)
        if p.is_dir():
            out.extend(iter_images(p))
        else:
            out.append(p)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="photopilot",
                                 description="照片筛选 / 追色 / 美化（全本地）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("cull", help="批量打分筛选")
    pc.add_argument("folder")
    pc.add_argument("--top", type=int, default=None, help="只保留前 N 张")
    pc.add_argument("--min-score", type=float, default=None)
    pc.add_argument("--write-xmp", action="store_true", help="写 XMP sidecar")
    pc.add_argument("--json", default=None, help="报告输出路径")
    pc.add_argument("--recursive", action="store_true")
    pc.add_argument("--jobs", type=int, default=None, help="并行扫描线程数（默认 4）")
    pc.add_argument("--no-faces", action="store_true", help="跳过人脸分析（大图库快速档，约快 5 倍）")
    pc.add_argument("--ai", action="store_true", help="NIMA 神经网络美学评分融入综合分（本地 ONNX，约 +90ms/张）")

    pl = sub.add_parser("color", help="追色到参考图/预设")
    pl.add_argument("inputs", nargs="+")
    pl.add_argument("--ref", default=None, help="参考图路径")
    pl.add_argument("--preset", choices=list(PRESETS), default=None)
    pl.add_argument("--algo", default="oklab",
                    choices=["oklab", "reinhard", "mkl", "histogram", "luma_hist"])
    pl.add_argument("--strength", type=float, default=1.0)
    pl.add_argument("--preserve-luma", action="store_true", help="只动色相不动亮度")
    pl.add_argument("--skin-protect", action="store_true", help="肤色区域少迁移（默认：检出人脸时自动启用）")
    pl.add_argument("--out-dir", default=None)
    pl.add_argument("--suffix", default="_c")

    pb = sub.add_parser("polish", help="美化：白平衡+磨皮+人脸/瑕疵修复+局部")
    pb.add_argument("inputs", nargs="+")
    pb.add_argument("--skin", type=float, default=0.6)
    pb.add_argument("--retain", type=float, default=0.4, help="皮肤纹理保留 0-1")
    pb.add_argument("--clarity", type=float, default=0.25)
    pb.add_argument("--wb", type=float, default=0.5)
    pb.add_argument("--face-repair", type=float, default=0.0, help="人脸局部修复 0-1")
    pb.add_argument("--blemish", type=float, default=0.0, help="皮肤瑕疵修复 0-1")
    pb.add_argument("--local-region", choices=["all", "skin", "face", "eyes", "background"],
                    default="all", help="局部处理区域")
    pb.add_argument("--no-eyes", action="store_true")
    pb.add_argument("--no-global-skin", action="store_true")
    pb.add_argument("--out-dir", default=None)
    pb.add_argument("--suffix", default="_p")

    pr = sub.add_parser("run", help="完整流水线：筛选→追色→美化→导出")
    pr.add_argument("folder")
    pr.add_argument("--out", default=None)
    pr.add_argument("--ref", default=None)
    pr.add_argument("--preset", choices=list(PRESETS), default=None)
    pr.add_argument("--algo", default="oklab",
                    choices=["oklab", "reinhard", "mkl", "histogram", "luma_hist"])
    pr.add_argument("--top", type=int, default=None)
    pr.add_argument("--min-score", type=float, default=None)
    pr.add_argument("--strength", type=float, default=1.0)
    pr.add_argument("--preserve-luma", action="store_true")
    pr.add_argument("--skin-protect", action="store_true", help="肤色区域少迁移（默认：检出人脸时自动启用）")
    pr.add_argument("--skin", type=float, default=0.6)
    pr.add_argument("--retain", type=float, default=0.4, help="皮肤纹理保留 0-1")
    pr.add_argument("--clarity", type=float, default=0.25)
    pr.add_argument("--wb", type=float, default=0.5)
    pr.add_argument("--face-repair", type=float, default=0.0, help="人脸局部修复 0-1")
    pr.add_argument("--blemish", type=float, default=0.0, help="皮肤瑕疵修复 0-1")
    pr.add_argument("--local-region", choices=["all", "skin", "face", "eyes", "background"],
                    default="all", help="局部处理区域")
    pr.add_argument("--write-xmp", action="store_true")
    pr.add_argument("--no-sheet", action="store_true", help="不生成对比小样")
    pr.add_argument("--recursive", action="store_true")
    pr.add_argument("--jobs", type=int, default=None, help="并行扫描线程数（默认 4）")
    pr.add_argument("--no-faces", action="store_true", help="跳过人脸分析（大图库快速档）")
    pr.add_argument("--ai", action="store_true", help="NIMA 神经网络美学评分融入综合分")
    pr.add_argument("--wb-mode", default="single", choices=["single", "batch", "off"],
                    help="白平衡：single=逐张自动 / batch=整批统一色温 / off")
    pr.add_argument("--scene-ref", action="store_true",
                    help="多参考追色：auto 模式按场景聚 ≤4 组独立匹配（≥6 张生效）")

    pw = sub.add_parser("wb", help="批量白平衡：整批统一色温导出（不做追色/磨皮）")
    pw.add_argument("inputs", nargs="+")
    pw.add_argument("--strength", type=float, default=0.9, help="校正强度 0-1")
    pw.add_argument("--out-dir", default=None)
    pw.add_argument("--suffix", default="_wb")
    pw.add_argument("--max-size", type=int, default=2560, help="导出最长边")

    pu = sub.add_parser("ui", help="启动本地 Web UI（浏览器操作）")
    pa = sub.add_parser("app", help="桌面软件模式（原生窗口，非浏览器）")
    pa.add_argument("folder", nargs="?", default=None, help="初始照片目录（可留空）")
    pa.add_argument("--port", type=int, default=0, help="0=自动选空闲端口")
    pu.add_argument("folder", nargs="?", default=None, help="初始照片目录（可留空）")
    pu.add_argument("--port", type=int, default=8618)
    pu.add_argument("--no-browser", action="store_true")

    args = ap.parse_args(argv)

    if args.cmd == "cull":
        report = cull_folder(args.folder, recursive=args.recursive, jobs=args.jobs,
                             faces=not args.no_faces, ai=args.ai)
        _print_cull(report, args.write_xmp)
        if args.json:
            Path(args.json).write_text(report.to_json(), encoding="utf-8")
            print(f"报告：{args.json}")

    elif args.cmd == "color":
        if not args.ref and not args.preset:
            sys.exit("需要 --ref 参考图或 --preset 预设")
        reference = (reference_from_image(args.ref) if args.ref
                     else reference_from_preset(args.preset))
        for p in _gather_inputs(args.inputs):
            img = load_image(p, max_size=None)
            out = match_color(img, reference, algo=args.algo, strength=args.strength,
                              preserve_luma=args.preserve_luma,
                              skin_protect=args.skin_protect)
            dst = (Path(args.out_dir) if args.out_dir else p.parent) / \
                  f"{p.stem}{args.suffix}{p.suffix.lower()}"
            save_image(out, dst)
            print(f"{p.name} → {dst.name}")

    elif args.cmd == "polish":
        params = PolishParams(wb=args.wb, skin=args.skin, retain=args.retain,
                              clarity=args.clarity, eyes=not args.no_eyes,
                              global_skin=not args.no_global_skin,
                              face_repair=args.face_repair, blemish=args.blemish,
                              local_region=args.local_region)
        for p in _gather_inputs(args.inputs):
            img = load_image(p, max_size=None)
            out = polish_image(img, params)
            dst = (Path(args.out_dir) if args.out_dir else p.parent) / \
                  f"{p.stem}{args.suffix}{p.suffix.lower()}"
            save_image(out, dst)
            print(f"{p.name} → {dst.name}")

    elif args.cmd == "run":
        params = PolishParams(wb=args.wb, skin=args.skin, retain=args.retain,
                              clarity=args.clarity, face_repair=args.face_repair,
                              blemish=args.blemish, local_region=args.local_region)
        run_pipeline(args.folder, out_dir=args.out, ref=args.ref, preset=args.preset,
                     algo=args.algo, top=args.top, min_score=args.min_score,
                     color_strength=args.strength, preserve_luma=args.preserve_luma,
                     skin_protect=args.skin_protect, polish_params=params,
                     write_xmp=args.write_xmp, contact=not args.no_sheet,
                     recursive=args.recursive, jobs=args.jobs,
                     faces=not getattr(args, "no_faces", False),
                     ai=args.ai, wb_mode=args.wb_mode, scene_ref=args.scene_ref)

    elif args.cmd == "wb":
        paths = _gather_inputs(args.inputs)
        if not paths:
            sys.exit("没有可处理的图片")
        from .polish import estimate_wb_gains, batch_target_gains, apply_wb_gains
        print(f"[1/2] 估计 {len(paths)} 张的色温 → 整批统一目标 …")
        gains = [estimate_wb_gains(load_image(p, max_size=512)) for p in paths]
        target = batch_target_gains(gains)
        out_dir = Path(args.out_dir) if args.out_dir else None
        print(f"[2/2] 校正导出（强度 {args.strength}，目标增益 "
              f"{target.round(3).tolist()}）…")
        for p, g in zip(paths, gains):
            img = load_image(p, max_size=args.max_size)
            out = apply_wb_gains(img, target / g, args.strength)
            dst = (out_dir if out_dir else p.parent) /                   f"{p.stem}{args.suffix}{p.suffix.lower() if p.suffix.lower() != '.jpeg' else '.jpg'}"
            save_image(out, dst)
            print(f"      {p.name} → {dst.name}")

    elif args.cmd == "app":
        from .server import run_app
        run_app(folder=args.folder, port=args.port)

    elif args.cmd == "ui":
        from .server import serve
        serve(folder=args.folder, port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
