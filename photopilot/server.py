"""PhotoPilot 本地 Web UI（零新增依赖，纯标准库 HTTP 服务）。

仅绑定 127.0.0.1，只应在本机使用。启动：photopilot ui [目录]

接口：
  GET  /                     单页前端（Apple 风格）
  GET  /api/thumb?p=&s=      缩略图 JPEG（磁盘缓存 + 内存 LRU）
  GET  /api/img?p=           原始文件（用于展示成品）
  GET  /api/presets          风格预设列表
  GET  /api/scan_status      {job, since} → 后台扫描进度与增量结果
  POST /api/scan             {folder, jobs, faces} → 启动后台扫描任务
  POST /api/scan_cancel      {job} → 请求取消（保留已扫部分）
  POST /api/prepare          操作 + 选中照片 + 参数 → 会话 sid（追色目标单独指定）
  POST /api/process_one      {sid, path} → 单张成品（前端逐张调用，进度真实）
  POST /api/upload           拖拽/文件选择导入（原始字节 + X-Filename/X-Batch 头）
  POST /api/import_paths     {paths[], recursive} → 本机路径直导（不拷贝，秒级）
  POST /api/rate             {path, rating(0-5), score...} → 写 XMP 星级 sidecar
  GET  /api/raw_img?p=       快审灯箱大图（2200px，磁盘缓存）
"""
from __future__ import annotations

import errno
import hashlib
import html
import json
import os
import threading
import time
import urllib.parse
import uuid
from collections import OrderedDict
from concurrent.futures import as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np

from .io import (load_image, save_image, iter_images, unique_paths,
                 collision_safe_output_names, output_suffix)
from .cull import (score_photo, dhash, finalize_cull, smart_rank, PhotoScore, WORK_SIZE,
                   _get_pool, _get_fast_pool)
from .color import (match_color, reference_from_image, reference_from_preset,
                    reference_from_stats, oklab_stats, PRESETS, PRESET_META)
from .polish import (polish_image, PolishParams, estimate_wb_gains,
                     batch_target_gains, apply_wb_gains)
from .face_analysis import analyze_faces, scale_faces
from .xmp import rating_for, read_rating, write_sidecar

PROCESS_LOCK = threading.Lock()   # FaceMesh 检测串行化，避免偶发竞态
_THUMB_LOCK = threading.Lock()     # 缩略图 LRU 由扫描线程与 HTTP 线程共享
EXPORT_SIZE = 2048                # Web 预览导出尺寸（比 run 命令略小，求快）

_thumb_cache: OrderedDict[tuple, bytes] = OrderedDict()
_CACHE_CAP = 600
_SESSIONS: dict[str, dict] = {}   # sid → 处理会话（参考图 + 参数）
_import_dirs: dict[str, Path] = {}  # 上传批次 → 落盘目录
# 拖入/选择导入的照片落盘位置：优先用户看得见的"图片"目录，失败（如 TCC 拒绝）
# 回退到项目目录
UPLOAD_ROOTS = [Path.home() / "Pictures" / "PhotoPilot",
                Path(__file__).resolve().parents[1] / "uploads"]

# 缩略图磁盘缓存：万张级图库二次扫描/滚动浏览接近零开销
_THUMB_DIR_CANDS = [Path.home() / ".cache" / "photopilot" / "thumbs",
                    Path(__file__).resolve().parents[1] / ".thumbcache"]
THUMB_DIR = _THUMB_DIR_CANDS[0]
try:
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    THUMB_DIR = _THUMB_DIR_CANDS[1]
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
THUMB_KEEP = 40000  # 张数上限，超出删最旧的

# 后台扫描任务：job_id → {total, done, photos, hashes, finished, cancelled, ...}
SCAN_JOBS: dict[str, dict] = {}


def _script_json(value: str) -> str:
    """把路径安全写入 script 文本，避免特殊字符结束内联脚本。"""
    return (json.dumps(value, ensure_ascii=True)
            .replace("&", "\\u0026")
            .replace("<", "\\u003c")
            .replace(">", "\\u003e"))


def _thumb_disk(path: str, size: int) -> Path:
    st = Path(path).stat()
    key = f"{path}|{st.st_mtime_ns}|{size}"
    h = hashlib.sha1(key.encode()).hexdigest()
    return THUMB_DIR / h[:2] / f"{h}.jpg"


def _remember_thumb(key: tuple, data: bytes) -> None:
    with _THUMB_LOCK:
        _thumb_cache[key] = data
        _thumb_cache.move_to_end(key)
        while len(_thumb_cache) > max(1, int(_CACHE_CAP)):
            _thumb_cache.popitem(last=False)


def _thumb_gc():
    """张数超限淘汰最旧缩略图（尽力而为）。"""
    try:
        files = list(THUMB_DIR.glob("*/*.jpg"))
        if len(files) <= THUMB_KEEP:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for p in files[:len(files) - int(THUMB_KEEP * 0.9)]:
            p.unlink(missing_ok=True)
    except Exception:
        pass


def _thumb_bytes_from_img(img: np.ndarray, src_path: str, size: int = 360) -> bytes:
    """从已解码图像生成缩略图并写磁盘缓存（扫描时顺带预热）。"""
    h, w = img.shape[:2]
    s = size / max(h, w)
    if s < 1:
        img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))),
                         interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(img, cv2.COLOR_RGB2BGR),
                           [cv2.IMWRITE_JPEG_QUALITY, 82])
    if not ok:
        raise ValueError("缩略图编码失败")
    data = buf.tobytes()
    try:
        dp = _thumb_disk(src_path, size)
        dp.parent.mkdir(parents=True, exist_ok=True)
        dp.write_bytes(data)
    except Exception:
        pass
    return data


def _thumb_url(path: str, size: int) -> str:
    return f"/api/thumb?p={urllib.parse.quote(path)}&s={size}"


def _thumb(path: str, size: int) -> bytes:
    """缩略图：磁盘缓存优先（扫描时已预热），未命中现生成并回写。"""
    try:
        source = Path(path)
        stat = source.stat()
        key = (str(source), size, stat.st_mtime_ns)
    except OSError:
        raise FileNotFoundError(path)
    with _THUMB_LOCK:
        if key in _thumb_cache:
            _thumb_cache.move_to_end(key)
            return _thumb_cache[key]
    try:
        dp = _thumb_disk(path, size)
        if dp.exists():
            data = dp.read_bytes()
            _remember_thumb(key, data)
            return data
    except OSError:
        pass
    img = load_image(path, max_size=size)
    data = _thumb_bytes_from_img(img, path, size)
    _remember_thumb(key, data)
    return data


def _warm_engine():
    """预热扫描引擎：向常驻池提交 N 个预热任务，把每个工作线程的
    FaceMesh + NIMA 线程本地会话都加载好。

    启动时后台调用：用户首次点「扫描打分」就是热态，单张也秒级。
    """
    try:
        import numpy as _np
        from .cull import _get_pool, score_photo
        ex = _get_pool(min(4, os.cpu_count() or 1))
        probe = _np.full((224, 224, 3), 128, _np.uint8)
        futs = [ex.submit(score_photo, probe, f"warmup-{i}", True, True)
                for i in range(min(4, os.cpu_count() or 1))]
        for f in futs:
            f.result()
        print("[warmup] 扫描引擎预热完成（人脸 + AI 模型已常驻）")
    except Exception as e:
        print(f"[warmup] 引擎预热失败（不影响功能）：{e}")


def _job_evictable(job: dict) -> bool:
    """Whether a scan record is safe to remove from the small in-memory cache.

    Fast scans mark ``finished`` before their optional face/AI refine thread
    completes.  Those records must remain addressable until the refine phase
    is done, otherwise the UI sees “任务不存在” and starts duplicate scans.
    """
    if job.get("cancelled"):
        return True
    if not job.get("finished"):
        return False
    heavy = bool(job.get("fast") and (job.get("faces") or job.get("ai")))
    return not heavy or bool(job.get("refine_finished"))


def _scan_start(payload: dict) -> dict:
    """启动后台扫描任务，立即返回 job_id；前端轮询 /api/scan_status 增量取结果。

    payload.paths 给定时只扫这些文件（file_mode：单张/多张导入不牵连整目录）。
    """
    raw_paths = payload.get("paths") or None
    override_paths = unique_paths(raw_paths) if raw_paths else None
    folder = Path(payload["folder"] or ".")
    # The top path field is intentionally a single input for both folders and
    # photos.  Normalize a file path into the same explicit-list mode used by
    # the native photo picker instead of rejecting it as a non-directory.
    if not override_paths and folder.is_file():
        override_paths = [str(folder)]
        folder = folder.parent
    if not override_paths and not folder.is_dir():
        raise ValueError(f"目录不存在：{folder}")
    job_id = uuid.uuid4().hex[:12]
    job = {
        "folder": str(folder), "total": 0, "done": 0, "skipped": 0,
        "photos": [], "hashes": {}, "finished": False, "cancelled": False,
        "cancel": False, "started": time.time(),
        "faces": bool(payload.get("faces", True)),
        "ai": bool(payload.get("ai", False)),
        "recursive": bool(payload.get("recursive", False)),
        "fast": bool(payload.get("fast", True)),   # 两段式：极速出图 + 精扫更新
        "updates": [], "refine_done": 0, "refine_total": 0,
        "refine_finished": False, "refine_cancel": False,
        "jobs": payload.get("jobs"), "futures": [],
    }
    SCAN_JOBS[job_id] = job
    if len(SCAN_JOBS) > 8:
        # 只淘汰已结束任务；活动中的导入任务必须保留到前端拿到终态，
        # 否则任务上限会把合法的 job 变成“任务不存在”。
        stale = next((jid for jid, old in SCAN_JOBS.items()
                      if jid != job_id and _job_evictable(old)), None)
        if stale is not None:
            SCAN_JOBS.pop(stale, None)
    job["override_paths"] = override_paths
    threading.Thread(target=_scan_worker, args=(job_id,), daemon=True).start()
    return {"job": job_id}


def _scan_worker(job_id: str):
    job = SCAN_JOBS.get(job_id)
    if job is None:
        return
    if job.get("override_paths"):
        paths = [str(p) for p in job["override_paths"] if Path(p).is_file()]
    else:
        paths = [str(p) for p in iter_images(job["folder"], recursive=job["recursive"])]
    job["total"] = len(paths)
    jobs_n = job["jobs"] or min(4, os.cpu_count() or 1)

    need_heavy = job["fast"] and (job["faces"] or job["ai"])
    full_for_fast = not job["fast"]   # fast=False：极速段直接跑全量（旧行为）

    def worker_fast(path: str):
        """极速段：只解码 + 基础指标 + 缩略图（无模型，秒级/张）。"""
        # RAW 优先使用相机写入的 JPEG 预览（毫秒级），避免批量 LibRaw
        # 显影把 78 张照片拖成分钟级；完整显影留给后续处理/精修。
        img = load_image(path, max_size=WORK_SIZE, raw_preview=job["fast"])
        ps = score_photo(img, path, faces=full_for_fast and job["faces"],
                         ai=full_for_fast and job["ai"])
        h = dhash(img)
        try:
            _thumb_bytes_from_img(img, path, 360)
        except Exception:
            pass
        return ps, h

    def worker_full(path: str):
        """精扫段：基础分已算过的图补人脸 + AI。"""
        img = load_image(path, max_size=WORK_SIZE)
        return score_photo(img, path, faces=job["faces"], ai=job["ai"])

    use_pool = jobs_n > 1 and len(paths) >= 2
    ex = _get_fast_pool(jobs_n) if (use_pool and need_heavy) else \
        (_get_pool(jobs_n) if use_pool else None)   # 有精扫段时极速走轻池，两不抢
    futs = {}
    try:
        if ex is not None:
            futs = {ex.submit(worker_fast, p): p for p in paths}
            job["futures"] = list(futs)
            for fut in as_completed(futs):
                if job["cancel"]:
                    break
                p = futs[fut]
                try:
                    ps, h = fut.result()
                    job["photos"].append(ps)
                    job["hashes"][ps.path] = h
                except Exception as e:
                    job["skipped"] += 1
                    print(f"[cull] 跳过无法读取的文件 {p}: {e}")
                job["done"] += 1
        else:
            for p in paths:
                if job["cancel"]:
                    break
                try:
                    ps, h = worker_fast(p)
                    job["photos"].append(ps)
                    job["hashes"][ps.path] = h
                except Exception as e:
                    job["skipped"] += 1
                    print(f"[cull] 跳过无法读取的文件 {p}: {e}")
                job["done"] += 1
    finally:
        # 取消时尽量撤掉尚未开始的 RAW 解码；正在运行的 C 扩展任务会
        # 自然返回，避免线程池继续吞掉下一次扫描的全部并发度。
        for fut in futs:
            fut.cancel()
        job["futures"] = []

    # ---- 精扫段：后台补人脸/AI，评分原地更新（不阻塞前端出图）----
    if need_heavy and not job["cancel"] and job["photos"]:
        def refine():
            try:
                photos = [p for p in job["photos"] if isinstance(p, PhotoScore)]
                job["refine_total"] = len(photos)
                pool = _get_pool(max(1, jobs_n))   # 常驻池：模型会话跨任务复用
                try:
                    futs2 = {pool.submit(worker_full, ps.path): ps for ps in photos}
                except RuntimeError as e:
                    # 解释器退出或宿主主动关闭线程池时，后台 daemon 线程可能
                    # 正好落在 submit；这不是用户任务失败，不应打印 traceback。
                    if "shutdown" in str(e).lower():
                        job["refine_finished"] = True
                        return
                    raise
                for fut in as_completed(futs2):
                    if job["refine_cancel"] or job["cancel"]:
                        return
                    ps_old = futs2[fut]
                    try:
                        ps_new = fut.result()
                        # 回写：photos 里的对象原地替换（final 重建时取到新分）
                        for idx, p in enumerate(job["photos"]):
                            if isinstance(p, PhotoScore) and p.path == ps_new.path:
                                job["photos"][idx] = ps_new
                                break
                        job["updates"].append(ps_new.to_dict())
                    except Exception:
                        pass
                    job["refine_done"] += 1
                # 精扫收尾：重跑批内相对模糊/连拍分组，重建终态排序
                refined = [p for p in job["photos"] if isinstance(p, PhotoScore)]
                finalize_cull(refined, job["hashes"])
                job["photos_final"] = smart_rank(refined)
                job["refine_finished"] = True
            except Exception:
                import traceback
                traceback.print_exc()
            finally:
                # 取消也必须收敛 refine 状态，否则前端会永久保持轮询。
                job["refine_finished"] = True
                if job.get("cancel"):
                    job["cancelled"] = True
        threading.Thread(target=refine, daemon=True).start()
    # 收尾（含已扫部分）：批内相对模糊 + 连拍分组
    try:
        photos = [p for p in job["photos"] if isinstance(p, PhotoScore)]
        finalize_cull(photos, job["hashes"])
        job["photos_final"] = smart_rank(photos)
    except Exception as e:   # 收尾失败也要放行前端，不能永远 pending
        import traceback
        traceback.print_exc()
        job["photos_final"] = []
        job["finalize_error"] = str(e)
    job["cancelled"] = bool(job.get("cancel"))
    job["finished"] = True
    _thumb_gc()
    # 会话里只留可序列化摘要，释放对象
    job["done"] = len(job["photos_final"]) + job["skipped"]


