"""端到端测试（纯 Python，无需 pytest）：python tests/run_all.py

覆盖：筛选排序与连拍分组、五种追色算法、肤色保护、预设、磨皮有效性、
XMP sidecar、完整流水线产物。
"""
from __future__ import annotations

import sys
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import make_testset  # noqa: E402
from photopilot import (load_image, cull_folder, match_color, reference_from_image,  # noqa: E402
                        reference_from_preset, polish_image, PolishParams,
                        run_pipeline)
from photopilot.cull import F_DUP, smart_rank  # noqa: E402
try:  # noqa: E402 - RED 时允许报告明确的能力缺失，而非导入异常
    from photopilot.io import collision_safe_output_names, output_suffix, unique_paths
except ImportError:
    collision_safe_output_names = output_suffix = unique_paths = None
from photopilot.xmp import rating_for, label_for, read_rating  # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def main():
    ts_dir, ref_path = make_testset.generate(ROOT / "demo", force=False)
    ref_img = load_image(ref_path, max_size=1024)

    # ---------------- 1. 筛选 ----------------
    print("\n== 筛选引擎 ==")
    report = cull_folder(ts_dir)
    by_name = {Path(p.path).name: p for p in report.photos}
    check("扫描全部 12 张测试图", len(report.photos) == 12, f"实际 {len(report.photos)}")
    sb, bd = by_name["01_sharp_bright.jpg"], by_name["04_blurry_dark.jpg"]
    check("清晰明亮 > 模糊昏暗", sb.score > bd.score + 0.15,
          f"{sb.score:.3f} vs {bd.score:.3f}")
    check("模糊图被标记", "可能模糊" in bd.flags, str(bd.flags))
    check("欠曝图被标记", "曝光异常" in by_name["02_sharp_dark.jpg"].flags)
    dups = [p for p in report.photos if F_DUP in p.flags]
    check("连拍去重生效（至少 2 张标记）", len(dups) >= 2, f"{len(dups)} 张")
    burst_best = [p for p in report.photos if Path(p.path).name.startswith("burst")
                  and F_DUP not in p.flags]
    check("连拍组内保留最清晰一张",
          len(burst_best) == 1 and "burst_02" not in burst_best[0].path)
    burst_members = [p for p in report.photos if Path(p.path).name.startswith("burst")]
    check("连拍组提供组大小与最佳帧标记",
          len(burst_members) == 4 and all(p.group_size == 4 for p in burst_members)
          and sum(bool(p.group_best) for p in burst_members) == 1,
          str([(p.group_size, p.group_best) for p in burst_members]))
    smart = smart_rank(report.photos)
    burst_indexes = [smart.index(p) for p in burst_members]
    check("智能排序保持近似照片连续",
          max(burst_indexes) - min(burst_indexes) + 1 == len(burst_members),
          str(burst_indexes))
    check("报告可序列化", len(report.to_json()) > 500)

    # ---------------- 2. 追色 ----------------
    print("\n== 追色模块 ==")
    src = load_image(ts_dir / "05_cast_blue.jpg", max_size=1024)
    r_mean_src = src[..., 0].mean()
    b_mean_src = src[..., 2].mean()

    out = match_color(src, reference_from_image(ref_img), algo="oklab", strength=1.0)
    check("oklab：偏蓝图追向暖调参考（R↑ B↓）",
          out[..., 0].mean() > r_mean_src + 8 and out[..., 2].mean() < b_mean_src - 8,
          f"R {r_mean_src:.0f}→{out[..., 0].mean():.0f}, B {b_mean_src:.0f}→{out[..., 2].mean():.0f}")
    check("oklab：输出范围合法",
          out.dtype == np.uint8 and out.shape == src.shape)

    zero = match_color(src, reference_from_image(ref_img), algo="oklab", strength=0.0)
    check("strength=0 恒等", np.array_equal(zero, src))

    half = match_color(src, reference_from_image(ref_img), algo="oklab", strength=0.5)
    full = match_color(src, reference_from_image(ref_img), algo="oklab", strength=1.0)
    d_half = abs(half[..., 0].mean() - r_mean_src)
    d_full = abs(full[..., 0].mean() - r_mean_src)
    check("强度单调（0.5 迁移量 < 1.0）", d_half < d_full)

    for algo in ("reinhard", "mkl", "histogram", "luma_hist"):
        o = match_color(src, reference_from_image(ref_img), algo=algo, strength=1.0)
        moved = abs(o.astype(np.float32) - src.astype(np.float32)).mean()
        check(f"{algo}：有效且有限", np.isfinite(o).all() and moved > 1.0,
              f"平均变化 {moved:.1f}")

    pl = match_color(src, reference_from_preset("film_warm"), strength=1.0)
    check("预设 film_warm 增暖", pl[..., 0].mean() > r_mean_src)
    for preset_name in ("natural", "portrait_soft", "travel_vibrant", "mono_contrast",
                        "wedding_air", "skin_glow", "pastel_matte", "golden_hour",
                        "street_neon", "forest_deep", "ocean_air", "cinematic_night",
                        "retro_fade", "bw_soft", "wedding_airy", "wedding_blush",
                        "japanese_fresh", "japanese_milk", "korean_cream",
                        "indoor_luminous", "outdoor_clean", "golden_sunset",
                        "forest_story", "ocean_breeze", "night_city", "retro_album"):
        po = match_color(src, reference_from_preset(preset_name), strength=1.0)
        check(f"预设 {preset_name} 输出合法", po.dtype == np.uint8 and po.shape == src.shape)

    # preserve_luma 的语义是"感知亮度(OKLab L)不变"，因此用 L 的偏移量来度量
    from photopilot.colorops import rgb_to_oklab

    def mean_abs_l(a, b):
        la = rgb_to_oklab(a.astype(np.float32) / 255.0)[..., 0]
        lb = rgb_to_oklab(b.astype(np.float32) / 255.0)[..., 0]
        return float(np.abs(la - lb).mean())

    keep_luma = match_color(src, reference_from_image(ref_img), algo="oklab",
                            strength=1.0, preserve_luma=True)
    no_luma = match_color(src, reference_from_image(ref_img), algo="oklab", strength=1.0)
    d_keep = mean_abs_l(src, keep_luma)
    d_full = mean_abs_l(src, no_luma)
    check("preserve_luma 感知亮度几乎不变", d_keep < 0.02 and d_keep < d_full * 0.3,
          f"ΔL {d_keep:.4f} vs {d_full:.4f}")

    # ---------------- 3. 美化 ----------------
    print("\n== 美化模块 ==")
    port = load_image(ts_dir / "08_portrait_skin.jpg", max_size=1024)
    h, w = port.shape[:2]

    # 3a. 磨皮核心单测：直接调 smooth_skin（全强度），脸颊光滑区高频应大幅下降
    from photopilot.polish import smooth_skin, auto_tone
    out_smooth = smooth_skin(port, strength=0.8, retain=0.35, mask=None)
    # 脸颊取样区：第一张脸椭圆内、避开眼睛嘴巴（脸心 0.38w,0.42h，rx150 ry200）
    cx, cy, pw, ph = int(w * 0.38), int(h * 0.42) + 80, 120, 90
    patch_o = cv2.cvtColor(port[cy - ph // 2:cy + ph // 2, cx - pw // 2:cx + pw // 2],
                           cv2.COLOR_RGB2GRAY)
    patch_p = cv2.cvtColor(out_smooth[cy - ph // 2:cy + ph // 2, cx - pw // 2:cx + pw // 2],
                           cv2.COLOR_RGB2GRAY)

    def hf(img):
        return float(cv2.Laplacian(img, cv2.CV_64F).var())

    check("磨皮输出合法", out_smooth.shape == port.shape and out_smooth.dtype == np.uint8)
    check("脸颊高频噪声大幅下降（磨皮有效）", hf(patch_p) < hf(patch_o) * 0.5,
          f"{hf(patch_o):.0f} → {hf(patch_p):.0f}")
    check("皮肤基调未漂移（均值接近）",
          abs(patch_p.mean() - patch_o.mean()) < 10,
          f"{patch_o.mean():.0f} vs {patch_p.mean():.0f}")

    # 3a. 智能自动曝光：欠曝应提升，过曝应压回，输出始终合法。
    dark = np.zeros((96, 128, 3), np.uint8)
    dark[..., 0] = 12
    dark[..., 1] = 18
    dark[..., 2] = 24
    bright = np.zeros((96, 128, 3), np.uint8)
    bright[..., 0] = 244
    bright[..., 1] = 238
    bright[..., 2] = 232
    dark_auto = auto_tone(dark)
    bright_auto = auto_tone(bright)
    gray = lambda x: cv2.cvtColor(x, cv2.COLOR_RGB2GRAY).mean()
    check("智能自动曝光输出合法",
          dark_auto.dtype == np.uint8 and bright_auto.dtype == np.uint8
          and dark_auto.shape == dark.shape and bright_auto.shape == bright.shape)
    check("智能自动曝光提升欠曝",
          gray(dark_auto) > gray(dark) + 18,
          f"{gray(dark):.1f} → {gray(dark_auto):.1f}")
    check("智能自动曝光压低过曝",
          gray(bright_auto) < gray(bright) - 18,
          f"{gray(bright):.1f} → {gray(bright_auto):.1f}")

    # 3b. 完整美化（无人脸 → 全局弱柔肤路径）应正常出图且不过度
    params = PolishParams(wb=0.3, skin=0.8, retain=0.35, clarity=0.2)
    out = polish_image(port, params)
    check("完整美化管线输出合法", out.shape == port.shape and out.dtype == np.uint8)

    plain = polish_image(load_image(ts_dir / "05_cast_blue.jpg", max_size=1024),
                         PolishParams())
    check("无人脸图正常走全局柔肤路径", plain.shape == src.shape)

    # 3c. 人像修复与局部区域：局部增强不能污染背景。
    from photopilot.face_analysis import analyze_faces, face_region_mask
    faces_for_local = analyze_faces(port)
    face_mask = face_region_mask(port, faces_for_local)
    check("人脸局部掩码有效", bool(face_mask.max() > 0.5 and face_mask.mean() < 0.5))
    local = polish_image(port, PolishParams(skin=0, clarity=0, eyes=False,
                                             face_repair=0.8, blemish=0.7,
                                             local_region="face"), faces=faces_for_local)
    delta = np.abs(local.astype(np.float32) - port.astype(np.float32)).mean(2)
    inside = delta[face_mask > 0.5].mean() if np.any(face_mask > 0.5) else 0
    outside = delta[face_mask < 0.05].mean() if np.any(face_mask < 0.05) else 0
    check("人脸修复限定在局部", inside > max(0.5, outside * 2),
          f"脸部 {inside:.2f} / 背景 {outside:.2f}")

    # ---------------- 4. 人脸分析（EAR/掩码几何/真实人像） ----------------
    print("\n== 人脸分析 ==")
    from photopilot.face_analysis import (eye_aspect_ratio, FaceInfo,
                                          skin_mask_from_faces, eye_regions,
                                          analyze_faces)

    # EAR 数学：几何构造睁眼/闭眼
    open_eye = np.array([[0, 0], [0, -8], [0, -8], [40, 0], [0, 8], [0, 8]], np.float32)
    shut_eye = np.array([[0, 0], [0, -1], [0, -1], [40, 0], [0, 1], [0, 1]], np.float32)
    check("EAR 数学：睁眼 > 0.25", eye_aspect_ratio(open_eye) > 0.25,
          f"{eye_aspect_ratio(open_eye):.3f}")
    check("EAR 数学：闭眼 < 0.1", eye_aspect_ratio(shut_eye) < 0.1,
          f"{eye_aspect_ratio(shut_eye):.3f}")

    real = ROOT / "demo" / "hopper.jpg"
    if real.exists():
        rimg = load_image(real, max_size=None)
        rfaces = analyze_faces(rimg)
        check("真实人像：检出 1 张带关键点的人脸",
              len(rfaces) == 1 and rfaces[0].mesh)
        check("真实人像：睁眼照片不误判闭眼",
              not rfaces[0].blink and min(rfaces[0].ear) > 0.2,
              f"EAR {rfaces[0].ear[0]:.2f}/{rfaces[0].ear[1]:.2f}")
        check("真实人像：两只眼睛精确定位", len(eye_regions(rfaces)) == 2)
        rm = skin_mask_from_faces(rimg, rfaces)
        fb = rfaces[0].box
        fcx, fcy = fb[0] + fb[2] // 2, fb[1] + fb[3] // 2
        check("真实人像：掩码盖住脸颊、避开背景",
              rm[fcy, fcx] > 0.5 and rm[10, 10] < 0.1,
              f"脸心 {rm[fcy, fcx]:.2f} / 角落 {rm[10, 10]:.2f}")
        rout = polish_image(rimg, PolishParams(skin=0.7, retain=0.4, clarity=0.2, wb=0.3),
                            faces=rfaces)
        ex, ey, ew, eh = eye_regions(rfaces)[0]
        d_eye = float(np.abs(rout[ey:ey+eh, ex:ex+ew].astype(int)
                             - rimg[ey:ey+eh, ex:ex+ew].astype(int)).mean())
        d_bg = float(np.abs(rout[10:40, 10:40].astype(int)
                            - rimg[10:40, 10:40].astype(int)).mean())
        check("真实人像：眼部提亮生效且强于背景", d_eye > d_bg,
              f"眼 {d_eye:.1f} vs 背景 {d_bg:.1f}")
    else:
        print("[SKIP] 无 demo/hopper.jpg，跳过真实人像测试")

    # ---------------- 5. XMP ----------------
    print("\n== XMP sidecar ==")
    check("星级映射", rating_for(0.9) == 5 and rating_for(0.7) == 4
          and rating_for(0.2) == 1 and label_for(0.9) == "Keeper")
    from photopilot.xmp import write_sidecar
    xmp_path = write_sidecar(ts_dir / "01_sharp_bright.jpg",
                             by_name["01_sharp_bright.jpg"].score,
                             by_name["01_sharp_bright.jpg"].flags)
    tree = ET.parse(xmp_path)
    ns = {"xmp": "http://ns.adobe.com/xap/1.0/"}
    rating = tree.getroot().find(".//xmp:Rating", ns)
    check("sidecar 可解析且含 Rating", rating is not None and rating.text.isdigit())
    write_sidecar(ts_dir / "02_sharp_dark.jpg", .5, [], rating=4)
    check("手动星级可从 XMP 读回", read_rating(ts_dir / "02_sharp_dark.jpg") == 4)

    # ---------------- 6. 智能排序/比较 UI 契约 ----------------
    print("\n== 智能排序与比较 UI ==")
    server_source = (ROOT / "photopilot" / "server.py").read_text(encoding="utf-8")
    check("UI 暴露智能排序/筛选/星级", all(s in server_source for s in
          ("智能分组", "星级", "比较 2–4 张")))
    check("UI 提供同步缩放比较", "compareZoom" in server_source and "同步缩放" in server_source)
    check("UI 预设按类别展示", "人像 / 婚礼" in server_source and "风光 / 街拍" in server_source)
    check("UI 提供人像修复与局部区域", all(s in server_source for s in
          ("faceRepair", "blemish", "localRegion", "局部区域")))
    lb_css = re.search(r"#lbStage\{([^}]*)\}", server_source)
    check("灯箱照片限制在可用画布内居中", bool(lb_css) and all(s in lb_css.group(1) for s in
          ("display:flex", "align-items:center", "justify-content:center", "overflow:hidden")))
    check("工具栏使用横向分组且品牌点不是伪控制", all(s in server_source for s in
          ("toolbarRow", "toolbarActions", "brandMark")) and ".dots" not in server_source)
    check("精扫更新就地刷新卡片避免相似组排序闪烁", "updateCardInPlace" in server_source
          and "card.replaceWith(nc)" not in server_source)
    check("重排视图不重播卡片入场动画", "function cardFor" in server_source
          and "replaceChildren" in server_source)
    check("相似组重排复用已有照片卡片", "cardCache" in server_source
          and "replaceChildren" in server_source
          and "$('grid').innerHTML='';renderedN=0;renderChunk(false)" not in server_source)
    check("终态按精扫阶段只重排一次", "FINAL_STAGE" in server_source
          and "finalStage" in server_source and "FINAL_STAGE!==finalStage" in server_source)
    check("UI提供智能自动曝光", all(s in server_source for s in
          ("autoTone", "auto_tone", "智能自动曝光")))
    check("单张修图使用大画布预览", all(s in server_source for s in
          ("single-result", "preview_size", "单张修图")))

    # ---------------- 5. 完整流水线 ----------------
    print("\n== 完整流水线 ==")
    out_dir = ROOT / "demo" / "pipeline_out"
    res = run_pipeline(ts_dir, out_dir=out_dir, preset="film_warm",
                       top=6, color_strength=0.9, write_xmp=True)
    outputs = [Path(d) for (_, d) in res.outputs]
    check("导出 ≥4 张成品", len(outputs) >= 4, f"{len(outputs)} 张")
    check("成品文件真实存在且非空", all(o.exists() and o.stat().st_size > 10_000 for o in outputs))
    check("对比小样已生成", res.sheet and res.sheet.exists())
    check("report.json 存在", res.report_json and res.report_json.exists())
    import json as _json
    data = _json.loads(res.report_json.read_text(encoding="utf-8"))
    check("report.json 结构完整",
          len(data["photos"]) == 12 and data["reference"].startswith("预设"))

    # ---------------- 6. 性能行为（并行一致性 / 参考缓存 / draft 解码） ----------------
    print("\n== 性能行为 ==")
    r1 = cull_folder(ts_dir, jobs=1)
    r2 = cull_folder(ts_dir, jobs=3)   # 12 张 ≥ PARALLEL_MIN → 走线程池
    s1 = {p.path: (p.score, tuple(p.flags)) for p in r1.photos}
    s2 = {p.path: (p.score, tuple(p.flags)) for p in r2.photos}
    check("并行扫描与串行结果一致", s1 == s2,
          f"{sum(s1[k] == s2[k] for k in s1)}/{len(s1)} 张相同")

    # LibRaw 在同一进程内并发 postprocess 偶发长时间占住所有扫描线程；
    # RAW 解码必须互斥，后续的指标/缩略图仍可并行。
    io_source = (ROOT / "photopilot" / "io.py").read_text(encoding="utf-8")
    check("RAW 解码使用进程内互斥", "RAW_DECODE_LOCK" in io_source
          and "with RAW_DECODE_LOCK" in io_source)
    server_source = (ROOT / "photopilot" / "server.py").read_text(encoding="utf-8")
    check("快速扫描优先使用 RAW 内置预览", "raw_preview=job[\"fast\"]" in server_source)

    ref2 = reference_from_image(ref_img)
    out_a = match_color(src, ref2, algo="oklab", strength=1.0)
    check("参考统计已入缓存", "oklab" in ref2._cache)
    out_b = match_color(src, ref2, algo="oklab", strength=1.0)
    check("缓存后结果逐像素确定", np.array_equal(out_a, out_b))

    big = ROOT / "demo" / "bench_big.jpg"
    if big.exists():
        from photopilot.io import resize_max
        full = load_image(big, max_size=None)
        draft = load_image(big, max_size=1024)
        check("draft 解码输出尺寸正确", max(draft.shape[:2]) == 1024)
        full_small = resize_max(full, 1024)
        diff = float(np.abs(draft.astype(np.int16) - full_small.astype(np.int16)).mean())
        check("draft 与全解码内容一致（DCT 降采样容差内）", diff < 4.0, f"平均差 {diff:.2f}")
    else:
        print("[SKIP] 无 demo/bench_big.jpg，跳过 draft 解码测试")

    # ---------------- 7. Web 服务端函数级冒烟（不经浏览器） ----------------
    print("\n== Web 服务端 ==")
    from photopilot import server as S
    import time as _time

    j = S._scan_start({"folder": str(ts_dir), "jobs": 2, "faces": False})
    jid = j["job"]
    final = None
    for _ in range(120):   # 最多等 60s
        st = S._scan_status({"job": [jid], "since": ["0"]})
        if st["finished"]:
            final = st
            break
        _time.sleep(0.5)
    check("后台扫描任务能完成", final is not None)
    if final:
        check("终态含全部照片与缩略图 URL",
              len(final["final"]) == 12 and final["final"][0]["thumb"].startswith("/api/thumb"))
        check("终态无收尾错误", final.get("finalize_error") is None)
    check("scan_status 对 list 型 query 正常（parse_qs 兼容）", True)
    check("取消接口响应 ok", S._scan_cancel({"job": jid}) == {"ok": True})

    up = S.Handler.__dict__  # 触发 Handler 类定义加载（导入期已完成，无异常即通过）
    check("Handler 类定义完整", callable(up.get("do_POST")) and callable(up.get("do_GET")))
    check("INDEX_HTML 含新前端要素",
          all(k in S.INDEX_HTML for k in ("facesOpt", "scanbar", "grid.addEventListener('scroll'", "__INIT_FOLDER_JSON__")))
    import inspect as _inspect
    check("启动目录安全嵌入内联脚本",
          "</script>" not in S._script_json("</script><script>alert(1)</script>")
          and "__INIT_FOLDER_JSON__" in S.INDEX_HTML
          and "html.escape(placeholder, quote=True)" in _inspect.getsource(S.Handler.do_GET))
    # 导入与旧扫描并发时，新请求必须使旧轮询失效，不能覆盖新 job；
    # 失效 job 由前端一次性重扫，不得静默采用别的任务。
    check("导入请求有代际保护",
          "SCAN_REQ_GEN" in S.INDEX_HTML
          and "if(req!==SCAN_REQ_GEN)return" in S.INDEX_HTML
          and "if(j.job&&j.job!==JOB)" not in S.INDEX_HTML)
    check("前端 GET 轮询使用有效的 fetch 参数",
          "fetch(path,body?{method:'POST'" in S.INDEX_HTML
          and ":undefined)" in S.INDEX_HTML)
    check("照片网格使用稳定的可读缩略图比例",
          "aspect-ratio:3/2" in S.INDEX_HTML
          and "grid-auto-rows:minmax" in S.INDEX_HTML
          and "min-height:var(--thumb-min)" in S.INDEX_HTML)
    check("照片网格支持密度切换",
          "data-density" in S.INDEX_HTML
          and "setDensity" in S.INDEX_HTML
          and "viewbar" in S.INDEX_HTML)
    rebuild_src = S.INDEX_HTML.split("function rebuildView", 1)[1].split("function rememberView", 1)[0]
    poll_src = S.INDEX_HTML.split("async function poll", 1)[1].split("function cancelScan", 1)[0]
    check("大图库分块渲染且扫描中仍可继续加载",
          "Math.max(CHUNK,renderedN)" in rebuild_src
          and "Math.max(CHUNK,renderedN)" in poll_src
          and "if(renderedN<ranked.length" in S.INDEX_HTML
          and "if(!JOB&&renderedN<ranked.length" not in S.INDEX_HTML)
    check("结果栏显示总数与选中数",
          "updateStats" in S.INDEX_HTML
          and "已选" in S.INDEX_HTML)
    check("精扫更新保留照片对象",
          "ranked[i]={...u,thumb:ranked[i].thumb}" in S.INDEX_HTML
          and "ranked[i]=u|" not in S.INDEX_HTML)
    update_card_src = S.INDEX_HTML.split("function updateCardInPlace", 1)[1].split("function cardFor", 1)[0]
    check("评分精扫按线性复杂度更新图库", "updateStats();" not in update_card_src
          and "ranked.reduce" in S.INDEX_HTML)
    selected_compare_src = S.INDEX_HTML.split("function openSelectedCompare", 1)[1].split("function closeGroupCompare", 1)[0]
    check("比较选择超过四张时明确拒绝", "selected.length>4" in selected_compare_src
          and "selected.slice(0,4)" in selected_compare_src)
    make_card_src = S.INDEX_HTML.split("function makeCard", 1)[1].split("async function scan", 1)[0]
    check("照片文件名在动态 HTML 中转义", "escapeHtml" in make_card_src)
    toggle_star_src = S.INDEX_HTML.split("async function toggleStar", 1)[1].split("let compareGroupId", 1)[0]
    check("星标失败恢复原有星级", "const previousRating=p.rating" in toggle_star_src
          and "p.rating=previousRating" in toggle_star_src)
    rate_src = S.INDEX_HTML.split("async function lbRate", 1)[1].split("document.addEventListener('keydown'", 1)[0]
    check("快审评分失败恢复显示状态", "const previousRating=p.rating" in rate_src
          and "p.rating=previousRating" in rate_src)
    check("星级更新同步所有照片视图",
          "function syncRatingUI" in S.INDEX_HTML
          and "pane.dataset.path=p.path" in S.INDEX_HTML
          and "aria-pressed" in S.INDEX_HTML)
    check("输出格式/名称辅助函数可用", callable(output_suffix)
          and callable(collision_safe_output_names))
    if callable(output_suffix) and callable(collision_safe_output_names):
        check("RAW 导出使用可识别格式", output_suffix("capture.ARW", preserve_raster=True) == ".jpg")
        with tempfile.TemporaryDirectory(prefix="photopilot-name-map-") as output_tmp:
            base = Path(output_tmp)
            collisions = collision_safe_output_names([base / "day1" / "IMG_1.ARW",
                                                       base / "day2" / "IMG_1.ARW"],
                                                      preserve_raster=True)
            check("同名照片导出文件不会互相覆盖",
                  len(set(collisions.values())) == 2
                  and all(name.endswith("_pp.jpg") for name in collisions.values()))

    with tempfile.TemporaryDirectory(prefix="photopilot-thumb-cache-") as cache_tmp:
        previous_thumb_state = (S.THUMB_DIR, S._CACHE_CAP, S._thumb_cache.copy())
        try:
            S.THUMB_DIR = Path(cache_tmp)
            S._CACHE_CAP = 2
            S._thumb_cache.clear()
            sources = sorted(ts_dir.glob("*.jpg"))[:5]
            for source in sources:
                S._thumb(str(source), 64)
            for source in sources:  # 第二轮命中磁盘缓存也必须遵守内存 LRU 上限
                S._thumb(str(source), 64)
            check("磁盘缩略图命中仍受内存缓存上限约束",
                  len(S._thumb_cache) <= S._CACHE_CAP,
                  f"{len(S._thumb_cache)} / {S._CACHE_CAP}")
        finally:
            S.THUMB_DIR, S._CACHE_CAP, cached = previous_thumb_state
            S._thumb_cache.clear()
            S._thumb_cache.update(cached)

    # 任务上限只应淘汰已结束任务，不能把仍在运行的导入任务踢掉。
    saved_jobs = dict(S.SCAN_JOBS)
    old_thread_cls = S.threading.Thread
    class _NoopThread:
        def __init__(self, *args, **kwargs): pass
        def start(self): pass
    try:
        S.SCAN_JOBS.clear()
        for i in range(8):
            S.SCAN_JOBS[f"active-{i}"] = {"finished": False, "started": float(i)}
        S.threading.Thread = _NoopThread
        active_job = S._scan_start({"folder": str(ts_dir),
                                    "paths": [str(ts_dir / "01_sharp_bright.jpg")],
                                    "faces": False, "ai": False})["job"]
        check("任务上限不淘汰运行中的导入任务",
              "active-0" in S.SCAN_JOBS and active_job in S.SCAN_JOBS)
        check("任务上限不淘汰精扫中的已出图任务",
          not S._job_evictable({"finished": True, "cancelled": False,
                                "fast": True, "faces": True, "ai": True,
                                "refine_finished": False}))
    finally:
        S.threading.Thread = old_thread_cls
        S.SCAN_JOBS.clear()
        S.SCAN_JOBS.update(saved_jobs)

    # MediaPipe/OpenCV 的 FaceMesh 在 macOS 上不能并发调用；并发时会触发
    # “Can't fetch data from terminated TLS container” 原生异常并杀掉宿主进程。
    import photopilot.face_analysis as _FA
    from concurrent.futures import ThreadPoolExecutor as _TPE
    old_impl = _FA._analyze_faces_impl
    active_calls = [0]
    peak_calls = [0]
    calls_lock = _FA.threading.Lock()
    def _fake_face_impl(_img):
        with calls_lock:
            active_calls[0] += 1
            peak_calls[0] = max(peak_calls[0], active_calls[0])
        _time.sleep(0.03)
        with calls_lock:
            active_calls[0] -= 1
        return []
    try:
        _FA._analyze_faces_impl = _fake_face_impl
        with _TPE(max_workers=4) as ex:
            list(ex.map(_FA.analyze_faces, [np.zeros((16, 16, 3), np.uint8)] * 4))
        check("FaceMesh 检测串行化避免原生并发崩溃", peak_calls[0] == 1,
              f"峰值并发 {peak_calls[0]}")
    finally:
        _FA._analyze_faces_impl = old_impl

    # ---------------- 8. 快审模式：星级 API 与灯箱要素 ----------------
    print("\n== 快审模式 ==")
    target = ts_dir / "01_sharp_bright.jpg"
    xmp_before = target.with_suffix(".xmp")
    if xmp_before.exists():
        xmp_before.unlink()
    r = S._rate({"path": str(target), "rating": 5, "score": 0.42,
                 "flags": ["手动"], "faces": 1})
    check("rate API 返回 ok", r.get("ok") is True and r.get("rating") == 5)
    check("XMP sidecar 已写出", xmp_before.exists())
    if xmp_before.exists():
        xt = xmp_before.read_text()
        check("手动星级覆盖自动映射（5 星不随低分回退）",
              "<xmp:Rating>5</xmp:Rating>" in xt, xt.split("<xmp:Rating>")[1][:2] if "<xmp:Rating>" in xt else "?")
        check("手动星级 label 落 Keeper", "<xmp:Label>Keeper</xmp:Label>" in xt)
        check("XMP 软件版本与项目一致",
              f"PhotoPilot {__import__('photopilot').__version__}" in xt)
    r0 = S._rate({"path": str(target), "rating": 0, "score": 0.42})
    xt0 = xmp_before.read_text()
    check("0 星清除并落 Reject 档",
          r0.get("rating") == 0 and "<xmp:Rating>0</xmp:Rating>" in xt0
          and "<xmp:Label>Reject</xmp:Label>" in xt0)
    try:
        S._rate({"path": str(ts_dir / "no_such.jpg"), "rating": 3})
        check("不存在文件报错", False)
    except FileNotFoundError:
        check("不存在文件报错", True)
    check("灯箱前端要素齐全",
          all(k in S.INDEX_HTML for k in
              ("lbImg", "/api/raw_img", "lbRate", "lbNav", "ondblclick")))
    # 追色与美化必须是两个明确动作；追色还需要先指定网格中的目标照片。
    check("追色与美化入口分离",
          'id="colorBtn"' in S.INDEX_HTML and 'id="polishBtn"' in S.INDEX_HTML
          and "process('color')" in S.INDEX_HTML
          and "process('polish')" in S.INDEX_HTML
          and "处理已选 → 追色 + 美化" not in S.INDEX_HTML)
    check("追色必须选择目标照片",
          "targetBtn" in S.INDEX_HTML and "targetPath" in S.INDEX_HTML
          and "请选择一张目标照片" in S.INDEX_HTML)
    from photopilot.xmp import write_sidecar as _ws
    _x = _ws(target, 0.9, ["通过"], rating=None)   # 缺省路径仍按分数自动映射
    check("write_sidecar 缺省自动映射不回归",
          "<xmp:Rating>5</xmp:Rating>" in _x.read_text() and "<xmp:Label>Keeper</xmp:Label>" in _x.read_text())
    xmp_before.unlink(missing_ok=True)

    # ---------------- 9. AI 美学评分（NIMA ONNX） ----------------
    print("\n== AI 美学评分 ==")
    from photopilot.aesthetic import available as ai_avail, nima_score, blend_with_traditional
    if ai_avail():
        test_img = load_image(ts_dir / "01_sharp_bright.jpg", max_size=1024)
        s1 = nima_score(test_img)
        check("NIMA 评分在 1-10 区间", s1 is not None and 1.0 <= s1 <= 10.0, f"{s1}")
        s2 = nima_score(test_img)
        check("NIMA 评分确定（逐像素推理无随机）", s1 == s2)
        b1 = blend_with_traditional(0.4, 9.5)
        b2 = blend_with_traditional(0.4, 1.0)
        b3 = blend_with_traditional(0.4, None)
        check("融合：高分抬升 / 低分拉低 / 缺失原样", b1 > 0.4 and b2 < 0.4 and b3 == 0.4)
        from photopilot.cull import score_photo as _sp
        ps_ai = _sp(test_img, "x.jpg", faces=False, ai=True)
        ps_no = _sp(test_img, "x.jpg", faces=False, ai=False)
        check("score_photo(ai=True) 填充 ai 字段", ps_ai.ai is not None and 1 <= ps_ai.ai <= 10)
        check("score_photo(ai=False) 不动 ai 字段", ps_no.ai is None)
        check("AI 融合改变了综合分", abs(ps_ai.score - ps_no.score) > 1e-6)
    else:
        print("[SKIP] 无 NIMA 模型，跳过 AI 评分测试")

    # ---------------- 10. 批量白平衡 ----------------
    print("\n== 批量白平衡 ==")
    from photopilot.polish import (estimate_wb_gains, batch_target_gains,
                                   apply_wb_gains)
    rng = np.random.default_rng(7)
    base = rng.integers(60, 200, (96, 96, 3)).astype(np.uint8)
    warm = np.clip(base.astype(np.float32) * np.array([1.18, 1.0, 0.82]), 0, 255).astype(np.uint8)
    cool = np.clip(base.astype(np.float32) * np.array([0.82, 1.0, 1.18]), 0, 255).astype(np.uint8)
    g_warm, g_cool = estimate_wb_gains(warm), estimate_wb_gains(cool)
    target = batch_target_gains([g_warm, g_cool])
    corr_w = apply_wb_gains(warm, target / g_warm, 1.0)
    corr_c = apply_wb_gains(cool, target / g_cool, 1.0)
    before = float(np.abs(estimate_wb_gains(warm) - estimate_wb_gains(cool)).sum())
    after = float(np.abs(estimate_wb_gains(corr_w) - estimate_wb_gains(corr_c)).sum())
    check("批量白平衡收敛（校正后跨张色温差显著缩小）", after < before * 0.35,
          f"before={before:.4f} after={after:.4f}")
    check("强度 0 不改变图像", np.array_equal(apply_wb_gains(warm, target / g_warm, 0.0), warm))
    half = apply_wb_gains(warm, target / g_warm, 0.5)
    check("强度 0.5 介于原图与全校正之间", not np.array_equal(half, warm)
          and not np.array_equal(half, corr_w))

    # ---------------- 11. 桌面模式 / CLI 要素 ----------------
    print("\n== 桌面模式与 CLI ==")
    from photopilot import cli as C, server as S2
    import io as _io, contextlib
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            C.main(["wb", str(ts_dir), "--out-dir", str(ts_dir / "wb_out"), "--strength", "0.9"])
        except SystemExit as e:
            print("exit:", e)
    check("CLI wb 批量白平衡跑通", (ts_dir / "wb_out").exists()
          and len(list((ts_dir / "wb_out").glob("*_wb.jpg"))) >= 4,
          f"{len(list((ts_dir / 'wb_out').glob('*_wb.jpg'))) if (ts_dir / 'wb_out').exists() else 0} 张")
    check("run_app / start_server 存在",
          callable(getattr(S2, "run_app", None)) and callable(getattr(S2, "start_server", None)))
    acquire_lock = getattr(S2, "_acquire_app_instance_lock", None)
    if callable(acquire_lock):
        with tempfile.TemporaryDirectory(prefix="photopilot-app-lock-") as lock_tmp:
            lock_path = Path(lock_tmp) / "app.lock"
            first_lock = acquire_lock(lock_path)
            duplicate_lock = acquire_lock(lock_path)
            check("桌面模式拒绝重复实例", first_lock is not None and duplicate_lock is None)
            if first_lock:
                first_lock.close()
            next_lock = acquire_lock(lock_path)
            check("桌面窗口退出后可再次启动", next_lock is not None)
            if next_lock:
                next_lock.close()
    else:
        check("桌面模式拒绝重复实例", False, "缺少进程级单实例锁")
    check("桌面入口启动前检查单实例锁",
          "_acquire_app_instance_lock()" in _inspect.getsource(S2.run_app)
          and "if instance_lock is None" in _inspect.getsource(S2.run_app))
    check("INDEX_HTML 含 AI / 批量白平衡 / 桌面要素",
          all(k in S.INDEX_HTML for k in ("aiOpt", "aiSmart", "wbSeg", "wb_mode")))
    # ---------------- 12. 多参考追色（场景聚类） ----------------
    print("\n== 多参考追色 ==")
    from photopilot.color import cluster_scenes
    mus = np.array([[0.90, 0.02, 0.03], [0.92, 0.03, 0.02],       # 亮暖组
                    [0.20, -0.04, -0.03], [0.22, -0.03, -0.04],   # 暗冷组
                    [0.55, 0.00, 0.00]], np.float32)              # 中性单例
    labels, centers = cluster_scenes(mus, 3)
    check("聚类分组正确（同场景同组）",
          labels[0] == labels[1] and labels[2] == labels[3] and len(set(labels)) == 3,
          str(labels))
    labels2, _ = cluster_scenes(mus, 3)
    check("聚类结果确定（固定种子）", (labels2 == labels).all())
    # k 大于样本数时安全收缩
    labels3, c3 = cluster_scenes(mus[:2], 5)
    check("k>n 安全收缩", len(c3) <= 2 and len(set(labels3)) <= 2)

    # pipeline 端到端：合成数据跑 scene_ref=True
    import shutil as _sh
    from photopilot import pipeline as _pp
    _run_pipeline = _pp.run_pipeline
    scene_dir = ts_dir / "scene_ref_out"
    out_prev = ts_dir / "photopilot_out"
    if out_prev.exists():
        _sh.rmtree(out_prev)
    try:
        res = _run_pipeline(str(ts_dir), out_dir=str(scene_dir), preset=None,
                           algo="oklab", top=None, color_strength=0.9, write_xmp=False,
                           contact=False, faces=False, ai=False, wb_mode="off",
                           scene_ref=True)
        check("scene_ref 流水线跑通并导出", len(res.outputs) >= 4,
              f"{len(res.outputs)} 张")
        import json as _json
        rep = _json.loads((scene_dir / "report.json").read_text())
        check("report 记录场景组数", rep["settings"]["scene_ref"] is True
              and rep["settings"]["scene_groups"] >= 1,
              f"groups={rep['settings']['scene_groups']}")
    finally:
        if scene_dir.exists():
            _sh.rmtree(scene_dir, ignore_errors=True)

    # 场景聚类仍是 CLI/后端能力；桌面 UI 的目标照片流程不再放置一个永远隐藏
    # 且与显式目标互斥的无效控件。
    check("桌面 UI 不含隐藏场景控件", "rowScene" not in S.INDEX_HTML
          and "sceneOpt" not in S.INDEX_HTML)
    check("cluster_scenes 导出于 color 包", callable(cluster_scenes))

    # ---------------- 13. 本机路径直导（导入优先） ----------------
    print("\n== 本机路径直导 ==")
    r = S._import_paths({"paths": [str(ts_dir)], "recursive": False})
    check("import_paths 登记目录", r["count"] == 12 and r["scan_dirs"] == [str(ts_dir)],
          f"count={r['count']}")
    r2 = S._import_paths({"paths": [str(ts_dir / "01_sharp_bright.jpg")]})
    check("import_paths 单文件", r2["count"] == 1
          and r2["scan_dirs"] == [str(ts_dir / "01_sharp_bright.jpg").replace("01_sharp_bright.jpg", "").rstrip("/")])
    # 顶部路径框也允许直接输入一张照片，而不是只能输入目录。
    one_file_job = S._scan_start({"folder": str(ts_dir / "01_sharp_bright.jpg"),
                                  "faces": False, "ai": False})["job"]
    check("路径框可直接扫描单张照片",
          S.SCAN_JOBS[one_file_job]["override_paths"] == [str(ts_dir / "01_sharp_bright.jpg")])
    try:
        S._import_paths({"paths": [str(ts_dir / "empty-nope")]})
        check("空路径报错", False)
    except ValueError:
        check("空路径报错", True)
    check("INDEX_HTML 含直导/完整上传要素",
          all(k in S.INDEX_HTML for k in ("import_paths", "uploadFiles", "uploadAbort")))
    with tempfile.TemporaryDirectory(prefix="photopilot-overlap-import-") as overlap_tmp:
        root = Path(overlap_tmp)
        child = root / "nested"
        child.mkdir()
        (root / "root.jpg").write_bytes(b"root")
        (child / "nested.jpg").write_bytes(b"nested")
        old_start, old_roots = S._scan_start, S.UPLOAD_ROOTS
        captured = {}
        try:
            def fake_scan(payload):
                captured["payload"] = payload
                return {"job": "overlap-import"}
            S._scan_start = fake_scan
            S.UPLOAD_ROOTS = [root / "uploads"]
            imported = S._import_paths({"paths": [str(root), str(child)], "recursive": True})
            check("重叠目录导入不会重复照片", imported["count"] == 2
                  and len(captured["payload"]["paths"]) == 2
                  and len(set(captured["payload"]["paths"])) == 2)
        finally:
            S._scan_start, S.UPLOAD_ROOTS = old_start, old_roots
    check("本机路径列表按真实文件去重且顺序稳定",
          callable(unique_paths) and unique_paths([str(target), str(target)]) == [str(target)])

    # ---------------- 14. 回归：导入/处理链路边界 ----------------
    print("\n== 回归：导入/处理链路边界 ==")
    # _process_one 不能在成品已写出后再因日志变量错误返回 400。
    # 用小型替身隔离图像模型，只验证接口收尾语义。
    import dataclasses as _dc
    process_src = ts_dir / "01_sharp_bright.jpg"
    process_out = Path(tempfile.mkdtemp(prefix="photopilot-process-"))
    old_funcs = {name: getattr(S, name) for name in
                 ("load_image", "analyze_faces", "polish_image", "save_image")}
    old_sessions = S._SESSIONS
    try:
        S.load_image = lambda *_a, **_k: np.zeros((8, 8, 3), dtype=np.uint8)
        S.analyze_faces = lambda *_a, **_k: []
        S.polish_image = lambda img, *_a, **_k: img
        S.save_image = lambda img, dst, **_k: Path(dst).write_bytes(b"jpeg")
        S._SESSIONS = {"regression": {
            "folder": process_out, "paths": [str(process_src)],
            "wb_mode": "off", "reference": None,
            "params": _dc.replace(S.PolishParams(), wb=0.0),
        }}
        try:
            processed = S._process_one({"sid": "regression", "path": str(process_src)})
            check("处理接口成功返回成品", processed.get("out", "").endswith("_pp.jpg"))
        except Exception as e:
            check("处理接口成功返回成品", False, f"{type(e).__name__}: {e}")
    finally:
        for name, fn in old_funcs.items():
            setattr(S, name, fn)
        S._SESSIONS = old_sessions

    # 追色操作必须显式给出一张真实目标照片；美化操作不应强制解析追色参考。
    try:
        S._prepare({"folder": str(process_out), "paths": [str(process_src)],
                    "operation": "color"})
        check("追色缺少目标照片时报错", False)
    except ValueError:
        check("追色缺少目标照片时报错", True)
    color_prep = S._prepare({"folder": str(process_out), "paths": [str(process_src)],
                             "operation": "color", "ref": str(process_src)})
    color_session = S._SESSIONS.get(color_prep["sid"], {})
    check("追色会话只执行追色",
          color_session.get("operation") == "color"
          and color_session.get("do_color") is True
          and color_session.get("do_polish") is False
          and color_session.get("ref_desc", "").startswith("目标照片："))
    S._SESSIONS.pop(color_prep["sid"], None)
    polish_prep = S._prepare({"folder": str(process_out), "paths": [str(process_src)],
                              "operation": "polish"})
    polish_session = S._SESSIONS.get(polish_prep["sid"], {})
    check("美化操作独立于追色参考",
          polish_session.get("operation") == "polish"
          and polish_session.get("do_color") is False
          and polish_session.get("do_polish") is True)
    S._SESSIONS.pop(polish_prep["sid"], None)

    # 取消必须立即结束前端的 refine_pending 状态，并记录为已取消；
    # 不存在的任务不能静默接管最近一次任务。
    saved_scan_jobs = dict(S.SCAN_JOBS)
    try:
        S.SCAN_JOBS.clear()
        S.SCAN_JOBS["audit-cancel"] = {
            "done": 1, "total": 1, "finished": False, "cancelled": False,
            "skipped": 0, "started": 0.0, "fast": True, "faces": True,
            "ai": False, "refine_finished": False, "refine_cancel": False,
            "updates": [], "photos_final": [], "photos": [], "cancel": False,
        }
        S._scan_cancel({"job": "audit-cancel"})
        cancelled = S._scan_status({"job": ["audit-cancel"], "since": ["0"]})
        check("取消任务标记完整",
              cancelled["cancelled"] is True and cancelled["finished"] is True
              and cancelled["refine_pending"] is False)
        try:
            S._scan_status({"job": ["definitely-missing"], "since": ["0"]})
            check("不存在任务返回错误", False)
        except ValueError:
            check("不存在任务返回错误", True)
    finally:
        S.SCAN_JOBS.clear()
        S.SCAN_JOBS.update(saved_scan_jobs)

    # 大批量处理不能静默丢掉第 501 张；选择状态也必须独立于当前已渲染 DOM。
    many_paths = [str(process_src)] * 501
    many_prep = S._prepare({"folder": str(process_out), "paths": many_paths,
                            "operation": "polish"})
    many_session = S._SESSIONS.get(many_prep["sid"], {})
    check("大批量处理不静默截断", len(many_session.get("paths", [])) == 501)
    S._SESSIONS.pop(many_prep["sid"], None)
    check("大图库选择状态不依赖 DOM",
          "selectedPaths" in S.INDEX_HTML
          and "ranked.filter(p=>selectedPaths.has(p.path))" in S.INDEX_HTML)
    check("大图库滚动加载绑定实际滚动容器",
          "grid.addEventListener('scroll'" in S.INDEX_HTML
          and "grid.scrollTop+grid.clientHeight" in S.INDEX_HTML)

    # 浏览器/拖拽上传应保留相对目录；同名文件不得互相覆盖。
    with tempfile.TemporaryDirectory(prefix="photopilot-upload-") as upload_tmp:
        upload_root = Path(upload_tmp)
        old_upload_roots, old_import_dirs = S.UPLOAD_ROOTS, dict(S._import_dirs)
        try:
            S.UPLOAD_ROOTS = [upload_root / "uploads"]
            S._import_dirs.clear()
            class _UploadHandler:
                def __init__(self, filename):
                    self.headers = {"X-Filename": filename, "X-Batch": "audit-upload"}
                    self.response = None
                def _json(self, obj, code=200):
                    self.response = obj
            h1 = _UploadHandler("trip/day1/IMG_0001.jpg")
            h2 = _UploadHandler("trip/day2/IMG_0001.jpg")
            S._upload(h1, b"day-one")
            S._upload(h2, b"day-two")
            root = Path(h1.response["folder"])
            check("上传保留子目录且不覆盖同名文件",
                  (root / "trip/day1/IMG_0001.jpg").read_bytes() == b"day-one"
                  and (root / "trip/day2/IMG_0001.jpg").read_bytes() == b"day-two")
        finally:
            S.UPLOAD_ROOTS = old_upload_roots
            S._import_dirs.clear()
            S._import_dirs.update(old_import_dirs)

    upload_block = S.INDEX_HTML[S.INDEX_HTML.index("async function uploadFiles"):
                                S.INDEX_HTML.index("async function importLocalPaths")]
    check("上传全部完成后再启动扫描",
          "triggerScan" not in upload_block
          and upload_block.index("await Promise.all") < upload_block.rindex("scan({recursive:"))
    check("缩略图缓存使用共享锁", "_THUMB_LOCK" in S._thumb.__code__.co_names)
    check("移除无效遗留控件样式", "#go{" not in S.INDEX_HTML)

    # process_one 的两个操作也必须互斥调用对应管线，避免 UI 拆分后后端又偷偷串联。
    old_process_funcs = {name: getattr(S, name) for name in
                         ("load_image", "analyze_faces", "match_color",
                          "polish_image", "save_image")}
    process_calls = []
    try:
        S.load_image = lambda *_a, **_k: np.zeros((8, 8, 3), dtype=np.uint8)
        S.analyze_faces = lambda *_a, **_k: []
        S.match_color = lambda img, *_a, **_k: process_calls.append("color") or img
        S.polish_image = lambda img, *_a, **_k: process_calls.append("polish") or img
        S.save_image = lambda img, dst, **_k: Path(dst).write_bytes(b"jpeg")
        common_session = {
            "folder": process_out, "paths": [str(process_src)],
            "algo": "oklab", "strength": 0.9, "preserve_luma": False,
            "skin_protect": None, "wb_mode": "off", "reference": object(),
            "params": _dc.replace(S.PolishParams(), wb=0.0),
        }
        S._SESSIONS = {"color-only": {**common_session, "operation": "color",
                                       "do_color": True, "do_polish": False}}
        S._process_one({"sid": "color-only", "path": str(process_src)})
        check("追色只调用追色管线", process_calls == ["color"], str(process_calls))
        process_calls.clear()
        S._SESSIONS = {"polish-only": {**common_session, "operation": "polish",
                                        "do_color": False, "do_polish": True,
                                        "reference": None}}
        S._process_one({"sid": "polish-only", "path": str(process_src)})
        check("美化只调用美化管线", process_calls == ["polish"], str(process_calls))
    finally:
        for name, fn in old_process_funcs.items():
            setattr(S, name, fn)
        S._SESSIONS = old_sessions

    # 多文件夹选择必须把所有已解析文件交给同一个扫描任务，不能只扫第一目录。
    with tempfile.TemporaryDirectory(prefix="photopilot-multi-") as multi_tmp:
        multi_dir = Path(multi_tmp)
        extra = multi_dir / "extra.jpg"
        extra.write_bytes(process_src.read_bytes())
        old_start, old_roots = S._scan_start, S.UPLOAD_ROOTS
        captured = {}
        try:
            def fake_scan(payload):
                captured["payload"] = payload
                return {"job": "regression-multi"}
            S._scan_start = fake_scan
            S.UPLOAD_ROOTS = [multi_dir / "uploads"]
            multi = S._import_paths({"paths": [str(ts_dir), str(multi_dir)]})
            payload = captured.get("payload", {})
            selected = set(payload.get("paths", []))
            check("多文件夹导入不丢文件",
                  multi["count"] == 13 and str(extra) in selected
                  and len(selected) == 13)
        finally:
            S._scan_start, S.UPLOAD_ROOTS = old_start, old_roots

    # 解释器退出/线程池关闭时，后台精扫不能把 RuntimeError traceback 打到用户日志。
    import contextlib as _ctx
    import io as _str_io
    class _ClosedPool:
        def submit(self, *_a, **_k):
            raise RuntimeError("cannot schedule new futures after shutdown")
    class _ImmediateThread:
        def __init__(self, target, args=(), daemon=None):
            self.target, self.args = target, args
        def start(self):
            self.target(*self.args)
    old_scan_funcs = {name: getattr(S, name) for name in
                      ("load_image", "score_photo", "dhash", "_thumb_bytes_from_img", "_get_pool")}
    old_thread = S.threading.Thread
    shutdown_job = "regression-shutdown"
    try:
        def fake_score(img, path, faces=True, ai=False):
            return S.PhotoScore(str(path), 8, 8, .5, .5, .5, .5, .5, .5, 0, 0)
        S.load_image = lambda *_a, **_k: np.zeros((8, 8, 3), dtype=np.uint8)
        S.score_photo = fake_score
        S.dhash = lambda *_a, **_k: np.zeros(64, dtype=bool)
        S._thumb_bytes_from_img = lambda *_a, **_k: b""
        S._get_pool = lambda *_a, **_k: _ClosedPool()
        S.threading.Thread = _ImmediateThread
        S.SCAN_JOBS[shutdown_job] = {
            "folder": str(ts_dir), "total": 0, "done": 0, "skipped": 0,
            "photos": [], "hashes": {}, "finished": False, "cancelled": False,
            "cancel": False, "started": 0.0, "faces": True, "ai": False,
            "recursive": False, "fast": True, "updates": [], "refine_done": 0,
            "refine_total": 0, "refine_finished": False, "refine_cancel": False,
            "jobs": 1, "override_paths": [str(process_src)],
        }
        err = _str_io.StringIO()
        with _ctx.redirect_stderr(err):
            S._scan_worker(shutdown_job)
        check("精扫线程池关闭时不打印 traceback", "Traceback" not in err.getvalue())
    finally:
        for name, fn in old_scan_funcs.items():
            setattr(S, name, fn)
        S.threading.Thread = old_thread
        S.SCAN_JOBS.pop(shutdown_job, None)

    # 发布版本应由项目版本统一驱动，避免源码、模块和桌面包显示不同版本。
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project_version = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M).group(1)
    init_version = __import__("photopilot").__version__
    import runpy as _runpy
    build_ns = _runpy.run_path(str(ROOT / "scripts" / "build_app.py"))
    build_versions = {build_ns.get("APP_VERSION")}
    launcher_template = build_ns.get("LAUNCHER", "")
    check("桌面启动器过滤 LaunchServices 参数",
          "-psn_*)" in launcher_template and '"${{args[@]}}"' in launcher_template
          and "/usr/bin/arch -arm64" in launcher_template)
    egg_info = (ROOT / "photopilot.egg-info" / "PKG-INFO")
    egg_match = re.search(r'^Version:\s*([^\n]+)', egg_info.read_text(encoding="utf-8"), re.M)
    egg_version = egg_match.group(1).strip() if egg_match else None
    check("项目版本元数据一致",
          init_version == project_version and build_versions == {project_version}
          and egg_version == project_version,
          f"project={project_version}, init={init_version}, app={sorted(build_versions)}, egg={egg_version}")

    app_plist = Path("/Applications/PhotoPilot.app/Contents/Info.plist")
    if app_plist.exists():
        import plistlib as _pl
        info = _pl.loads(app_plist.read_bytes())
        check("PhotoPilot.app 已安装且配置正确",
              info.get("CFBundleExecutable") == "PhotoPilot"
              and (app_plist.parent / "MacOS" / "PhotoPilot").exists()
              and (app_plist.parent / "Resources" / "PhotoPilot.icns").exists())
    else:
        print("[SKIP] /Applications/PhotoPilot.app 不存在（未构建桌面包）")

    print("\n" + "=" * 50)
    if failures:
        print(f"共 {len(failures)} 项失败：{failures}")
        sys.exit(1)
    print("全部测试通过 ✔")


if __name__ == "__main__":
    main()