def _scan_status(query: dict) -> dict:
    # parse_qs 的值是列表：query["job"] == ["abc"]
    requested = query.get("job", [""])[0]
    job = SCAN_JOBS.get(requested)
    if job is None:
        # 绝不能把过期/拼错的 job 静默映射到另一批照片；前端已有一次性
        # 自动重扫逻辑，会在这里收到明确错误后恢复。
        raise ValueError("任务不存在或已过期")
    since = int(query.get("since", ["0"])[0])
    out = {
        "job": requested,
        "done": job["done"], "total": job["total"],
        "finished": job["finished"], "cancelled": job["cancelled"],
        "skipped": job["skipped"],
        "elapsed": round(time.time() - job["started"], 1),
    }
    out["refine_pending"] = bool(job.get("fast") and (job.get("faces") or job.get("ai"))
                                 and not job.get("refine_finished"))
    if job["updates"]:
        out["updates"] = job["updates"]
        out["refine_done"] = job["refine_done"]
        out["refine_total"] = job["refine_total"]
        job["updates"] = []          # 取走即清（增量语义）
    def stored_rating(p: PhotoScore) -> int:
        value = read_rating(p.path)
        # 没有人工星级时保持 0，避免“已加星”筛选把所有自动评分照片都误当成用户收藏。
        return 0 if value is None else value

    if job["finished"]:
        out["finalize_error"] = job.get("finalize_error")
        out["final"] = [p.to_dict() | {"thumb": _thumb_url(p.path, 360),
                                       "rating": stored_rating(p)}
                        for p in job["photos_final"]]
    else:
        new = job["photos"][since:]
        out["since"] = since
        out["photos"] = [p.to_dict() | {"thumb": _thumb_url(p.path, 360),
                                        "rating": stored_rating(p)}
                         for p in new]
    return out


def _scan_cancel(payload: dict) -> dict:
    job = SCAN_JOBS.get(payload.get("job", ""))
    if job is not None:
        job["cancel"] = True
        job["refine_cancel"] = True
        for fut in job.get("futures", ()):
            fut.cancel()
        # 立即收敛前端状态；后台 worker 仍会在 finally 中再次确认。
        # 把当前已扫部分作为终态，避免某个卡住的 RAW 解码让 UI 永久等待。
        if not job.get("finished"):
            partial = [p for p in list(job.get("photos", ()))
                       if isinstance(p, PhotoScore)]
            try:
                finalize_cull(partial, dict(job.get("hashes", {})))
            except Exception:
                pass
            job["photos_final"] = smart_rank(partial)
            job["finished"] = True
        job["cancelled"] = True
        job["refine_finished"] = True
    return {"ok": True}


def _rate(payload: dict) -> dict:
    """手动星级 → 写 XMP sidecar（0=清除星级），Lightroom / darktable 导入即读。"""
    path = Path(payload.get("path", ""))
    if not path.is_file():
        raise FileNotFoundError(str(path))
    rating = max(0, min(5, int(payload.get("rating", 0))))
    label = "Keeper" if rating >= 4 else "Review" if rating >= 2 else "Reject"
    xmp_path = write_sidecar(
        path, float(payload.get("score") or 0), list(payload.get("flags") or []),
        float(payload.get("sharpness") or 0), float(payload.get("exposure") or 0),
        int(payload.get("faces") or 0), rating=rating, label=label)
    return {"ok": True, "xmp": str(xmp_path), "rating": rating}


def _prepare(payload: dict) -> dict:
    """解析一次处理会话与参数，返回会话 id；随后逐张 process_one。

    auto 参考用流式统计（逐张加载即算即弃），万张级选择也不会撑爆内存。
    """
    folder = Path(payload["folder"])
    paths = [Path(p) for p in payload.get("paths", [])]
    if not paths:
        raise ValueError("未选择照片")

    operation = str(payload.get("operation", "both")).lower()
    if operation not in {"color", "polish", "both"}:
        raise ValueError("未知处理操作")
    do_color = operation in {"color", "both"}
    do_polish = operation in {"polish", "both"}

    sp = payload.get("skin_protect")
    ses = {
        "folder": folder,
        "operation": operation,
        "do_color": do_color,
        "do_polish": do_polish,
        "algo": payload.get("algo", "oklab"),
        "strength": float(payload.get("strength", 1.0)),
        "preserve_luma": bool(payload.get("preserve_luma", False)),
        "skin_protect": None if sp is None else bool(sp),  # None=自动（有脸即保护）
        "params": PolishParams(
            wb=float(payload.get("polish", {}).get("wb", 0.5)),
            skin=float(payload.get("polish", {}).get("skin", 0.6)),
            retain=float(payload.get("polish", {}).get("retain", 0.4)),
            clarity=float(payload.get("polish", {}).get("clarity", 0.25)),
            eyes=bool(payload.get("polish", {}).get("eyes", True)),
            face_repair=float(payload.get("polish", {}).get("face_repair", 0.0)),
            blemish=float(payload.get("polish", {}).get("blemish", 0.0)),
            local_region=str(payload.get("polish", {}).get("local_region", "all")),
            auto_tone=bool(payload.get("polish", {}).get("auto_tone", False))),
        "paths": [str(p) for p in paths],
        "output_names": collision_safe_output_names(str(p) for p in paths),
    }
    preset, ref = payload.get("preset") or None, payload.get("ref") or None
    ses["scene_ref"] = bool(payload.get("scene_ref", False))
    color_off = (payload.get("color_off") is True) or (preset is None and ref is None
                 and not ses["scene_ref"] and payload.get("algo") == "none")
    ses["wb_mode"] = payload.get("wb_mode", "off")   # off | single | batch
    ses["wb_strength"] = float(payload.get("wb_strength", 0.95))
    if ses["wb_mode"] == "batch":
        # 批量白平衡：每张估计通道增益 → 取中位数为整批统一目标
        gains = [estimate_wb_gains(load_image(p, max_size=512)) for p in paths]
        tg = batch_target_gains(gains)          # 整批统一目标（中位数，抗离群）
        ses["wb_target"] = tg
        ses["wb_gains"] = {str(p): tg / g for p, g in zip(paths, gains)}
    if operation == "color":
        if not ref:
            raise ValueError("追色需要先选择一张目标照片")
        ref_path = Path(ref).expanduser()
        if not ref_path.is_file():
            raise ValueError(f"目标照片不存在：{ref_path}")
        ses["reference"], ses["ref_desc"] = reference_from_image(ref_path), \
            f"目标照片：{ref_path.name}"
    elif operation == "polish":
        ses["reference"], ses["ref_desc"] = None, "仅美化（不追色）"
    elif color_off:
        ses["reference"], ses["ref_desc"] = None, "仅白平衡/美化（不追色）"
    elif preset:
        ses["reference"], ses["ref_desc"] = reference_from_preset(preset), f"预设 {preset}"
    elif ref:
        ses["reference"], ses["ref_desc"] = reference_from_image(ref), "参考图"
    elif ses["scene_ref"] and len(paths) >= 6:
        # 多参考追色：按 OKLab 均值聚类成 ≤4 个场景组，每组独立参考
        from .color import cluster_scenes
        mus_l, sds_l = [], []
        for p in paths:
            st = oklab_stats(load_image(p, max_size=1024))
            mus_l.append(st[0]); sds_l.append(st[1])
        labels, centers = cluster_scenes(np.stack(mus_l), k=min(4, len(paths) // 3))
        groups = []   # [path, ...] 列表 + 各组参考
        g_map = {}    # path -> ColorReference
        for j in range(len(centers)):
            mem = [p for p, lb in zip(paths, labels) if lb == j]
            if not mem:
                continue
            g_mus = [mus_l[i] for i, lb in enumerate(labels) if lb == j]
            g_sds = [sds_l[i] for i, lb in enumerate(labels) if lb == j]
            g_ref = reference_from_stats(np.mean(g_mus, 0), np.mean(g_sds, 0))
            for p in mem:
                g_map[str(p)] = g_ref
            groups.append(mem)
        ses["scene_refs"] = g_map
        ses["reference"] = reference_from_stats(np.mean(mus_l, 0), np.mean(sds_l, 0))
        ses["ref_desc"] = f"多参考（{len(groups)} 个场景组）"
    else:
        mus, sds = [], []
        for p in paths:
            st = oklab_stats(load_image(p, max_size=1024))
            mus.append(st[0]); sds.append(st[1])
        ses["reference"] = reference_from_stats(np.mean(mus, 0), np.mean(sds, 0))
        ses["ref_desc"] = "auto（本批平均色调）"
    sid = uuid.uuid4().hex[:12]
    _SESSIONS[sid] = ses
    if len(_SESSIONS) > 16:   # 只保留最近会话
        _SESSIONS.pop(next(iter(_SESSIONS)))
    return {"sid": sid, "ref_desc": ses["ref_desc"], "count": len(paths)}


def _process_one(payload: dict) -> dict:
    ses = _SESSIONS.get(payload.get("sid", ""))
    if ses is None:
        raise ValueError("会话已过期，请重新处理")
    path = Path(payload["path"])
    if str(path) not in ses["paths"]:
        raise ValueError(f"照片不在会话中：{path.name}")
    try:
        preview_size = int(payload.get("preview_size", 640))
    except (TypeError, ValueError):
        preview_size = 640
    # 批量结果保持轻量；单张修图由前端请求更大的对比预览，但限制上限避免
    # 恶意/误填尺寸导致无谓的缓存和内存占用。
    preview_size = max(640, min(preview_size, 2048))
    started = time.time()

    out_dir = ses["folder"] / "photopilot_out"
    out_dir.mkdir(parents=True, exist_ok=True)

    do_color = ses.get("do_color", True)
    do_polish = ses.get("do_polish", True)
    wb_mode = ses.get("wb_mode", "off")
    wb_str = float(ses.get("wb_strength", 0.95))
    wb_gain = ses.get("wb_gains", {}).get(str(path))

    with PROCESS_LOCK:
        work = load_image(path, max_size=1024)
        if wb_mode == "batch" and wb_gain is not None:
            work = apply_wb_gains(work, wb_gain, wb_str)
        elif wb_mode == "single" and wb_str > 0:
            from .polish import auto_white_balance
            work = auto_white_balance(work, wb_str)
        faces_w = analyze_faces(work)
        cur_ref = ses.get("scene_refs", {}).get(str(path), ses["reference"])
        if not do_color or cur_ref is None:
            graded = work                       # 批量白平衡-only：跳过追色
        else:
            graded = match_color(work, cur_ref, algo=ses["algo"],
                                 strength=ses["strength"],
                                 preserve_luma=ses["preserve_luma"],
                                 skin_protect=ses["skin_protect"],
                                 faces=[f.box for f in faces_w])
        params = ses["params"]
        if wb_mode in ("batch", "single"):
            from dataclasses import replace as _dc_replace
            params = _dc_replace(params, wb=0.0)   # 已做 WB，polish 不重复
        final = polish_image(graded, params, faces=faces_w) if do_polish else graded

        full = load_image(path, max_size=EXPORT_SIZE)
        scale = full.shape[1] / work.shape[1]
        if scale > 1.05:
            faces_f = scale_faces(faces_w, scale)
            if wb_mode == "batch" and wb_gain is not None:
                full = apply_wb_gains(full, wb_gain, wb_str)
            elif wb_mode == "single" and wb_str > 0:
                from .polish import auto_white_balance
                full = auto_white_balance(full, wb_str)
            if not do_color or cur_ref is None:
                graded_f = full
            else:
                graded_f = match_color(full, cur_ref, algo=ses["algo"],
                                       strength=ses["strength"],
                                       preserve_luma=ses["preserve_luma"],
                                       skin_protect=ses["skin_protect"],
                                       faces=[f.box for f in faces_f])
            final = polish_image(graded_f, params, faces=faces_f) if do_polish else graded_f

    output_name = ses.get("output_names", {}).get(
        str(path), f"{path.stem}_pp{output_suffix(path)}")
    dst = out_dir / output_name
    save_image(final, dst)
    print(f"[process] 完成 {path.name}，耗时 {time.time()-started:.1f}s → {dst.name}")
    return {"src": str(path), "out": str(dst),
            "before": _thumb_url(str(path), preview_size),
            "after": f"/api/img?p={urllib.parse.quote(str(dst))}"}


def _import_paths(payload: dict) -> dict:
    """桌面模式：把本机已有照片/文件夹直接登记为批次（不拷贝文件，秒级），
    并在服务端一步启动扫描任务（返回 job，前端只轮询）。"""
    paths = [str(Path(p).expanduser()) for p in payload.get("paths", []) if str(p).strip()]
    files, folders = [], []
    for p in paths:
        if Path(p).is_dir():
            folders.append(p)
        elif Path(p).is_file():
            files.append(p)
    from .io import iter_images as _ii
    for f in folders:
        files.extend(str(x) for x in _ii(f, recursive=bool(payload.get("recursive", False))))
    files = unique_paths(files)
    if not files:
        raise ValueError("所选路径里没有可识别的照片")
    batch = "local-" + uuid.uuid4().hex[:8]
    root = UPLOAD_ROOTS[0] if (UPLOAD_ROOTS[0].exists() or _mkdir_ok(UPLOAD_ROOTS[0])) \
        else next((r for r in UPLOAD_ROOTS[1:] if _mkdir_ok(r)), None)
    manifest = root / batch
    manifest.mkdir(parents=True, exist_ok=True)
    (manifest / "paths.txt").write_text("\n".join(files), encoding="utf-8")

    # 导入优先：服务端一步启动扫描（前端只拿 job 轮询，无二次调用可断）
    scan_dirs = folders or sorted({str(Path(f).parent) for f in files})[:3]
    scan_payload = {"jobs": 4,
                    "faces": bool(payload.get("faces", True)),
                    "ai": bool(payload.get("ai", True)),
                    "recursive": bool(payload.get("recursive", False))}
    # 始终使用解析后的文件清单：多文件夹选择不能只把第一个目录交给扫描器，
    # 也避免扫描用户未选择、但后来出现在目录里的新文件。
    scan_payload["folder"] = "."
    scan_payload["paths"] = files
    job = _scan_start(scan_payload)
    print(f"[import] 批次 {batch}：{len(files)} 张，扫描任务 {job['job']} 已启动")
    return {"batch": batch, "count": len(files), "folders": folders,
            "files": files, "file_mode": not folders, "job": job["job"],
            "scan_dirs": scan_dirs}


def _mkdir_ok(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        return True
    except OSError:
        return False


def _upload(handler, body: bytes):
    """拖拽/文件选择导入：原始字节 + 文件名头，落盘到批次目录。"""
    raw_name = urllib.parse.unquote(handler.headers.get("X-Filename", "photo.jpg"))
    # webkitdirectory / 拖拽目录会传相对路径。只接受相对路径片段，避免
    # ../ 穿越，同时保留子目录中的同名照片。
    parts = [part for part in raw_name.replace("\\", "/").split("/")
             if part not in {"", ".", ".."}]
    if not parts:
        parts = ["photo.jpg"]
    batch = "".join(ch for ch in handler.headers.get("X-Batch", "batch")
                    if ch.isalnum() or ch in "-_")[:40] or "batch"
    folder = _import_dirs.get(batch)
    if folder is None:
        for root in UPLOAD_ROOTS:
            try:
                cand = root / batch
                cand.mkdir(parents=True, exist_ok=True)
                folder = cand
                break
            except OSError:
                continue
        if folder is None:
            raise ValueError("无法创建导入目录")
        _import_dirs[batch] = folder
    dst = folder.joinpath(*parts)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(body)
    handler._json({"path": str(dst), "folder": str(folder)})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 安静模式
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, data: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/":
                placeholder = (f"已载入 {INIT_FOLDER}，正在自动扫描…" if INIT_FOLDER
                               else "输入路径，或把文件/文件夹拖进窗口")
                rendered_html = (INDEX_HTML
                        .replace("__INIT_FOLDER_JSON__", _script_json(INIT_FOLDER or ""))
                        .replace("__INIT_PLACEHOLDER__",
                                 html.escape(placeholder, quote=True)))
                self._file(rendered_html.encode(), "text/html; charset=utf-8")
            elif u.path == "/api/thumb":
                self._file(_thumb(q["p"][0], int(q.get("s", ["360"])[0])), "image/jpeg")
            elif u.path == "/api/img":
                p = Path(q["p"][0])
                ext = p.suffix.lower()
                if ext in {".jpg", ".jpeg"}:
                    self._file(p.read_bytes(), "image/jpeg")
                elif ext == ".png":
                    self._file(p.read_bytes(), "image/png")
                else:                # RAW 等：浏览器 img 显示不了原始字节，渲染成 JPEG
                    self._file(_thumb(q["p"][0], 2200), "image/jpeg")
            elif u.path == "/api/raw_img":
                # 快审灯箱大图：复用缩略图管线（磁盘缓存 + 内存 LRU），上限 2200px
                size = min(2200, max(360, int(q.get("s", ["2200"])[0])))
                self._file(_thumb(q["p"][0], size), "image/jpeg")
            elif u.path == "/api/presets":
                self._json({"presets": [{"name": name, **PRESET_META.get(name, {})}
                                         for name in PRESETS]})
            elif u.path == "/api/scan_status":
                self._json(_scan_status(q))
            else:
                self._json({"error": "not found"}, 404)
        except FileNotFoundError as e:
            self._json({"error": f"文件不存在：{e}"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 400)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n) if n else b""
        try:
            if self.path == "/api/upload":
                _upload(self, body)
            elif self.path == "/api/scan":
                payload = json.loads(body or b"{}")
                result = _scan_start(payload)
                self._json(result)
            elif self.path == "/api/scan_cancel":
                self._json(_scan_cancel(json.loads(body or b"{}")))
            elif self.path == "/api/import_paths":
                self._json(_import_paths(json.loads(body or b"{}")))
            elif self.path == "/api/rate":
                self._json(_rate(json.loads(body or b"{}")))
            elif self.path == "/api/prepare":
                self._json(_prepare(json.loads(body or b"{}")))
            elif self.path == "/api/process_one":
                self._json(_process_one(json.loads(body or b"{}")))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 400)


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PhotoPilot — 筛选 / 追色 / 美化</title>
<style>
@property --p{syntax:'<number>';inherits:false;initial-value:0}
:root{
  --bg:#f5f5f7; --fg:#1d1d1f; --dim:#7c7c82;
  --panel:rgba(255,255,255,.66); --panel-solid:#ffffff;
  --line:rgba(0,0,0,.08); --track:rgba(120,120,128,.22);
  --acc:#0071e3; --acc2:#42a5ff;
  --good:#34c759; --warn:#ff9f0a; --bad:#ff3b30;
  --chip-r:rgba(255,59,48,.12); --chip-w:rgba(255,159,10,.14); --chip-g:rgba(52,199,89,.13);
  --shadow:0 2px 8px rgba(0,0,0,.06),0 12px 32px rgba(0,0,0,.10);
  --shadow-lg:0 4px 12px rgba(0,0,0,.08),0 24px 64px rgba(0,0,0,.16);
  --ease-interactive:cubic-bezier(.22,1,.36,1); --ease:cubic-bezier(.25,.9,.3,1);
}
@media(prefers-color-scheme:dark){:root{
  --bg:#101014; --fg:#f5f5f7; --dim:#98989f;
  --panel:rgba(38,38,42,.62); --panel-solid:#232327;
  --line:rgba(255,255,255,.10); --track:rgba(120,120,128,.34);
  --acc:#0a84ff; --acc2:#5eb3ff;
  --chip-r:rgba(255,69,58,.18); --chip-w:rgba(255,159,10,.16); --chip-g:rgba(48,209,88,.16);
  --shadow:0 2px 8px rgba(0,0,0,.35),0 12px 32px rgba(0,0,0,.4);
  --shadow-lg:0 4px 12px rgba(0,0,0,.4),0 24px 64px rgba(0,0,0,.55);
}}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--fg);overflow:hidden;
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Helvetica Neue",sans-serif;
  -webkit-font-smoothing:antialiased}
/* 动态壁纸光斑 */
body::before,body::after{content:'';position:fixed;width:52vw;height:52vw;border-radius:50%;
  filter:blur(90px);opacity:.5;z-index:0;pointer-events:none}
body::before{background:radial-gradient(circle,#9ec9ff,transparent 65%);top:-18vw;left:-10vw;animation:drift 26s ease-in-out infinite alternate}
body::after{background:radial-gradient(circle,#ffd4e0,#ffc9a3 55%,transparent 70%);bottom:-20vw;right:-12vw;animation:drift 32s ease-in-out infinite alternate-reverse}
@media(prefers-color-scheme:dark){body::before,body::after{opacity:.3}}
@keyframes drift{from{transform:translate(0,0) scale(1)}to{transform:translate(6vw,4vh) scale(1.15)}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}

/* ── 顶栏 ── */
header{position:sticky;top:0;z-index:20;display:flex;gap:12px;align-items:center;
  padding:12px 20px;background:var(--panel);backdrop-filter:saturate(180%) blur(24px);
  -webkit-backdrop-filter:saturate(180%) blur(24px);border-bottom:1px solid var(--line)}
.brandMark{width:28px;height:28px;display:grid;place-items:center;flex:none;border-radius:8px;
  background:linear-gradient(145deg,#0a84ff,#5e5ce6);color:#fff;font-size:11px;font-weight:800;
  letter-spacing:-.04em;box-shadow:0 3px 10px rgba(10,132,255,.24)}
header h1{font-size:17px;font-weight:700;letter-spacing:-.02em;margin:0 10px 0 0}
header h1 span{color:var(--acc)}
#folder{flex:1;max-width:540px;padding:8px 14px;border-radius:11px;border:1px solid var(--line);
  background:var(--panel-solid);color:var(--fg);outline:none;font-size:13px;
  transition:box-shadow .25s var(--ease),border-color .25s}
#folder:focus{border-color:var(--acc2);box-shadow:0 0 0 4px rgba(0,113,227,.18)}
.btn{padding:8px 14px;border-radius:10px;border:none;cursor:pointer;font-weight:600;font-size:13px;
  white-space:nowrap;
  background:linear-gradient(180deg,var(--acc2),var(--acc));color:#fff;
  box-shadow:0 1px 2px rgba(0,60,160,.25);transition:transform .18s var(--ease-interactive),box-shadow .25s,filter .2s}
.btn:hover{transform:translateY(-1px);box-shadow:0 4px 14px rgba(0,113,227,.4);filter:brightness(1.05)}
.btn:active{transform:scale(.96)}
.btn:disabled{opacity:.45;cursor:default;transform:none;box-shadow:none}
.btn.ghost{background:var(--panel-solid);color:var(--fg);border:1px solid var(--line);box-shadow:none}
.btn.ghost:hover{box-shadow:var(--shadow)}
#scanInfo{color:var(--dim);font-size:13px}
.grow{flex:1}

/* ── 主区 ── */
main{display:flex;height:calc(100vh - 57px);position:relative;z-index:1}
#center{flex:1;display:flex;flex-direction:column;min-width:0}
#toolbar{display:flex;flex-direction:column;gap:9px;padding:12px 20px 10px;border-bottom:1px solid var(--line);
  background:rgba(255,255,255,.16)}
.toolbarRow{display:flex;align-items:center;gap:10px;min-width:0;flex-wrap:wrap}
.toolbarRow.primary{min-height:34px}
.toolbarRow.secondary{padding-top:1px}
#toolbar .tip{display:flex;align-items:center;gap:9px;flex:1 1 260px;min-width:220px;color:var(--dim);font-size:12px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.toolbarKicker{color:var(--fg);font-weight:700;font-size:12px}
.toolGroup{display:flex;align-items:center;gap:8px;min-width:0;padding-left:12px;border-left:1px solid var(--line)}
.toolGroupLabel{color:var(--dim);font-size:11px;font-weight:700;letter-spacing:.04em;white-space:nowrap}
#toolbar .toolSelect{display:flex;align-items:center;gap:5px;color:var(--dim);font-size:11px;white-space:nowrap;flex:none}
#toolbar .toolSelect select{width:auto;min-width:78px;padding:6px 8px;font-size:12px;border-radius:8px}
#toolbar .toolbarActions{display:flex;align-items:center;gap:8px;margin-left:auto;flex:none}
#toolbar .toolbarActions .btn{padding-left:12px;padding-right:12px}
#viewbar{display:flex;align-items:center;gap:2px;padding:2px;border:1px solid var(--line);
  border-radius:9px;background:var(--track);flex:none}
#viewbar button{border:0;border-radius:7px;background:transparent;color:var(--dim);cursor:pointer;
  padding:5px 9px;font-size:12px;font-weight:650;line-height:1;transition:background .2s,color .2s,box-shadow .2s}
#viewbar button:hover{color:var(--fg)}
#viewbar button.on{background:var(--panel-solid);color:var(--fg);box-shadow:0 1px 4px rgba(0,0,0,.14)}
#scanInfo{white-space:nowrap;font-variant-numeric:tabular-nums}
.chk.slim{margin:0;white-space:nowrap;flex:none}
.chk.slim .sw{width:36px;height:21px;border-radius:11px}
.chk.slim .sw::after{width:17px;height:17px}
.chk.slim input:checked+.sw::after{transform:translateX(15px)}
#scanbar{display:none;align-items:center;gap:12px;padding:10px 20px 0}
#scanbar.show{display:flex}
#scantrack{flex:1;height:4px;border-radius:2px;background:var(--track);overflow:hidden}
#scanfill{display:block;height:100%;width:0;border-radius:2px;
  background:linear-gradient(90deg,var(--acc2),var(--acc));transition:opacity .2s var(--ease)}
#scantext{color:var(--dim);font-size:12px;white-space:nowrap;font-variant-numeric:tabular-nums}
#grid{--tile-min:230px;--thumb-min:160px;flex:1;overflow-y:auto;padding:14px 20px 28px;
  display:grid;grid-template-columns:repeat(auto-fill,minmax(var(--tile-min),1fr));
  grid-auto-rows:minmax(calc(var(--thumb-min) + 76px),auto);gap:18px;align-content:start;align-items:start}
#grid[data-density="compact"]{--tile-min:190px;--thumb-min:140px;gap:14px}
#grid[data-density="large"]{--tile-min:300px;--thumb-min:208px;gap:20px}
.card{position:relative;background:var(--panel-solid);border-radius:18px;overflow:hidden;cursor:pointer;
  border:2.5px solid transparent;box-shadow:var(--shadow);
  display:block;width:100%;height:calc(var(--thumb-min) + 76px);min-width:0;min-height:calc(var(--thumb-min) + 76px);
  transition:transform .3s var(--ease),box-shadow .3s,border-color .25s;
  animation:cardIn .6s var(--ease) both}
@keyframes cardIn{from{opacity:0;transform:translateY(18px) scale(.96)}to{opacity:1;transform:none}}
.card:hover{transform:translateY(-4px) scale(1.012);box-shadow:var(--shadow-lg)}
.card.sel{border-color:var(--acc)}
.th{position:relative;width:100%;height:var(--thumb-min);min-height:var(--thumb-min);aspect-ratio:3/2;
  flex:none;background:var(--track);overflow:hidden;contain:layout paint}
.th::before{content:'';position:absolute;inset:0;
  background:linear-gradient(100deg,transparent 30%,rgba(255,255,255,.55) 50%,transparent 70%);
  background-size:220% 100%;animation:shimmer 1.4s linear infinite}
.th.ld::before{opacity:0;transition:opacity .4s}
@keyframes shimmer{from{background-position:180% 0}to{background-position:-80% 0}}
.th img{width:100%;height:100%;object-fit:cover;display:block;opacity:0;transform:scale(1.06);
  transition:opacity .5s ease,transform .6s var(--ease)}
.th.ld img{opacity:1;transform:none}
.badges{position:absolute;left:8px;bottom:8px;display:flex;gap:6px;align-items:center}
.pill{padding:3px 8px;border-radius:7px;font-size:11.5px;font-weight:600;color:var(--fg);
  background:rgba(255,255,255,.75);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  box-shadow:0 1px 4px rgba(0,0,0,.12)}
.pill.face{color:#0a5dc2}
.ck{position:absolute;top:9px;right:9px;width:24px;height:24px;border-radius:50%;
  background:var(--acc);color:#fff;display:grid;place-items:center;font-size:13px;font-weight:700;
  box-shadow:0 2px 8px rgba(0,113,227,.5);transform:scale(0);transition:transform .35s var(--ease-interactive)}
.card.sel .ck{transform:scale(1)}
.targetBtn{position:absolute;top:9px;left:9px;z-index:2;border:1px solid rgba(255,255,255,.72);
  border-radius:99px;padding:4px 8px;background:rgba(20,24,32,.62);color:#fff;font-size:10.5px;
  font-weight:650;cursor:pointer;backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
  transition:background .2s,color .2s,border-color .2s}
.targetBtn:hover{background:var(--acc);border-color:var(--acc)}
.targetBtn.on{background:#ffd60a;border-color:#ffd60a;color:#3b2b00}
.card.target{box-shadow:0 0 0 3px rgba(255,214,10,.38),var(--shadow-lg)}
.meta{padding:9px 12px 11px;display:flex;gap:10px;align-items:center;height:61px;min-height:61px}
.ring{--p:0;--ring-c:var(--acc);width:40px;height:40px;border-radius:50%;flex:none;position:relative;
  background:conic-gradient(var(--ring-c) calc(var(--p)*1%),var(--track) 0);
  transition:--p 1s var(--ease);display:grid;place-items:center}
.ring::before{content:'';position:absolute;inset:3.5px;border-radius:50%;background:var(--panel-solid)}
.ring b{position:relative;font-size:11px;font-weight:700;letter-spacing:-.02em}
.flags{display:flex;gap:4px;flex-wrap:wrap;min-width:0}
.flag{font-size:10.5px;padding:2px 8px;border-radius:99px;background:var(--track);color:var(--dim);font-weight:600}
.flag.bad{background:var(--chip-r);color:var(--bad)}
.flag.warn{background:var(--chip-w);color:var(--warn)}
.flag.good{background:var(--chip-g);color:var(--good)}
.name{font-size:11px;color:var(--dim);margin-top:5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#empty{margin:auto;text-align:center;color:var(--dim);padding:40px}
#empty .orb{width:84px;height:84px;margin:0 auto 18px;border-radius:50%;
  background:conic-gradient(from 20deg,#6fb3ff,#c9a2ff,#ffb0c8,#ffd9a0,#6fb3ff);
  filter:blur(1px);animation:float 4s ease-in-out infinite;box-shadow:var(--shadow-lg)}
@keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-10px)}}
#empty h2{color:var(--fg);font-size:19px;letter-spacing:-.02em;margin:0 0 6px}
#drop{position:fixed;inset:12px;z-index:70;border:3px dashed var(--acc);border-radius:28px;
  background:rgba(0,113,227,.1);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);
  display:none;place-items:center;pointer-events:none}
#drop.show{display:grid;animation:fadeIn .2s ease}
#drop .inner{text-align:center}
#drop .big{font-size:22px;font-weight:700;color:var(--fg);letter-spacing:-.02em}
#drop .sm{color:var(--dim);margin-top:6px}

/* ── 侧栏 ── */
#side{width:308px;flex:none;margin:14px 14px 14px 0;border-radius:20px;padding:18px;
  background:var(--panel);backdrop-filter:saturate(180%) blur(28px);
  -webkit-backdrop-filter:saturate(180%) blur(28px);
  border:1px solid var(--line);box-shadow:var(--shadow);overflow-y:auto}
#side h2{font-size:15px;font-weight:700;letter-spacing:-.01em;margin:0 0 14px}
.sect{font-size:11px;font-weight:700;color:var(--dim);text-transform:uppercase;letter-spacing:.06em;margin:16px 0 8px}
.row{margin-bottom:13px}
.row label{display:block;color:var(--dim);font-size:12px;margin-bottom:5px}
.val{float:right;color:var(--fg);font-variant-numeric:tabular-nums;font-weight:600}
select{width:100%;padding:8px 10px;border-radius:10px;border:1px solid var(--line);
  background:var(--panel-solid);color:var(--fg);outline:none;font-size:13px}
/* 分段控件 */
.seg{position:relative;display:grid;grid-auto-flow:column;grid-auto-columns:1fr;
  background:var(--track);border-radius:10px;padding:2.5px;user-select:none}
.seg .thumb{position:absolute;top:2.5px;bottom:2.5px;left:2.5px;width:calc((100% - 5px)/var(--segn,4));
  background:var(--panel-solid);border-radius:8.5px;box-shadow:0 1px 5px rgba(0,0,0,.14);
  transition:transform .32s var(--ease-interactive)}
.seg button{position:relative;z-index:1;border:none;background:none;color:var(--dim);
  padding:6px 0;font-size:12.5px;font-weight:600;cursor:pointer;border-radius:8px;transition:color .2s}
.seg button.on{color:var(--fg)}
/* 滑杆 */
input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:4px;border-radius:2px;
  background:linear-gradient(to right,var(--acc) var(--val,50%),var(--track) var(--val,50%));outline:none}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:19px;height:19px;border-radius:50%;
  background:#fff;box-shadow:0 1px 5px rgba(0,0,0,.3),0 3px 9px rgba(0,0,0,.14);cursor:grab;
  transition:transform .18s var(--ease-interactive)}
input[type=range]::-webkit-slider-thumb:active{cursor:grabbing;transform:scale(1.18)}
/* 开关 */
.chk{display:flex;gap:10px;align-items:center;margin:9px 0;cursor:pointer;color:var(--fg);font-size:13px}
.chk input{display:none}
.sw{width:40px;height:24px;border-radius:12px;background:var(--track);position:relative;flex:none;
  transition:background .25s}
.sw::after{content:'';position:absolute;top:2px;left:2px;width:20px;height:20px;border-radius:50%;
  background:#fff;box-shadow:0 1px 4px rgba(0,0,0,.25);transition:transform .3s var(--ease-interactive)}
.chk input:checked+.sw{background:var(--good)}
.chk input:checked+.sw::after{transform:translateX(16px)}
.autoToneRow{margin:10px 0 4px;font-weight:650}
.subhint{color:var(--dim);font-size:11px;line-height:1.4;margin:-1px 0 10px 50px}
#actionGroup{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:14px}
#actionGroup .btn{width:100%;margin:0}
#actionHint{color:var(--dim);font-size:11.5px;line-height:1.45;margin-top:8px;text-align:center}
#targetPanel{display:flex;gap:8px;align-items:center;margin-bottom:12px}
#targetInfo{flex:1;min-width:0;color:var(--dim);font-size:12px;line-height:1.35}
#targetInfo.on{color:var(--fg);font-weight:600}
#clearTarget{flex:none;padding:6px 9px;font-size:11px}
#status{margin-top:10px;color:var(--dim);font-size:12px;min-height:16px;text-align:center}

/* ── 结果浮层 ── */
#results{position:fixed;inset:0;z-index:40;background:rgba(20,20,24,.42);
  backdrop-filter:blur(28px) saturate(150%);-webkit-backdrop-filter:blur(28px) saturate(150%);
  display:none;overflow-y:auto;padding:34px}
#results.show{display:block;animation:fadeIn .3s ease}
@keyframes fadeIn{from{opacity:0}}
#resInner{max-width:1080px;margin:0 auto;padding-bottom:40px}
#resInner h2{color:#fff;font-size:20px;font-weight:700;letter-spacing:-.02em;text-align:center;margin:4px 0 22px}
#results.single-result{padding:20px}
#results.single-result #resInner{width:min(1600px,calc(100vw - 40px));max-width:none;height:calc(100vh - 40px);display:flex;flex-direction:column}
#results.single-result #resInner h2{display:flex;align-items:center;justify-content:center;gap:16px;flex:none;margin:0 0 12px}
#results.single-result #resInner h2 .resultClose{margin-left:auto;flex:none}
.pair{background:var(--panel-solid);border-radius:20px;padding:12px;margin-bottom:16px;
  box-shadow:var(--shadow-lg);animation:cardIn .55s var(--ease) both}
#results.single-result .pair{display:flex;flex:1;min-height:0;flex-direction:column;margin:0;padding:10px}
.cmp{--pos:50%;position:relative;border-radius:14px;overflow:hidden;user-select:none;touch-action:none;cursor:ew-resize}
.cmp img{width:100%;display:block;pointer-events:none}
#results.single-result .cmp{display:flex;flex:1;min-height:0;align-items:center;justify-content:center;background:#101216}
#results.single-result .cmp img{width:100%;height:100%;object-fit:contain}
#results.single-result .cmp img.b{width:100%;height:100%;object-fit:contain}
#results.single-result .pair>.name{flex:none}
.cmp img.b{position:absolute;inset:0;height:100%;object-fit:cover;
  clip-path:inset(0 calc(100% - var(--pos)) 0 0)}
.cmp .bar{position:absolute;top:0;bottom:0;left:var(--pos);width:2.5px;background:#fff;
  box-shadow:0 0 10px rgba(0,0,0,.5);transform:translateX(-50%)}
.cmp .knob{position:absolute;top:50%;left:var(--pos);transform:translate(-50%,-50%);
  width:30px;height:30px;border-radius:50%;background:#fff;box-shadow:0 2px 10px rgba(0,0,0,.4);
  display:grid;place-items:center;color:#333;font-size:12px;font-weight:800;letter-spacing:-2px}
.cmp .tag{position:absolute;top:10px;font-size:11px;font-weight:700;padding:4px 11px;border-radius:99px;
  background:rgba(255,255,255,.8);backdrop-filter:blur(10px);color:#1d1d1f}
.cmp .tag.l{left:10px}.cmp .tag.r{right:10px}

/* ── 进度弹窗 ── */
#prog{position:fixed;inset:0;z-index:50;display:none;place-items:center;
  background:rgba(20,20,24,.35);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px)}
#prog.show{display:grid;animation:fadeIn .25s ease}
.pcard{background:var(--panel-solid);border-radius:22px;padding:30px 40px;text-align:center;
  box-shadow:var(--shadow-lg);animation:pop .45s var(--ease-interactive)}
@keyframes pop{from{opacity:0;transform:scale(.85)}to{opacity:1;transform:none}}
.spin{width:34px;height:34px;margin:0 auto 14px;position:relative}
.spin i{position:absolute;inset:0;animation:blade 1s linear infinite}
.spin i::after{content:'';display:block;margin:2px auto;width:5px;height:11px;border-radius:3px;background:var(--acc)}
.spin i:nth-child(1){animation-delay:-1s}.spin i:nth-child(2){animation-delay:-.875s;transform:rotate(45deg)}
.spin i:nth-child(3){animation-delay:-.75s;transform:rotate(90deg)}.spin i:nth-child(4){animation-delay:-.625s;transform:rotate(135deg)}
.spin i:nth-child(5){animation-delay:-.5s;transform:rotate(180deg)}.spin i:nth-child(6){animation-delay:-.375s;transform:rotate(225deg)}
.spin i:nth-child(7){animation-delay:-.25s;transform:rotate(270deg)}.spin i:nth-child(8){animation-delay:-.125s;transform:rotate(315deg)}
@keyframes blade{0%{opacity:1}100%{opacity:.15}}
.pcard h3{margin:0 0 4px;font-size:15px}
.pcard .d{color:var(--dim);font-size:12.5px;margin-bottom:14px}
.pbar{width:240px;height:5px;border-radius:3px;background:var(--track);overflow:hidden}
.pbar i{display:block;height:100%;width:0;border-radius:3px;
  background:linear-gradient(90deg,var(--acc2),var(--acc));transition:opacity .2s var(--ease)}
.pnum{margin-top:8px;font-size:12px;color:var(--dim);font-variant-numeric:tabular-nums}

/* ── 快审灯箱 ── */
#lb{position:fixed;inset:0;z-index:55;display:none;flex-direction:column;
  background:rgba(12,12,16,.92);backdrop-filter:blur(30px) saturate(140%);
  -webkit-backdrop-filter:blur(30px) saturate(140%)}
#lb.show{display:flex;animation:fadeIn .22s ease}
#lbTop{display:flex;flex:none;gap:12px;align-items:center;padding:14px 22px 10px}
#lbName{color:#fff;font-weight:600;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#lbIdx{color:var(--dim);font-size:12px;font-variant-numeric:tabular-nums}
#lbClose{margin-left:auto}
#lbStage{flex:1;position:relative;min-width:0;min-height:0;display:flex;align-items:center;justify-content:center;
  overflow:hidden;padding:12px 78px}
#lbImg{display:block;width:auto;height:auto;max-width:100%;max-height:100%;object-fit:contain;border-radius:12px;box-shadow:0 12px 60px rgba(0,0,0,.55);
  opacity:0;transition:opacity .3s ease;user-select:none;-webkit-user-drag:none}
#lbImg.ld{opacity:1}
#lbSpin{position:absolute;color:var(--dim);font-size:13px}
.lbNav{position:absolute;top:50%;transform:translateY(-50%);width:44px;height:44px;border-radius:50%;
  border:none;cursor:pointer;background:rgba(255,255,255,.14);color:#fff;font-size:19px;line-height:1;
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);transition:background .2s,transform .25s var(--ease-interactive)}
.lbNav:hover{background:rgba(255,255,255,.3);transform:translateY(-50%) scale(1.08)}
#lbPrev{left:20px}#lbNext{right:20px}
#lbBar{display:flex;flex:none;gap:18px;align-items:center;justify-content:center;padding:12px 22px 20px;flex-wrap:wrap}
#lbStars{display:flex;gap:6px;align-items:center}
#lbStars button{border:none;background:none;font-size:25px;cursor:pointer;line-height:1;padding:2px 4px;
  color:rgba(255,255,255,.26);transition:color .15s,transform .25s var(--ease-interactive)}
#lbStars button.on{color:#ffd60a}
#lbStars button:hover{transform:scale(1.25)}
#lbStars .clr{font-size:15px;padding:3px 10px;border-radius:99px;border:1px solid rgba(255,255,255,.2)}
#lbInfo{color:var(--dim);font-size:12.5px;font-variant-numeric:tabular-nums;display:flex;gap:14px;flex-wrap:wrap;justify-content:center}
#lbInfo b{color:#fff}
.lbKey{display:inline-block;padding:1px 7px;border-radius:6px;background:rgba(255,255,255,.14);
  color:#fff;font-size:11px;font-weight:600;margin:0 2px}
.card.lbcur{border-color:#ffd60a;box-shadow:0 0 0 4px rgba(255,214,10,.22),var(--shadow-lg)}
.pill.rstar{color:#9a6b00}
.pill.ai{color:#6941c6}
.starBtn,.compareBtn{position:absolute;z-index:3;border:1px solid rgba(255,255,255,.72);cursor:pointer;
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);font-weight:700;transition:transform .18s var(--ease-interactive),background .2s}
.starBtn{top:9px;right:9px;width:27px;height:27px;border-radius:50%;background:rgba(20,24,32,.62);color:rgba(255,255,255,.85);font-size:16px;line-height:1}
.starBtn.on{background:#ffd60a;border-color:#ffd60a;color:#3b2b00}
.starBtn:hover,.compareBtn:hover{transform:scale(1.08);background:var(--acc);color:#fff;border-color:var(--acc)}
.compareBtn{right:8px;bottom:8px;border-radius:99px;padding:3px 8px;background:rgba(20,24,32,.68);color:#fff;font-size:10.5px}
.groupBadge{background:rgba(0,113,227,.12);color:var(--acc)}
.card{cursor:pointer}
.card.dbl{position:relative}

/* ── 同组对比 ── */
#compareModal{position:fixed;inset:0;z-index:58;display:none;flex-direction:column;
  background:rgba(12,12,16,.95);backdrop-filter:blur(26px) saturate(140%);-webkit-backdrop-filter:blur(26px)}
#compareModal.show{display:flex;animation:fadeIn .22s ease}
#compareTop{display:flex;align-items:center;gap:12px;padding:14px 22px 10px;color:#fff}
#compareTitle{font-size:15px;font-weight:700}
#compareHint{color:var(--dim);font-size:12px}
#compareClose{margin-left:auto}
#compareControls{display:flex;align-items:center;gap:10px;padding:0 22px 12px;color:var(--dim);font-size:12px}
#compareZoom{width:180px}
#compareZoomValue{font-variant-numeric:tabular-nums;color:#fff;min-width:30px}
#compareGrid{flex:1;min-height:0;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;padding:0 22px 22px;align-items:stretch}
.comparePane{position:relative;min-width:0;min-height:180px;border-radius:14px;overflow:hidden;background:#1c1c22;display:flex;align-items:center;justify-content:center}
.comparePane img{max-width:100%;max-height:100%;object-fit:contain;transform:scale(var(--zoom,1));transform-origin:center;transition:transform .15s ease;user-select:none}
.comparePane .compareMeta{position:absolute;left:8px;right:8px;bottom:8px;display:flex;align-items:center;gap:6px;color:#fff;font-size:11px;text-shadow:0 1px 3px #000}
.comparePane .compareMeta span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.comparePane .compareStar{border:0;background:rgba(0,0,0,.5);border-radius:99px;color:#fff;padding:3px 7px;cursor:pointer}
.comparePane .compareStar.on{color:#ffd60a}
@media(max-width:900px){#compareGrid{grid-template-columns:repeat(2,minmax(0,1fr));overflow-y:auto}.comparePane{min-height:220px}}
@media(max-width:560px){#toolbar .toolSelect{display:none}#compareGrid{grid-template-columns:1fr}.comparePane{min-height:260px}}

/* ── toast ── */
#toast{position:fixed;left:50%;bottom:26px;transform:translate(-50%,80px);z-index:60;
  padding:10px 20px;border-radius:99px;background:var(--panel-solid);color:var(--fg);
  box-shadow:var(--shadow-lg);font-size:13px;font-weight:600;opacity:0;pointer-events:none;
  transition:transform .4s var(--ease-interactive),opacity .3s}
#toast.show{transform:translate(-50%,0);opacity:1}
</style></head><body>
<header>
  <div class="brandMark" aria-hidden="true">PP</div>
  <h1>Photo<span>Pilot</span></h1>
  <input id="folder" type="text" placeholder="__INIT_PLACEHOLDER__">
  <button class="btn ghost" onclick="pickFolder()">文件夹…</button>
  <button class="btn ghost" onclick="pickPhotos()">照片…</button>
  <button class="btn" id="scanBtn" onclick="scan()">扫描打分</button>
  <span id="scanInfo"></span>
  <input type="file" id="pickDir" webkitdirectory multiple hidden
    onchange="uploadFiles(this.files);this.value=''">
  <input type="file" id="pickFiles" multiple hidden
    accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp,.arw,.cr2,.cr3,.nef,.nrw,.dng,.raf,.orf,.rw2,.pef,.srw,.x3f"
    onchange="uploadFiles(this.files);this.value=''">
</header>
<main>
<div id="center">
  <div id="toolbar">
    <div class="toolbarRow primary">
      <span class="tip" id="tip"><span class="toolbarKicker">审片</span>点击选择 · 双击进入快审 · ⌘A 全选</span>
      <div class="toolGroup" aria-label="排列和筛选">
        <span class="toolGroupLabel">排列</span>
        <label class="toolSelect"><span>排序</span><select id="sortMode" aria-label="排序方式">
          <option value="smart">智能分组</option><option value="score">综合分</option>
          <option value="name">文件名</option><option value="rating">星级</option>
        </select></label>
        <label class="toolSelect"><span>筛选</span><select id="filterMode" aria-label="照片筛选">
          <option value="all">全部</option><option value="recommended">推荐</option>
          <option value="groups">相似组</option><option value="starred">已加星</option>
          <option value="review">待复核</option>
        </select></label>
      </div>
      <button class="btn ghost" type="button" onclick="openSelectedCompare()" aria-label="比较 2–4 张">比较 2–4 张</button>
      <div id="viewbar" role="group" aria-label="缩略图大小">
        <button type="button" data-density="compact" title="显示更多缩略图">小</button>
        <button type="button" data-density="comfortable" class="on" title="平衡浏览与细节">中</button>
        <button type="button" data-density="large" title="放大缩略图">大</button>
      </div>
    </div>
    <div class="toolbarRow secondary">
      <div class="toolGroup" aria-label="分析选项">
        <span class="toolGroupLabel">分析</span>
        <label class="chk slim" title="跳过可大幅提速，适合先快速粗筛"><input type="checkbox" id="facesOpt" checked><span class="sw"></span>人脸分析</label>
        <label class="chk slim" title="NIMA 神经网络美学评分（AVA 训练，本地 CPU 推理约 90ms/张），融入综合分让 AI 帮你选片"><input type="checkbox" id="aiOpt" checked><span class="sw"></span>AI 美学</label>
        <label class="chk slim" title="扫描时递归进入子文件夹（婚礼/旅拍分日文件夹必备）"><input type="checkbox" id="recursiveOpt"><span class="sw"></span>含子文件夹</label>
      </div>
      <div class="toolbarActions">
        <button class="btn ghost" onclick="aiSmart()" title="保留：无硬伤 · 非连拍重复 · AI 美学或综合分达标">AI 智能筛选</button>
        <button class="btn ghost" onclick="selAll(1)">全选</button>
        <button class="btn ghost" onclick="selAll(0)">恢复推荐</button>
        <button class="btn ghost" onclick="selAll(-1)">全不选</button>
      </div>
    </div>
  </div>
  <div id="scanbar"><div id="scantrack"><i id="scanfill"></i></div>
    <span id="scantext"></span>
    <button class="btn ghost" id="cancelscan" onclick="cancelScan()" style="display:none">取消</button></div>
  <div id="grid" data-density="comfortable"></div>
  <div id="empty"><div class="orb"></div><h2>把照片拖进来，或选择文件夹</h2>
    <p>支持拖入整个文件夹（含子文件夹）· JPEG / PNG / RAW · 也可直接输入路径</p></div>
</div>
<div id="side"><h2>处理参数</h2>
  <div class="sect">白平衡</div>
  <div class="seg" id="wbSeg">
    <div class="thumb"></div>
    <button data-v="batch" class="on">整批统一</button>
    <button data-v="single">单张自动</button>
    <button data-v="off">关闭</button>
  </div>
  <div class="row" style="margin-top:10px" id="rowWbStrength" hidden><label>强度 <span class="val" id="vWbStr">0.95</span></label>
    <input type="range" id="wbStrength" min="0" max="1" step="0.05" value="0.95" oninput="slide(this,'vWbStr')"></div>
  <div class="sect">追色目标</div>
  <div id="targetPanel">
    <div id="targetInfo">请选择一张目标照片：点击卡片上的「设为目标」</div>
    <button class="btn ghost" id="clearTarget" type="button" onclick="clearTarget()" disabled>清除</button>
  </div>
  <div class="row"><label>算法</label><select id="algo">
    <option>oklab</option><option>reinhard</option><option>mkl</option><option>histogram</option><option>luma_hist</option></select></div>
  <div class="sect">追色</div>
  <div class="row"><label>强度 <span class="val" id="vStrength">0.90</span></label>
    <input type="range" id="strength" min="0" max="1" step="0.05" value="0.9" oninput="slide(this,'vStrength')"></div>
  <label class="chk"><input type="checkbox" id="preserveLuma"><span class="sw"></span>只调色不调亮度</label>
  <label class="chk"><input type="checkbox" id="skinProtect" checked><span class="sw"></span>肤色保护（检测到人脸时）</label>
  <div class="sect">风格预设</div>
  <div class="row"><label>原创算法预设</label><select id="presetSelect" aria-label="风格预设">
    <optgroup label="基础"><option value="natural">自然校正</option><option value="clean_cool">清爽冷调</option></optgroup>
    <optgroup label="人像 / 婚礼"><option value="portrait_soft">人像柔和</option><option value="wedding_air">婚礼通透</option><option value="wedding_airy">婚礼空气感</option><option value="wedding_blush">婚礼蜜桃</option><option value="skin_glow">肤色发光</option><option value="pastel_matte">粉彩哑光</option><option value="japanese_fresh">日系清新</option><option value="japanese_milk">日系奶油</option><option value="korean_cream">韩式奶油</option><option value="indoor_luminous">室内明亮</option></optgroup>
    <optgroup label="风光 / 街拍"><option value="travel_vibrant">旅行鲜明</option><option value="sunset_gold">落日金</option><option value="golden_hour">金色时刻</option><option value="golden_sunset">夕阳电影</option><option value="street_neon">街头霓虹</option><option value="forest_deep">森林深绿</option><option value="forest_story">森林故事</option><option value="ocean_air">海风蓝</option><option value="ocean_breeze">海边清蓝</option><option value="outdoor_clean">户外通透</option><option value="moody_teal">暗调青橙</option><option value="cinematic_night">电影夜色</option><option value="night_city">城市夜景</option></optgroup>
    <optgroup label="胶片 / 黑白"><option value="film_warm">温润胶片</option><option value="mono_contrast">黑白高反差</option><option value="retro_fade">复古褪色</option><option value="retro_album">旧相册胶片</option><option value="bw_soft">柔和黑白</option></optgroup>
  </select></div>
  <div class="sect">美化</div>
  <label class="chk autoToneRow"><input type="checkbox" id="autoTone" checked><span class="sw"></span><span>智能自动曝光</span></label>
  <div class="subhint">自动判断过曝/欠曝，压回高光并抬升暗部；原图不改动。</div>
  <div class="row"><label>磨皮 <span class="val" id="vSkin">0.60</span></label>
    <input type="range" id="skin" min="0" max="1" step="0.05" value="0.6" oninput="slide(this,'vSkin')"></div>
  <div class="row"><label>纹理保留 <span class="val" id="vRetain">0.40</span></label>
    <input type="range" id="retain" min="0" max="1" step="0.05" value="0.4" oninput="slide(this,'vRetain')"></div>
  <div class="row"><label>清晰度 <span class="val" id="vClarity">0.25</span></label>
    <input type="range" id="clarity" min="0" max="1" step="0.05" value="0.25" oninput="slide(this,'vClarity')"></div>
  <div class="row"><label>白平衡 <span class="val" id="vWb">0.50</span></label>
    <input type="range" id="wb" min="0" max="1" step="0.05" value="0.5" oninput="slide(this,'vWb')"></div>
  <div class="row"><label>人脸修复 <span class="val" id="vFaceRepair">0.30</span></label>
    <input type="range" id="faceRepair" min="0" max="1" step="0.05" value="0.3" oninput="slide(this,'vFaceRepair')" title="局部去噪并恢复面部微细节，不生成新五官"></div>
  <div class="row"><label>瑕疵修复 <span class="val" id="vBlemish">0.20</span></label>
    <input type="range" id="blemish" min="0" max="1" step="0.05" value="0.2" oninput="slide(this,'vBlemish')" title="仅抑制皮肤孤立明暗斑，保留毛孔纹理"></div>
  <div class="row"><label>局部区域</label><select id="localRegion" aria-label="局部修复区域">
    <option value="all">全图</option><option value="skin">皮肤</option><option value="face">人脸</option>
    <option value="eyes">眼睛</option><option value="background">背景</option>
  </select></div>
  <div id="actionGroup">
    <button class="btn" id="colorBtn" type="button" onclick="process('color')">追色</button>
    <button class="btn ghost" id="polishBtn" type="button" onclick="process('polish')">美化</button>
    <button class="btn ghost" id="presetBtn" type="button" onclick="process('preset')">套用预设</button>
  </div>
  <div id="actionHint">选中一张照片后点击美化或预设，会用大画布显示原图/成品；智能自动曝光可随时关闭。</div>
  <div id="status">就绪</div>
</div>
</main>
<div id="results" onclick="if(event.target===this)closeResults()">
  <div id="resInner"><h2 id="resTitle">处理结果 · 左右拖动对比</h2></div>
</div>
<div id="prog"><div class="pcard"><div class="spin"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>
  <h3>正在处理</h3><div class="d" id="progDesc"></div>
  <div class="pbar"><i id="pfill"></i></div><div class="pnum" id="pnum">0 / 0</div></div></div>
<div id="drop"><div class="inner"><div class="big">松开，导入照片</div>
  <div class="sm">支持整个文件夹（含子文件夹）· JPEG / PNG / RAW</div></div></div>
<div id="lb">
  <div id="lbTop"><span id="lbName"></span><span id="lbIdx"></span>
    <button class="btn ghost" id="lbClose" onclick="lbHide()">完成 Esc</button></div>
  <div id="lbStage"><div id="lbSpin">加载中…</div>
    <img id="lbImg" src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=" alt="照片预览">
    <button class="lbNav" id="lbPrev" onclick="lbNav(-1)">‹</button>
    <button class="lbNav" id="lbNext" onclick="lbNav(1)">›</button></div>
  <div id="lbBar">
    <div id="lbStars"></div>
    <div id="lbInfo"></div>
    <div style="color:var(--dim);font-size:11.5px">
      <span class="lbKey">←</span><span class="lbKey">→</span>切换
      <span class="lbKey">空格</span>保留/移出
      <span class="lbKey">0</span>–<span class="lbKey">5</span>星级（写 XMP）
      <span class="lbKey">Esc</span>关闭</div>
  </div>
</div>
<div id="compareModal" role="dialog" aria-modal="true" aria-labelledby="compareTitle">
  <div id="compareTop"><span id="compareTitle">相似照片对比</span><span id="compareHint">同组最佳帧在左 · 可直接加星</span>
    <button class="btn ghost" id="compareClose" onclick="closeGroupCompare()">完成 Esc</button></div>
  <div id="compareControls"><span>同步缩放</span><input id="compareZoom" type="range" min="1" max="4" step="0.1" value="1" aria-label="同步缩放">
    <span id="compareZoomValue">1.0×</span></div>
  <div id="compareGrid"></div>
</div>
<div id="toast"></div>

<script>
const BAD=['低分','闭眼'],WARN=['可能模糊','曝光异常','连拍重复','疑似眨眼','AI 低分'];
/* ── 扫描（后台任务 + 增量出图） ── */
let JOB=null, since=0, ranked=[], library=[], renderedN=0, ringQ=[], targetPath=null;
let selectedPaths=new Set(), selectionTouched=false;
// 视图重排时复用卡片和已加载的缩略图，避免筛选/相似组排序反复重载图片。
const cardCache=new Map();
const CHUNK=240;
// 每次启动扫描/导入都递增；旧请求返回后不得覆盖新任务。
let SCAN_REQ_GEN=0;
const $=id=>document.getElementById(id);
async function api(path,body){
  const r=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:undefined);
  const j=await r.json(); if(j.error)throw new Error(j.error); return j;}
const DENSITY_KEY='photopilot-density-v2';
function setDensity(mode,save=true){
  const value=['compact','comfortable','large'].includes(mode)?mode:'comfortable';
  $('grid').dataset.density=value;
  document.querySelectorAll('#viewbar button').forEach(b=>b.classList.toggle('on',b.dataset.density===value));
  if(save){try{localStorage.setItem(DENSITY_KEY,value);}catch(e){}}
}
document.querySelectorAll('#viewbar button').forEach(b=>b.onclick=()=>setDensity(b.dataset.density));
try{setDensity(localStorage.getItem(DENSITY_KEY)||'comfortable',false);}catch(e){setDensity('comfortable',false);}
async function pickFolder(){
  try{
    if(window.pywebview&&window.pywebview.api&&window.pywebview.api.pick_folder){
      const dir=await window.pywebview.api.pick_folder();
      if(dir){$('folder').value=dir;
        scan({recursive:$('recursiveOpt').checked});}
      return;}
  }catch(e){}
  $('pickDir').click();               // 浏览器环境：webkitdirectory 兜底
}
async function pickPhotos(){
  try{
    if(window.pywebview&&window.pywebview.api&&window.pywebview.api.pick_photos){
      const files=await window.pywebview.api.pick_photos();
      if(files&&files.length){await importLocalPaths(files);return;}
    }
  }catch(e){}
  $('pickFiles').click();             // 浏览器环境：走上传
}
function slide(el,out){$(out).textContent=(+el.value).toFixed(2);
  el.style.setProperty('--val',((el.value-el.min)/(el.max-el.min)*100)+'%');}
function toast(msg){const t=$('toast');t.textContent=msg;t.classList.add('show');
  clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove('show'),2600);}
const SORT_KEY='photopilot-sort-v1', FILTER_KEY='photopilot-filter-v1';
let sortMode='smart', filterMode='all';
function isGroup(p){return Number(p.group_size||1)>=2;}
function groupOrder(items){
  const best=new Map();
  for(const p of items){if(!isGroup(p))continue;const g=String(p.group);
    if(!best.has(g)||p.score>best.get(g).score)best.set(g,p);}
  return best;
}
function sortPhotos(items){
  const out=items.slice(), best=groupOrder(out);
  if(sortMode==='name')return out.sort((a,b)=>a.path.localeCompare(b.path));
  if(sortMode==='rating')return out.sort((a,b)=>(b.rating||0)-(a.rating||0)||b.score-a.score);
  if(sortMode==='score')return out.sort((a,b)=>b.score-a.score||a.path.localeCompare(b.path));
  return out.sort((a,b)=>{
    const ag=isGroup(a),bg=isGroup(b);
    if(ag!==bg)return ag?-1:1;
    if(ag&&a.group!==b.group){const d=best.get(String(b.group)).score-best.get(String(a.group)).score;
      if(Math.abs(d)>1e-6)return d;}
    if(ag&&a.group===b.group)return (a.group_rank||99)-(b.group_rank||99)||b.score-a.score;
    return b.score-a.score||a.path.localeCompare(b.path);
  });
}
function filterPhotos(items){
  if(filterMode==='recommended')return items.filter(autoSel);
  if(filterMode==='groups')return items.filter(isGroup);
  if(filterMode==='starred')return items.filter(p=>(p.rating||0)>0);
  if(filterMode==='review')return items.filter(p=>p.flags&&p.flags.length);
  return items;
}
function rebuildView(){
  ranked=sortPhotos(filterPhotos(library));
  const frag=document.createDocumentFragment();
  const end=Math.min(ranked.length,Math.max(CHUNK,renderedN));
  for(let i=0;i<end;i++)frag.appendChild(cardFor(ranked[i],i,false));
  $('grid').replaceChildren(frag);renderedN=end;
  flushRings();updateTargetUI();updateStats();
}
function rememberView(){try{localStorage.setItem(SORT_KEY,sortMode);localStorage.setItem(FILTER_KEY,filterMode);}catch(e){}}
function bindViewControls(){
  try{sortMode=localStorage.getItem(SORT_KEY)||'smart';filterMode=localStorage.getItem(FILTER_KEY)||'all';}catch(e){}
  if(!$('sortMode'))return;
  $('sortMode').value=sortMode;$('filterMode').value=filterMode;
  $('sortMode').onchange=e=>{sortMode=e.target.value;rememberView();rebuildView();};
  $('filterMode').onchange=e=>{filterMode=e.target.value;rememberView();rebuildView();};
}
function autoSel(p){return !p.flags.some(f=>BAD.includes(f)||f==='连拍重复')
  &&(p.ai==null||p.ai>=4.5);}
function updateStats(){
  const total=library.length;
  if(!total){$('scanInfo').textContent='';return;}
  const visible=ranked.length;
  const selected=ranked.reduce((count,p)=>count+(selectedPaths.has(p.path)?1:0),0);
  const recommended=library.filter(autoSel).length;
  $('scanInfo').textContent=`共 ${total} 张 · 当前 ${visible} · 已选 ${selected} · 推荐 ${recommended}`;
}
function updateTargetUI(){
  const info=$('targetInfo'),clear=$('clearTarget');
  if(targetPath){
    info.textContent='目标：'+targetPath.split('/').pop();
    info.classList.add('on');clear.disabled=false;
  }else{
    info.textContent='请选择一张目标照片：点击卡片上的「设为目标」';
    info.classList.remove('on');clear.disabled=true;
  }
  document.querySelectorAll('.card').forEach(c=>{
    const on=c.dataset.path===targetPath;
    c.classList.toggle('target',on);
    const b=c.querySelector('.targetBtn');
    if(b){b.classList.toggle('on',on);b.textContent=on?'目标照片':'设为目标';b.setAttribute('aria-pressed',on?'true':'false');}
  });
}
function setTarget(path){
  if(!path)return;
  targetPath=path;updateTargetUI();
  toast('已设为追色目标：'+path.split('/').pop());
}
function clearTarget(){
  targetPath=null;updateTargetUI();toast('已清除追色目标');
}
function setActionButtonsDisabled(disabled){
  ['colorBtn','polishBtn','presetBtn'].forEach(id=>{const b=$(id);if(b)b.disabled=disabled;});
}
function aiSmart(){
  const keep=ranked.filter(p=>!p.flags.some(f=>BAD.includes(f)||f==='连拍重复')
    &&(p.ai!=null&&p.ai>=5.2||p.ai==null&&p.score>=0.55));
  selectionTouched=true;selectedPaths=new Set(keep.map(p=>p.path));
  document.querySelectorAll('.card').forEach(c=>c.classList.toggle('sel',selectedPaths.has(c.dataset.path)));
  const n=keep.length;
  updateStats();
  toast(`AI 智能筛选：保留 ${n} / ${ranked.length} 张${keep.length?' · 快审微调即可':''}`);}
function fmtT(s){s=Math.max(0,Math.round(s));return Math.floor(s/60)+':'+String(s%60).padStart(2,'0');}

function flushRings(){
  requestAnimationFrame(()=>{
    for(const el of ringQ)el.style.setProperty('--p',el.dataset.p);
    ringQ=[];});}

function scoreRingColor(p){return p.score>=0.65?'var(--good)':p.score>=0.45?'var(--acc)':'var(--warn)';}
function escapeHtml(value){return String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));}
function flagsHtml(p){
  return p.flags.length?p.flags.map(f=>`<span class="flag ${BAD.includes(f)?'bad':WARN.includes(f)?'warn':''}">${escapeHtml(f)}</span>`).join('')
    :'<span class="flag good">通过</span>';
}
function updateCardInPlace(card,p){
  // 精扫会逐张回传 AI/人脸结果；只更新文本和评分环，不能重建 img 节点。
  // 重建会重新触发 cardIn/shimmer，批量相似组排序时看起来像整页闪烁。
  if(!card)return;
  const ring=card.querySelector('.ring');
  if(ring){
    ring.style.setProperty('--ring-c',scoreRingColor(p));
    ring.dataset.p=p.score*100;
    ring.style.setProperty('--p',p.score*100);
    const score=ring.querySelector('b');if(score)score.textContent=p.score.toFixed(2).slice(1);
  }
  const flags=card.querySelector('.flags');if(flags)flags.innerHTML=flagsHtml(p);
  const badges=card.querySelector('.badges');
  if(!badges)return;
  let face=badges.querySelector('.face');
  if(p.faces){
    if(!face){face=document.createElement('span');face.className='pill face';badges.appendChild(face);}
    face.textContent=`${p.faces} 脸${p.blinks?' · '+p.blinks+' 闭眼':''}`;
  }else if(face)face.remove();
  let ai=badges.querySelector('.ai');
  if(p.ai!=null){
    if(!ai){ai=document.createElement('span');ai.className='pill ai';badges.appendChild(ai);}
    ai.textContent=`AI ${p.ai.toFixed(1)}`;
  }else if(ai)ai.remove();
}

function cardFor(p,idx=0,animate=false){
  let card=cardCache.get(p.path);
  if(!card){
    card=makeCard(p,idx,animate);
    cardCache.set(p.path,card);
  }else{
    updateCardInPlace(card,p);
  }
  return card;
}

function makeCard(p,idx,animate=true){
  const ringC=scoreRingColor(p), flags=flagsHtml(p);
  const photoName=escapeHtml(p.path.split('/').pop()),thumbUrl=escapeHtml(p.thumb);
  const d=document.createElement('div');
  d.className='card'+(selectedPaths.has(p.path)?' sel':'');d.dataset.path=p.path;
  if(animate)d.style.animationDelay=Math.min((idx%24)*25,400)+'ms';
  else d.style.animation='none';
  const group=p.group_size>=2?`<span class="pill groupBadge">相似 ${p.group_rank}/${p.group_size}</span>`:'';
  const compare=p.group_best&&p.group_size>=2?`<button class="compareBtn" type="button" title="并排比较这组照片">比较 ${Math.min(4,p.group_size)} 张</button>`:'';
  d.innerHTML=`<div class="th"><img loading="lazy" src="${thumbUrl}" alt="${photoName}" onload="this.parentElement.classList.add('ld')"></div>
    <button class="targetBtn${targetPath===p.path?' on':''}" type="button" aria-pressed="${targetPath===p.path?'true':'false'}" title="将这张照片作为追色目标">${targetPath===p.path?'目标照片':'设为目标'}</button>
    <button class="starBtn${p.rating?' on':''}" type="button" aria-label="${p.rating?'取消星标':'加星'}" title="${p.rating?'取消星标':'加星'}">${p.rating?'★':'☆'}</button>
    ${compare}<div class="badges">${group}${p.faces?`<span class="pill face">${p.faces} 脸${p.blinks?' · '+p.blinks+' 闭眼':''}</span>`:''}${p.ai!=null?`<span class="pill ai">AI ${p.ai.toFixed(1)}</span>`:''}</div>
    <div class="ck">✓</div>
    <div class="meta"><div class="ring" style="--ring-c:${ringC};--p:0" data-p="${p.score*100}"><b>${p.score.toFixed(2).slice(1)}</b></div>
    <div style="min-width:0"><div class="flags">${flags}</div><div class="name">${photoName}</div></div></div>`;
  d.onclick=()=>{selectionTouched=true;d.classList.toggle('sel');
    if(d.classList.contains('sel'))selectedPaths.add(p.path);else selectedPaths.delete(p.path);
    updateStats();};
  d.querySelector('.targetBtn').onclick=e=>{e.stopPropagation();setTarget(p.path);};
  d.querySelector('.starBtn').onclick=e=>{e.stopPropagation();toggleStar(p);};
  const cmp=d.querySelector('.compareBtn');
  if(cmp)cmp.onclick=e=>{e.stopPropagation();openGroupCompare(p.group);};
  d.ondblclick=()=>{const i=ranked.findIndex(x=>x.path===p.path);if(i>=0)lbShow(i);};
  ringQ.push(d.querySelector('.ring'));
  return d;}

async function scan(opt){
  const f=$('folder').value.trim();
  if(!f&&!(opt&&opt.paths))return toast('请先输入路径、拖入照片或点「文件夹…」');
  const req=++SCAN_REQ_GEN;
  POLL_GEN++;JOB=null;POLL_FAIL=0;
  $('scanBtn').disabled=true;$('empty').style.display='none';
  $('grid').innerHTML='';ranked=[];library=[];renderedN=0;since=0;targetPath=null;
  cardCache.clear();FINAL_STAGE='';
  selectedPaths.clear();selectionTouched=false;updateTargetUI();
  $('scanbar').classList.add('show');$('cancelscan').style.display='inline-block';
  $('scanfill').style.width='0%';$('scantext').textContent='正在读取照片列表…';POLL_FAIL=0;RECOVERED=false;
  try{
    const j=await api('/api/scan',{folder:f||'.',jobs:4,
      paths:opt&&opt.paths?opt.paths:undefined,
      faces:$('facesOpt').checked,ai:$('aiOpt').checked,
      recursive:opt&&opt.recursive!==undefined?opt.recursive:$('recursiveOpt').checked});
    if(req!==SCAN_REQ_GEN)return;
    JOB=j.job;RECOVERED=false;startPoll();
  }catch(e){
    if(req!==SCAN_REQ_GEN)return;
    scanReset();toast('出错：'+e.message);
  }}

function scanReset(){
  $('scanBtn').disabled=false;
  $('scanbar').classList.remove('show');
  $('cancelscan').style.display='none';
  JOB=null;}

let POLL_FAIL=0, POLL_GEN=0, RECOVERED=false, FINAL_STAGE='';
function startPoll(){POLL_GEN++;const g=POLL_GEN;poll(g);}   // 新任务=新一代；旧循环自动作废
async function poll(g){
  if(!JOB||g!==POLL_GEN)return;                 // 代际不符：僵尸循环退出
  try{
    const j=await api('/api/scan_status?job='+JOB+'&since='+since);
    POLL_FAIL=0;
    const g2=$('grid');
    if(j.photos&&j.photos.length){
      const frag=document.createDocumentFragment();
      for(const p of j.photos){ranked.push(p);library.push(p);
        if(!selectionTouched&&autoSel(p))selectedPaths.add(p.path);
        if(ranked.length<=Math.max(CHUNK,renderedN)){
          frag.appendChild(cardFor(p,ranked.length,true));renderedN=ranked.length;}}
      g2.appendChild(frag);since+=j.photos.length;flushRings();}
    if(j.updates&&j.updates.length){         // 精扫段：评分原地更新
      for(const u of j.updates){
        const i=ranked.findIndex(x=>x.path===u.path);
        if(i<0)continue;
        ranked[i]={...u,thumb:ranked[i].thumb};
        updateCardInPlace(cardCache.get(ranked[i].path)||lbCard(ranked[i]),ranked[i]);
      }
      flushRings();updateStats();
      $('scantext').textContent='AI 精扫中 '+j.refine_done+'/'+j.refine_total+' …（可先选片）';
      $('scanInfo').textContent='精扫 '+j.refine_done+'/'+j.refine_total;}
    const pct=j.total?Math.min(100,j.done/j.total*100):0;
    $('scanfill').style.width=pct+'%';
    const eta=j.done>2?fmtT(j.elapsed/j.done*(j.total-j.done)):'…';
    if(!j.finished){
      $('scantext').textContent=`扫描中 ${j.done}/${j.total} · 剩余约 ${eta}`
        +(j.skipped?` · 跳过 ${j.skipped}`:'');
      $('scanInfo').textContent=`${j.done}/${j.total}`;
      setTimeout(()=>poll(g),600);return;}
    const finalStage=j.refine_pending?'fast':'refined';
    if(j.final&&FINAL_STAGE!==finalStage){   // 极速/精扫终态各只重排一次；不能用可见数量判断
      FINAL_STAGE=finalStage;
      library=j.final.slice();
      if(!selectionTouched)selectedPaths=new Set(library.filter(autoSel).map(p=>p.path));
      rebuildView();}
    if(j.refine_pending){                      // 极速段完成，精扫继续
      $('scantext').textContent=`出图完成 ${ranked.length} 张 · AI 精扫中（可先选片）`;
      setTimeout(()=>poll(g),1200);return;}
    $('scantext').textContent=(j.cancelled?'已取消 · ':'')+`完成 ${ranked.length} 张`
      +(j.skipped?` · 跳过 ${j.skipped}`:'');
    updateStats();
    scanReset();
    toast((j.cancelled?'已取消，已保留已扫部分':'扫描完成')+' · 按综合分排序');
  }catch(e){
    if(g!==POLL_GEN)return;                    // 旧循环的失败不处理
    POLL_FAIL=(POLL_FAIL||0)+1;
    if(POLL_FAIL<=3){$('scantext').textContent='扫描状态查询重试中…';setTimeout(()=>poll(g),1200);return;}
    // 任务丢失（引擎重启等）：有目录上下文时自动重扫一次
    if(!RECOVERED&&$('folder').value.trim()){
      RECOVERED=true;POLL_FAIL=0;
      toast('扫描任务丢失，自动重新扫描…');
      $('status').textContent='任务丢失，自动恢复中…';
      scan();return;}
    scanReset();POLL_FAIL=0;
    $('scantext').textContent='';
    $('status').textContent='扫描中断：'+e.message+' · 请重新点「扫描打分」';
    toast('扫描中断：'+e.message);
  }}

function cancelScan(){if(JOB){$('scantext').textContent='正在取消…';api('/api/scan_cancel',{job:JOB});}}

function renderChunk(animate=true){
  const g=$('grid');const frag=document.createDocumentFragment();
  const end=Math.min(ranked.length,renderedN+CHUNK);
  for(let i=renderedN;i<end;i++)frag.appendChild(cardFor(ranked[i],i,animate));
  g.appendChild(frag);renderedN=end;flushRings();}

const grid=$('grid');
let renderScrollQueued=false;
grid.addEventListener('scroll',()=>{
  if(renderScrollQueued)return;
  renderScrollQueued=true;
  requestAnimationFrame(()=>{
    renderScrollQueued=false;
    if(renderedN<ranked.length
       &&grid.scrollTop+grid.clientHeight>=grid.scrollHeight-600)renderChunk();
  });
});

function selAll(mode){
  selectionTouched=true;
  if(mode==1)selectedPaths=new Set(ranked.map(p=>p.path));
  else if(mode==0)selectedPaths=new Set(ranked.filter(autoSel).map(p=>p.path));
  else selectedPaths.clear();
  document.querySelectorAll('.card').forEach(c=>{
    c.classList.toggle('sel',selectedPaths.has(c.dataset.path));});
  updateStats();
}

/* 分段控件 */
function bindSeg(id,onPick){
  const seg=$(id);const n=seg.querySelectorAll('button').length;
  seg.style.setProperty('--segn',n);
  seg.querySelectorAll('button').forEach((b,i)=>b.onclick=()=>{
    seg.querySelectorAll('button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
    seg.querySelector('.thumb').style.transform=`translateX(${i*100}%)`;
    onPick&&onPick(b.dataset.v);});}
bindSeg('wbSeg',v=>{$('rowWbStrength').hidden=v=='off';});

/* ── 导入：原生选择 / 拖拽 → 预处理 → 上传 → 自动扫描 ── */
const RAW_EXT=new Set(['arw','cr2','cr3','nef','nrw','dng','raf','orf','rw2','pef','srw','x3f']);
const OK_EXT=new Set(['jpg','jpeg','png','webp','tif','tiff','bmp',...RAW_EXT]);
const RESIZABLE=new Set(['jpg','jpeg','png','webp']);

async function prepareBlob(file){
  const ext=(file.name.split('.').pop()||'').toLowerCase();
  if(!OK_EXT.has(ext))return null;
  if(!RESIZABLE.has(ext))return file;                     // RAW 原样上传，交给 LibRaw
  try{
    const bmp=await createImageBitmap(file);
    const s=Math.min(1,2560/Math.max(bmp.width,bmp.height));
    if(s>=1&&file.size<8*1024*1024)return file;           // 小图没必要重编码
    const cv=document.createElement('canvas');
    cv.width=Math.round(bmp.width*s);cv.height=Math.round(bmp.height*s);
    cv.getContext('2d').drawImage(bmp,0,0,cv.width,cv.height);
    const blob=await new Promise(r=>cv.toBlob(r,'image/jpeg',0.92));
    return blob?new File([blob],file.name.replace(/\.\w+$/,'')+'.jpg',{type:'image/jpeg'}):file;
  }catch(e){return file;}                                 // 解码失败原样上传让服务端试
}

let uploadAbort=null;
async function uploadFiles(list){
  const files=[...list].filter(f=>OK_EXT.has((f.name.split('.').pop()||'').toLowerCase()));
  if(!files.length)return toast('没有可导入的照片文件');
  const batch='import-'+Date.now().toString(36);
  let folder=null,ok=0,done=0;
  setActionButtonsDisabled(true);
  const CONC=4;let idx=0;
  const worker=async()=>{
    while(idx<files.length){
      const i=idx++;
      done++;
      $('status').textContent=`导入中 ${done}/${files.length} · ${files[i].name}`;
      try{
        const blob=await prepareBlob(files[i]);
        if(!blob)continue;
        const relativeName=files[i]._photoPilotRelativePath||files[i].webkitRelativePath||files[i].name;
        const r=await fetch('/api/upload',{method:'POST',
          headers:{'X-Filename':encodeURIComponent(relativeName),'X-Batch':batch},
          body:await blob.arrayBuffer()});
        const j=await r.json();
        if(j.error){toast('跳过 '+files[i].name+'：'+j.error);continue;}
        folder=j.folder;ok++;
      }catch(e){toast('导入出错：'+e.message)}
    }
  };
  await Promise.all(Array.from({length:Math.min(CONC,files.length)},worker));
  setActionButtonsDisabled(false);
  if(!ok){$('status').textContent='';return toast('没有照片被导入');}
  $('folder').value=folder;
  const hasNested=files.some(f=>(f._photoPilotRelativePath||f.webkitRelativePath||'').includes('/'));
  $('status').textContent=`导入完成 ${ok} 张，正在扫描…`;
  scan({recursive:$('recursiveOpt').checked||hasNested});
}

async function importLocalPaths(paths){
  // 桌面模式：本机路径直导（不拷贝不重编码）+ 服务端一步启动扫描
  const req=++SCAN_REQ_GEN;
  POLL_GEN++;JOB=null;POLL_FAIL=0;RECOVERED=false;
  try{
    const j=await api('/api/import_paths',{paths,
      recursive:$('recursiveOpt').checked,
      faces:$('facesOpt').checked,ai:$('aiOpt').checked});
    if(req!==SCAN_REQ_GEN)return;
    $('status').textContent=`已导入 ${j.count} 张（本机直导）`;
    $('folder').value=(j.scan_dirs&&j.scan_dirs[0])||'';
    $('scanBtn').disabled=true;$('empty').style.display='none';
    $('grid').innerHTML='';ranked=[];library=[];renderedN=0;since=0;targetPath=null;
    cardCache.clear();FINAL_STAGE='';
    selectedPaths.clear();selectionTouched=false;updateTargetUI();
    $('scanbar').classList.add('show');$('cancelscan').style.display='inline-block';
    $('scanfill').style.width='0%';$('scantext').textContent='正在读取照片…';POLL_FAIL=0;RECOVERED=false;
    JOB=j.job;startPoll();                            // 服务端已开扫，这里只轮询
  }catch(e){
    if(req!==SCAN_REQ_GEN)return;
    scanReset();toast('导入失败：'+e.message);
  }
}
function walkEntry(entry,out,prefix=''){
  return new Promise(res=>{
    if(entry.isFile)entry.file(f=>{f._photoPilotRelativePath=prefix+f.name;out.push(f);res();},()=>res());
    else if(entry.isDirectory){
      const rd=entry.createReader();
      const read=()=>rd.readEntries(async ents=>{
        if(!ents.length)return res();
        for(const e of ents)await walkEntry(e,out,prefix+entry.name+'/');
        read();
      },()=>res());
      read();
    }else res();
  });
}
let dragDepth=0;
window.addEventListener('dragover',e=>{
  if(![...e.dataTransfer.types].includes('Files'))return;
  e.preventDefault();$('drop').classList.add('show');});
window.addEventListener('dragenter',e=>{
  if([...e.dataTransfer.types].includes('Files')){e.preventDefault();dragDepth++;}});
window.addEventListener('dragleave',e=>{
  if(--dragDepth<=0){dragDepth=0;$('drop').classList.remove('show');}});
window.addEventListener('drop',async e=>{
  e.preventDefault();dragDepth=0;$('drop').classList.remove('show');
  const items=[...(e.dataTransfer.items||[])];
  const entries=items.map(i=>i.webkitGetAsEntry&&i.webkitGetAsEntry()).filter(Boolean);
  if(!entries.length)return uploadFiles(e.dataTransfer.files);
  const out=[];
  $('status').textContent='读取拖入内容…';
  for(const en of entries)await walkEntry(en,out);
  uploadFiles(out);});

async function process(operation){
  let sel=ranked.filter(p=>selectedPaths.has(p.path)).map(p=>p.path);
  if(!sel.length)return toast('未选中任何照片');
  if(operation==='color'){
    if(!targetPath)return toast('请选择一张目标照片：点击卡片上的「设为目标」');
    sel=sel.filter(p=>p!==targetPath);
    if(!sel.length)return toast('请再选至少一张要追色的照片');
  }
  if(operation==='preset' && !$('presetSelect').value)return toast('请选择一个风格预设');
  const polish={skin:+$('skin').value,retain:+$('retain').value,
                clarity:+$('clarity').value,wb:+$('wb').value,
                face_repair:+$('faceRepair').value,blemish:+$('blemish').value,
                local_region:$('localRegion').value,
                auto_tone:$('autoTone').checked};
  let prep;
  setActionButtonsDisabled(true);$('prog').classList.add('show');
  $('progDesc').textContent=operation==='color'?'解析目标照片…':operation==='preset'?'准备风格预设…':'准备美化参数…';$('pfill').style.width='0%';
  try{
    const preset=operation==='preset'?$('presetSelect').value:null;
    prep=await api('/api/prepare',{folder:$('folder').value.trim(),paths:sel,
      operation:operation==='preset'?'both':operation,
      preset,
      algo:operation==='color'?$('algo').value:(operation==='preset'?'oklab':'none'),strength:operation==='color'||operation==='preset'?+$('strength').value:0,
      preserve_luma:$('preserveLuma').checked,
      skin_protect:$('skinProtect').checked?true:null,
      ref:operation==='color'?targetPath:null,
      wb_mode:operation==='color'?'off':$('wbSeg').querySelector('button.on').dataset.v,
      wb_strength:+$('wbStrength').value,
      polish});
    $('progDesc').textContent=prep.ref_desc;
    const inner=$('resInner');
    const single=sel.length===1;
    $('results').classList.toggle('single-result',single);
    const refDesc=escapeHtml(prep.ref_desc);
    inner.innerHTML=single
      ?`<h2 id="resTitle"><span>单张修图 · ${refDesc} · 左右拖动对比</span><button class="btn ghost resultClose" type="button" onclick="closeResults()">完成 Esc</button></h2>`
      :`<h2 id="resTitle">处理结果 · ${refDesc} · 左右拖动对比</h2>`;
    let done=0;
    for(const p of sel){
      $('pnum').textContent=`${done} / ${prep.count}`;
      $('pfill').style.width=(done/prep.count*100)+'%';
      try{
        const r=await api('/api/process_one',{sid:prep.sid,path:p,preview_size:single?1800:640});
        done++;
        const pair=document.createElement('div');pair.className='pair';
        pair.innerHTML=`<div class="cmp"><img src="${escapeHtml(r.before)}"><img class="b" src="${escapeHtml(r.after)}">
          <div class="bar"></div><div class="knob">◄►</div>
          <span class="tag l">原始</span><span class="tag r">成品</span></div>
          <div class="name" style="padding:8px 4px 0">${escapeHtml(p.split('/').pop())}</div>`;
        bindCmp(pair.querySelector('.cmp'));
        inner.appendChild(pair);
      }catch(e){done++;console.warn(e);toast('跳过一张：'+e.message)}
    }
    $('pnum').textContent=`${done} / ${prep.count}`;
    $('pfill').style.width='100%';
    setTimeout(()=>{$('prog').classList.remove('show');
      if(done){$('results').classList.add('show');toast(`完成 ${done} 张 · 拖动分割线对比`);}
      $('status').textContent=`完成 ${done}/${prep.count} · 成品在 photopilot_out/`;},450);
  }catch(e){$('prog').classList.remove('show');toast('出错：'+e.message);$('status').textContent='';}
    setActionButtonsDisabled(false);}

function closeResults(){
  $('results').classList.remove('show','single-result');
}

function bindCmp(el){
  el.style.setProperty('--pos','50%');
  const move=e=>{const r=el.getBoundingClientRect();
    const p=Math.min(97,Math.max(3,(e.clientX-r.left)/r.width*100));
    el.style.setProperty('--pos',p+'%');};
  el.addEventListener('pointerdown',e=>{el.setPointerCapture(e.pointerId);move(e);
    el.onpointermove=move;});
  el.addEventListener('pointerup',()=>el.onpointermove=null);
  el.addEventListener('pointercancel',()=>el.onpointermove=null);}

function syncRatingUI(p){
  const card=lbCard(p);
  if(card){
    const button=card.querySelector('.starBtn');
    if(button){button.classList.toggle('on',!!p.rating);button.textContent=p.rating?'★':'☆';
      button.title=p.rating?'取消星标':'加星';button.setAttribute('aria-label',p.rating?'取消星标':'加星');}
    const badges=card.querySelector('.badges');
    let badge=badges&&badges.querySelector('.rstar');
    if(p.rating){
      if(!badge&&badges){badge=document.createElement('span');badge.className='pill rstar';badges.appendChild(badge);}
      if(badge)badge.textContent='★'+p.rating;
    }else if(badge)badge.remove();
  }
  document.querySelectorAll('.comparePane').forEach(pane=>{
    if(pane.dataset.path!==p.path)return;
    const button=pane.querySelector('.compareStar');
    if(button){button.classList.toggle('on',!!p.rating);button.textContent=p.rating?'★':'☆';
      button.setAttribute('aria-pressed',p.rating?'true':'false');}
  });
  if($('lb').classList.contains('show')&&ranked[LBI]?.path===p.path)lbRenderStars(p.rating||0);
  updateStats();
}
function refreshRatedView(p){
  if(sortMode!=='rating'&&filterMode!=='starred')return;
  const lbOpen=$('lb').classList.contains('show'),oldIndex=LBI;
  rebuildView();
  if(lbOpen){
    const same=ranked.findIndex(item=>item.path===p.path);
    LBI=same>=0?same:Math.min(oldIndex,ranked.length-1);
    if(LBI<0)lbHide();else lbUpdate(true);
  }
}
async function toggleStar(p){
  const previousRating=p.rating||0;
  const next=previousRating?0:5;p.rating=next;syncRatingUI(p);
  try{
    await api('/api/rate',{path:p.path,rating:next,score:p.score,flags:p.flags,
      sharpness:p.sharpness,exposure:p.exposure,faces:p.faces});
    refreshRatedView(p);
    toast(next?'已加 5 星':'已取消星标');
  }catch(e){p.rating=previousRating;syncRatingUI(p);toast('写评分失败：'+e.message);}
}

let compareGroupId=null;
function groupItems(group){
  return library.filter(p=>String(p.group)===String(group)).sort((a,b)=>(a.group_rank||99)-(b.group_rank||99)).slice(0,4);
}
function renderGroupCompare(items){
  const grid=$('compareGrid');grid.innerHTML='';
  $('compareTitle').textContent=`相似照片对比 · ${items.length} 张`;
  for(const p of items){
    const pane=document.createElement('div');pane.className='comparePane';pane.style.setProperty('--zoom','1');
    pane.dataset.path=p.path;
    pane.innerHTML=`<img src="/api/raw_img?p=${encodeURIComponent(p.path)}&amp;s=1800" alt="${escapeHtml(p.path.split('/').pop())}">
      <div class="compareMeta"><span>${escapeHtml(p.path.split('/').pop())} · ${p.score.toFixed(2)}</span>
      <button class="compareStar${p.rating?' on':''}" type="button" aria-label="${p.rating?'取消星标':'加星'}" aria-pressed="${p.rating?'true':'false'}">${p.rating?'★':'☆'}</button></div>`;
    pane.querySelector('.compareStar').onclick=()=>toggleStar(p);
    grid.appendChild(pane);
  }
}
function openGroupCompare(group){
  const items=groupItems(group);if(items.length<2)return toast('这张照片没有可比较的相似组');
  compareGroupId=group;renderGroupCompare(items);$('compareZoom').value=1;$('compareZoomValue').textContent='1.0×';
  $('compareModal').classList.add('show');
}
function openSelectedCompare(){
  const selected=ranked.filter(p=>selectedPaths.has(p.path));
  if(selected.length<2||selected.length>4)return toast('请先选择 2–4 张照片进行对比');
  const items=selected.slice(0,4);
  compareGroupId=null;renderGroupCompare(items);$('compareZoom').value=1;$('compareZoomValue').textContent='1.0×';$('compareModal').classList.add('show');
}
function closeGroupCompare(){$('compareModal').classList.remove('show');compareGroupId=null;}
$('compareZoom').oninput=e=>{
  const z=+e.target.value;$('compareZoomValue').textContent=z.toFixed(1)+'×';
  document.querySelectorAll('.comparePane').forEach(p=>p.style.setProperty('--zoom',z));
};
$('compareModal').onclick=e=>{if(e.target===$('compareModal'))closeGroupCompare();};

/* ── 快审灯箱：双击卡片进入，键盘批量审片，星级写 XMP sidecar ── */
let LBI=-1;
const lbStars=$('lbStars');
for(let i=1;i<=5;i++){
  const b=document.createElement('button');b.textContent='★';b.dataset.v=i;
  b.title=i+' 星';b.onclick=()=>lbRate(i);lbStars.appendChild(b);}
const lbClr=document.createElement('button');lbClr.textContent='清除';lbClr.className='clr';
lbClr.title='清除星级（0）';lbClr.onclick=()=>lbRate(0);lbStars.appendChild(lbClr);
function lbRenderStars(r){
  lbStars.querySelectorAll('button').forEach(b=>{
    if(b.className==='clr'){b.classList.toggle('on',r===0);return;}
    b.classList.toggle('on',+b.dataset.v<=r);});}
function lbShow(i){LBI=i;$('lb').classList.add('show');lbUpdate(true);}
function lbHide(){$('lb').classList.remove('show');}
function lbCard(p){return [...document.querySelectorAll('.card')].find(c=>c.dataset.path===p.path);}
function lbUpdate(reload){
  const p=ranked[LBI];if(!p)return;
  $('lbName').textContent=p.path.split('/').pop();
  $('lbIdx').textContent=(LBI+1)+' / '+ranked.length;
  lbRenderStars(p.rating||0);
  $('lbInfo').innerHTML=`<span>综合分 <b>${p.score.toFixed(2)}</b></span>`
    +(p.faces?`<span>人脸 <b>${p.faces}</b></span>`:'')
    +(p.flags.length?p.flags.map(f=>`<span>${escapeHtml(f)}</span>`).join(''):'<span><b>通过</b></span>');
  if(reload){
    const img=$('lbImg');img.alt=p.path.split('/').pop()+' 预览';
    img.classList.remove('ld');$('lbSpin').style.display='block';$('lbSpin').textContent='加载中…';
    img.onload=()=>{img.classList.add('ld');$('lbSpin').style.display='none';};
    img.onerror=()=>{$('lbSpin').textContent='无法预览此文件';};
    img.src='/api/raw_img?p='+encodeURIComponent(p.path);
    const nb=ranked[(LBI+1)%ranked.length];            // 预取下一张，翻页秒开
    if(nb){const pre=new Image();pre.src='/api/raw_img?p='+encodeURIComponent(nb.path);}}
  document.querySelectorAll('.card.lbcur').forEach(c=>c.classList.remove('lbcur'));
  const card=lbCard(p);if(card){card.classList.add('lbcur');
    if(card.scrollIntoView)card.scrollIntoView({block:'nearest'});}}
function lbNav(d){
  if(!ranked.length)return;
  LBI=(LBI+d+ranked.length)%ranked.length;lbUpdate(true);}
async function lbRate(r){
  const p=ranked[LBI];if(!p)return;
  const previousRating=p.rating||0;
  p.rating=r;syncRatingUI(p);
  try{
    await api('/api/rate',{path:p.path,rating:r,score:p.score,flags:p.flags,
      sharpness:p.sharpness,exposure:p.exposure,faces:p.faces});
    refreshRatedView(p);
    toast(p.path.split('/').pop()+' → '+(r?r+' 星（XMP 已写）':'星级已清除'));
  }catch(e){p.rating=previousRating;syncRatingUI(p);toast('写评分失败：'+e.message);}}

document.addEventListener('keydown',e=>{
  const tag=(document.activeElement&&document.activeElement.tagName)||'';
  if($('compareModal').classList.contains('show')){
    if(e.key==='Escape')closeGroupCompare();
    return;
  }
  if($('lb').classList.contains('show')){
    if(e.key==='Escape')lbHide();
    else if(e.key==='ArrowRight'||(e.key.toLowerCase()==='j'&&!/input|select/i.test(tag)))lbNav(1);
    else if(e.key==='ArrowLeft'||(e.key.toLowerCase()==='k'&&!/input|select/i.test(tag)))lbNav(-1);
    else if(e.key===' '){e.preventDefault();
      const p=ranked[LBI];if(!p)return;
      selectionTouched=true;
      if(selectedPaths.has(p.path))selectedPaths.delete(p.path);else selectedPaths.add(p.path);
      const card=lbCard(p);if(card)card.classList.toggle('sel',selectedPaths.has(p.path));
      updateStats();toast(selectedPaths.has(p.path)?'已加入保留':'已移出保留');}
    else if(/^[0-5]$/.test(e.key)&&!/input|select/i.test(tag))lbRate(+e.key);
    return;}
  if($('results').classList.contains('show')&&e.key==='Escape'){
    closeResults();return;
  }
  if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='a'&&!/input|select/i.test(tag)){
    e.preventDefault();selAll(1);}});
bindViewControls();
const PRESET_TUNING={};
function applyPresetTuning(name){
  const t=PRESET_TUNING[name];if(!t)return;
  const ranges={skin:'vSkin',retain:'vRetain',clarity:'vClarity',wb:'vWb',face_repair:'vFaceRepair',blemish:'vBlemish'};
  for(const [key,out] of Object.entries(ranges)){
    const el=$(key==='face_repair'?'faceRepair':key);
    if(el&&t[key]!==undefined){el.value=t[key];slide(el,out);}
  }
  if($('localRegion')&&t.local_region)$('localRegion').value=t.local_region;
}
api('/api/presets').then(j=>{
  const s=$('presetSelect');if(!s||!Array.isArray(j.presets))return;
  s.innerHTML='';
  const groups={};
  for(const p of j.presets){
    if(p.polish)PRESET_TUNING[p.name]=p.polish;
    const key=p.category||'基础';
    const g=groups[key]||(groups[key]=document.createElement('optgroup'));
    g.label=key;
    const o=document.createElement('option');
    o.value=p.name;o.textContent=p.label||p.name;o.title=p.description||'';
    g.appendChild(o);
  }
  Object.values(groups).forEach(g=>s.appendChild(g));
  s.onchange=()=>applyPresetTuning(s.value);
  applyPresetTuning(s.value);
}).catch(()=>{});
updateTargetUI();
document.querySelectorAll('input[type=range]').forEach(r=>slide(r,r.id.replace(/^(strength|skin|retain|clarity|wb|faceRepair|blemish)$/,'v$1').replace('vstrength','vStrength').replace('vretain','vRetain').replace('vclarity','vClarity').replace('vwb','vWb').replace('vskin','vSkin').replace('vfaceRepair','vFaceRepair').replace('vblemish','vBlemish')));
const INIT_FOLDER=__INIT_FOLDER_JSON__;
if(INIT_FOLDER){$('folder').value=INIT_FOLDER;scan();}
</script></body></html>"""


INIT_FOLDER: str | None = None   # serve(folder) 注入，页面加载即自动扫描


def _acquire_app_instance_lock(lock_path: Path | None = None):
    """桌面模式单实例锁；关闭窗口/进程退出后由 OS 自动释放。"""
    path = Path(lock_path) if lock_path else Path.home() / ".cache" / "photopilot" / "app.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise


def _find_free_port(preferred: int = 8618) -> int:
    import socket
    for p in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return preferred


def start_server(folder: str | None = None, port: int = 8618):
    """绑定端口并返回运行中的 httpd（app 模式在后台线程 serve_forever）。"""
    global INIT_FOLDER
    if folder:
        INIT_FOLDER = str(Path(folder).resolve())
    threading.Thread(target=_thumb_gc, daemon=True).start()
    threading.Thread(target=_warm_engine, daemon=True).start()   # 引擎预热
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(folder: str | None = None, port: int = 8618, open_browser: bool = True):
    import webbrowser
    port = _find_free_port(port)
    httpd = start_server(folder, port)
    url = f"http://127.0.0.1:{port}"
    print(f"PhotoPilot UI 运行中：{url}  （Ctrl+C 停止）")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("已停止")


def run_app(folder: str | None = None, port: int = 0):
    """桌面软件模式：pywebview 原生窗口（WKWebView），不开浏览器标签页。

    pywebview 不可用时回退 Chrome --app 窗口，再回退默认浏览器。
    """
    import traceback

    try:
        instance_lock = _acquire_app_instance_lock()
    except OSError as e:
        print(f"无法创建 PhotoPilot 单实例锁：{e}")
        return
    if instance_lock is None:
        print("PhotoPilot 已在运行，本次启动已忽略；请切换到已打开的窗口。")
        return
    # Keep the file handle alive so the OS lock lasts for the whole app session.

    # LaunchServices does not expose a console, so preserve the real startup
    # exception for diagnosis instead of silently losing it to /dev/null.
    launch_log = Path.home() / "Library" / "Logs" / "PhotoPilot.log"

    def log_launch(message: str):
        try:
            launch_log.parent.mkdir(parents=True, exist_ok=True)
            with launch_log.open("a", encoding="utf-8") as fh:
                fh.write(message.rstrip() + "\n")
        except OSError:
            pass

    port = _find_free_port(port or 8618)
    httpd = start_server(folder, port)
    url = f"http://127.0.0.1:{port}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log_launch(f"PhotoPilot startup: engine={url}")
    print(f"PhotoPilot 引擎运行中：{url}")

    try:
        import webview

        class NativeDialogs:
            """暴露给页面的原生选择器：window.pywebview.api.* 不存在时页面自动回退。"""

            def pick_folder(self):
                result = webview.windows[0].create_file_dialog(
                    webview.FOLDER_DIALOG)
                return result[0] if result else None

            def pick_photos(self):
                result = webview.windows[0].create_file_dialog(
                    webview.OPEN_DIALOG, allow_multiple=True,
                    file_types=("图片文件 (*.jpg;*.jpeg;*.png;*.webp;*.tif;*.tiff;*.bmp;"
                                "*.arw;*.cr2;*.cr3;*.nef;*.nrw;*.dng;*.raf;*.orf;*.rw2;*.pef;*.srw;*.x3f)",))
                return list(result) if result else None

        webview.create_window("PhotoPilot", url, width=1440, height=920,
                              min_size=(1080, 680), background_color="#101014",
                              js_api=NativeDialogs())
        webview.start()          # 窗口关闭即返回
        print("窗口已关闭")
        return
    except BaseException as e:
        details = traceback.format_exc()
        log_launch(f"pywebview startup failed: {e}\n{details}")
        print(f"pywebview 不可用（{e}），尝试 Chrome App 窗口 …")

    import subprocess
    chrome = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",)
    if Path(chrome[0]).exists():
        try:
            profile = Path.home() / ".cache" / "photopilot" / "chrome"
            profile.mkdir(parents=True, exist_ok=True)
            child = subprocess.Popen(
                [*chrome, f"--app={url}", "--window-size=1440,920",
                 f"--user-data-dir={profile}"])
            log_launch(f"Chrome fallback started: pid={child.pid}")
            # Chrome may hand the request to an existing process and return
            # immediately. Keep the local engine alive in either case.
            child.wait()
        except Exception:
            log_launch(f"Chrome fallback failed:\n{traceback.format_exc()}")
        finally:
            threading.Event().wait()
        return
    import webbrowser
    webbrowser.open(url)
    print("已用默认浏览器打开（安装 pywebview 可获得原生窗口）")
    try:
        threading.Event().wait()   # 保持引擎运行
    except KeyboardInterrupt:
        pass
