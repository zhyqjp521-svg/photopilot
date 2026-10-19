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
  POST /api/trash            {paths[]} → 把选中的本地原片与 XMP sidecar 移到系统废纸篓/回收站
  GET  /api/raw_img?p=       快审灯箱大图（2200px，磁盘缓存）
"""
from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import html
import io
import json
import math
import os
import tempfile
import threading
import time
import urllib.parse
import uuid
from collections import OrderedDict
from dataclasses import replace
from concurrent.futures import as_completed, ThreadPoolExecutor, Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np

from .io import (load_image, save_image, iter_images, unique_paths,
                 collision_safe_output_names, output_suffix, resize_max, is_image,
                 extract_exif, extract_icc, srgb_icc_profile, exif_summary)
from .cull import (score_photo, dhash, finalize_cull, smart_rank, PhotoScore, WORK_SIZE,
                   _get_pool, _get_fast_pool)
from .color import (match_color, reference_from_image, reference_from_preset,
                    reference_from_stats, oklab_stats, PRESETS, PRESET_META)
from .polish import (polish_image, PolishParams, estimate_wb_gains,
                     batch_target_gains, apply_wb_gains)
from .face_analysis import analyze_faces, scale_faces
from .xmp import rating_for, read_rating, write_sidecar
from .project import get_project_store, MAX_PROJECT_BYTES
from .export_queue import ExportQueue
from .export_recipe import freeze_session, thaw_session
from .similarity_jobs import SimilarityJobs, MAX_REGROUP_BYTES

PROCESS_LOCK = threading.Lock()   # FaceMesh 检测串行化，避免偶发竞态
# HTTP threads are short-lived; their TLS FaceMesh would be recreated for every
# slider request. One persistent worker owns/reuses the edit detector instead.
_EDIT_FACE_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='PhotoPilotEditFace')
_THUMB_LOCK = threading.Lock()     # 缩略图 LRU 由扫描线程与 HTTP 线程共享
EXPORT_SIZE = 2048                # Web 预览导出尺寸（比 run 命令略小，求快）

_thumb_cache: OrderedDict[tuple, bytes] = OrderedDict()
_thumb_flights: dict[tuple, Future] = {}
_PREVIEW_DECODE_LIMIT = threading.BoundedSemaphore(2)
_CACHE_CAP = 600
_SESSIONS: dict[str, dict] = {}   # sid → 处理会话（参考图 + 参数）
_EXPORT_QUEUE = None
_EXPORT_QUEUE_LOCK = threading.Lock()
_EXPORT_SUBMIT_LOCK = threading.Lock()
MAX_EXPORT_REQUEST_BYTES = 2 * 1024 * 1024
MAX_TRASH_REQUEST_BYTES = 4 * 1024 * 1024
MAX_TRASH_PATHS = 20000
_import_dirs: dict[str, Path] = {}  # 上传批次 → 落盘目录
# 拖入/选择导入的照片落盘位置：优先用户看得见的"图片"目录，失败（如 TCC 拒绝）
# 回退到项目目录
from .platform import pictures_dir, cache_dir, log_dir as _log_dir
UPLOAD_ROOTS = [pictures_dir(),
                Path(__file__).resolve().parents[1] / "uploads"]

# 缩略图磁盘缓存：万张级图库二次扫描/滚动浏览接近零开销
_THUMB_DIR_CANDS = [cache_dir() / "thumbs",
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
SIMILARITY_JOBS = SimilarityJobs(lambda path, size: _thumb(path, size))


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
        flight = _thumb_flights.get(key)
        owner = flight is None
        if owner:
            flight = _thumb_flights[key] = Future()
    if not owner:
        return flight.result()
    try:
        data = None
        try:
            dp = _thumb_disk(path, size)
            if dp.exists():
                data = dp.read_bytes()
        except OSError:
            pass
        if data is None:
            # Browsing never needs full RAW development; exports still use
            # load_image's full-quality default. Bound concurrent native decodes.
            with _PREVIEW_DECODE_LIMIT:
                img = load_image(path, max_size=size, raw_preview=True)
                data = _thumb_bytes_from_img(img, path, size)
        _remember_thumb(key, data)
        flight.set_result(data)
        return data
    except BaseException as exc:
        flight.set_exception(exc)
        raise
    finally:
        with _THUMB_LOCK:
            _thumb_flights.pop(key, None)


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
        print("[warmup] 扫描引擎预热完成（人脸检测 + 美学模型就绪）")
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
    similarity_profile = payload.get('similarity_profile', 'balanced')
    if similarity_profile not in ('strict', 'balanced', 'loose'):
        raise ValueError('相似分组 similarity_profile 无效')
    raw_paths = payload.get("paths") or None
    override_paths = (unique_paths(str(Path(p).expanduser().absolute()) for p in raw_paths)
                      if raw_paths else None)
    folder = Path(payload.get("folder") or ".").expanduser().absolute()
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
        "similarity_profile": similarity_profile,
        "ai": bool(payload.get("ai", False)),
        "recursive": bool(payload.get("recursive", False)),
        "recursive_auto": False,
        "fast": bool(payload.get("fast", True)),   # 两段式：极速出图 + 精扫更新
        "updates": [], "refine_done": 0, "refine_total": 0,
        "refine_finished": False, "refine_cancel": False,
        "jobs": max(1, min(8, int(payload.get("jobs") or 4))), "futures": [],
        "refine_futures": [],
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
    return {"job": job_id, "folder": str(folder)}


def _scan_worker(job_id: str):
    """Every scan, including enumeration/executor failures, has a terminal state."""
    try:
        _scan_worker_impl(job_id)
    except Exception as exc:
        job = SCAN_JOBS.get(job_id)
        if job is not None:
            for future in [*job.get("futures", ()), *job.get("refine_futures", ())]:
                future.cancel()
            job.update(finished=True, refine_finished=True,
                       cancelled=bool(job.get("cancel")),
                       finalize_error=str(exc), photos_final=job.get("photos_final", []))
        print(f"[cull] 扫描失败：{exc}")


def _scan_worker_impl(job_id: str):
    job = SCAN_JOBS.get(job_id)
    if job is None:
        return
    if job.get("override_paths"):
        paths = [str(p) for p in job["override_paths"] if Path(p).is_file()]
    else:
        paths = [str(p) for p in iter_images(job["folder"], recursive=job["recursive"])]
        # Folder selection is often used on an event root whose photos are all
        # in day/session subfolders. If the top level has no readable photos,
        # retry recursively once so the import does not appear to do nothing.
        if not paths and not job["recursive"]:
            nested_paths = [str(p) for p in iter_images(job["folder"], recursive=True)]
            if nested_paths:
                paths = nested_paths
                job["recursive"] = True
                job["recursive_auto"] = True
    job["total"] = len(paths)
    jobs_n = job["jobs"] or min(4, os.cpu_count() or 1)

    need_heavy = job["fast"] and (job["faces"] or job["ai"])
    full_for_fast = not job["fast"]   # fast=False：极速段直接跑全量（旧行为）

    def worker_fast(path: str):
        """极速段：只解码 + 基础指标 + 缩略图（无模型，秒级/张）。"""
        # RAW 优先使用相机写入的 JPEG 预览（毫秒级），避免批量 LibRaw
        # 显影把 78 张照片拖成分钟级；完整显影留给后续处理/精修。
        metadata = {}
        img = load_image(path, max_size=WORK_SIZE, raw_preview=job["fast"], metadata=metadata)
        ps = score_photo(img, path, faces=full_for_fast and job["faces"],
                         ai=full_for_fast and job["ai"], metadata=metadata)
        h = dhash(img)
        try:
            _thumb_bytes_from_img(img, path, 360)
        except Exception:
            pass
        return ps, h

    def worker_full(path: str):
        """精扫段：基础分已算过的图补人脸 + AI。"""
        metadata = {}
        img = load_image(path, max_size=WORK_SIZE, metadata=metadata)
        return score_photo(img, path, faces=job["faces"], ai=job["ai"], metadata=metadata)

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
                positions = {p.path: i for i, p in enumerate(job["photos"])}
                job["refine_total"] = len(photos)
                pool = _get_pool(max(1, jobs_n))   # 常驻池：模型会话跨任务复用
                try:
                    futs2 = {pool.submit(worker_full, ps.path): ps for ps in photos}
                    job["refine_futures"] = list(futs2)
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
                        job["photos"][positions[ps_new.path]] = ps_new
                        job["updates"].append(ps_new.to_dict())
                    except Exception:
                        pass
                    job["refine_done"] += 1
                # 精扫收尾：重跑批内相对模糊/连拍分组，重建终态排序
                refined = [p for p in job["photos"] if isinstance(p, PhotoScore)]
                finalize_cull(refined, job["hashes"], job.get('similarity_profile', 'balanced'))
                job["photos_final"] = smart_rank(refined)
                job["refine_finished"] = True
                for pending in job.get("refine_futures", ()):
                    pending.cancel()
                job["refine_futures"] = []
            except Exception:
                import traceback
                traceback.print_exc()
            finally:
                # 取消也必须收敛 refine 状态，否则前端会永久保持轮询。
                for pending in job.get("refine_futures", ()):
                    pending.cancel()
                job["refine_futures"] = []
                job["refine_finished"] = True
                if job.get("cancel"):
                    job["cancelled"] = True
        # Start only after the fast snapshot is finalized below. Otherwise two
        # threads can regroup the same PhotoScore objects concurrently.
        refine_task = refine
    else:
        refine_task = None
        job["refine_finished"] = True
    # 收尾（含已扫部分）：批内相对模糊 + 连拍分组
    try:
        photos = [p for p in job["photos"] if isinstance(p, PhotoScore)]
        finalize_cull(photos, job["hashes"], job.get('similarity_profile', 'balanced'))
        job["photos_final"] = smart_rank(photos)
    except Exception as e:   # 收尾失败也要放行前端，不能永远 pending
        import traceback
        traceback.print_exc()
        job["photos_final"] = []
        job["finalize_error"] = str(e)
    job["cancelled"] = bool(job.get("cancel"))
    job["finished"] = True
    if refine_task is not None:
        threading.Thread(target=refine_task, daemon=True).start()
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
        "recursive_auto": bool(job.get("recursive_auto")),
        "elapsed": round(time.time() - job["started"], 1),
    }
    out["refine_pending"] = bool(job.get("fast") and (job.get("faces") or job.get("ai"))
                                 and not job.get("refine_finished"))
    cursor = max(0, int(query.get("updates_since", ["0"])[0]))
    updates = list(job["updates"])
    out["updates_since"] = len(updates)
    out["refine_done"] = job.get("refine_done", 0)
    out["refine_total"] = job.get("refine_total", 0)
    if cursor < len(updates):
        out["updates"] = updates[cursor:]
    def stored_rating(p: PhotoScore) -> int:
        value = read_rating(p.path, manual_only=True)
        # 没有人工星级时保持 0，避免“已加星”筛选把所有自动评分照片都误当成用户收藏。
        return 0 if value is None else value

    if job["finished"]:
        out["finalize_error"] = job.get("finalize_error")
        out["final_stage"] = "fast" if out["refine_pending"] else "refined"
        if query.get("final_stage", [""])[0] != out["final_stage"]:
            out["final"] = [p.to_dict() | {"thumb": _thumb_url(p.path, 360),
                                       "rating": stored_rating(p)}
                            for p in list(job["photos_final"])]
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
        for fut in job.get("refine_futures", ()):
            fut.cancel()
        # 立即收敛前端状态；后台 worker 仍会在 finally 中再次确认。
        # 把当前已扫部分作为终态，避免某个卡住的 RAW 解码让 UI 永久等待。
        if not job.get("finished"):
            partial = [p for p in list(job.get("photos", ()))
                       if isinstance(p, PhotoScore)]
            try:
                finalize_cull(partial, dict(job.get("hashes", {})), job.get('similarity_profile', 'balanced'))
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


def _move_to_trash(path: str | Path) -> None:
    """Move one file to the system Trash / Recycle Bin (never permanently unlink user media)."""
    from .platform import move_to_trash
    move_to_trash(path)


def _trash_photos(payload: dict) -> dict:
    """Validate the full selection before moving any original to recoverable Trash."""
    if not isinstance(payload, dict):
        raise ValueError('删除请求结构无效')
    paths = payload.get('paths')
    if (not isinstance(paths, list) or not paths or len(paths) > MAX_TRASH_PATHS
            or any(not isinstance(value, str) for value in paths)):
        raise ValueError('请选择 1–20000 张本地照片')

    prepared = []
    seen = set()
    for value in paths:
        if (not value or len(value) > 4096 or value.startswith('//')
                or any(ord(char) < 32 for char in value)):
            raise ValueError('照片路径无效')
        path = Path(value)
        if not path.is_absolute() or '..' in path.parts or not is_image(path):
            raise ValueError('只能删除绝对路径下的照片文件')
        if path.is_symlink():
            raise ValueError(f'不支持删除符号链接照片：{path.name}')
        if not path.is_file():
            raise FileNotFoundError(f'本地照片不存在：{path}')
        resolved = str(path.resolve())
        if resolved in seen:
            raise ValueError('删除列表中有重复照片')
        seen.add(resolved)
        sidecar = path.with_suffix('.xmp')
        if sidecar.is_symlink():
            raise ValueError(f'不支持移动符号链接 XMP：{sidecar.name}')
        if sidecar.exists() and not sidecar.is_file():
            raise ValueError(f'XMP sidecar 不是普通文件：{sidecar.name}')
        prepared.append((path, sidecar if sidecar.is_file() else None))

    removed, failed, warnings = [], [], []
    for path, sidecar in prepared:
        try:
            _move_to_trash(path)
        except Exception as exc:
            failed.append({'path': str(path), 'error': str(exc) or type(exc).__name__})
            continue
        removed.append(str(path))
        if sidecar is not None:
            try:
                _move_to_trash(sidecar)
            except Exception as exc:
                warnings.append({'path': str(path),
                                 'message': '原片已移到废纸篓/回收站，但 XMP sidecar 未能移动：'
                                            + (str(exc) or type(exc).__name__)})
    removed_set = set(removed)
    if removed_set:
        with _THUMB_LOCK:
            for key in [key for key in _thumb_cache if key[0] in removed_set]:
                _thumb_cache.pop(key, None)
    return {'removed': removed, 'failed': failed, 'warnings': warnings}


def _prepare(payload: dict, *, retain_session=True) -> dict:
    """解析一次处理会话与参数，返回会话 id；随后逐张 process_one。

    auto 参考用流式统计（逐张加载即算即弃），万张级选择也不会撑爆内存。
    """
    folder = Path(payload.get("folder") or ".").expanduser()
    if folder.is_file():
        folder = folder.parent
    paths = [Path(p) for p in unique_paths(payload.get("paths", []))]
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
            global_skin=bool(payload.get("polish", {}).get("global_skin", True)),
            face_repair=float(payload.get("polish", {}).get("face_repair", 0.0)),
            blemish=float(payload.get("polish", {}).get("blemish", 0.0)),
            local_region=str(payload.get("polish", {}).get("local_region", "all")),
            auto_tone=bool(payload.get("polish", {}).get("auto_tone", False)),
            exposure=float(payload.get("polish", {}).get("exposure", 0)),
            contrast=float(payload.get("polish", {}).get("contrast", 0)),
            highlights=float(payload.get("polish", {}).get("highlights", 0)),
            shadows=float(payload.get("polish", {}).get("shadows", 0)),
            temperature=float(payload.get("polish", {}).get("temperature", 0)),
            saturation=float(payload.get("polish", {}).get("saturation", 0)),
            ai_denoise=float(payload.get("polish", {}).get("ai_denoise", 0.0))),
        "paths": [str(p) for p in paths],
        "output_names": collision_safe_output_names((str(p) for p in paths),
                                                    preserve_raster=True),
    }
    export_size = payload.get("export_size", EXPORT_SIZE)
    ses["export_size"] = None if export_size == "original" else int(export_size)
    if ses["export_size"] is not None and ses["export_size"] not in {1024, 2048, 2560, 4096}:
        raise ValueError("请选择有效的导出尺寸")
    ses["export_quality"] = max(75, min(100, int(payload.get("export_quality", 95))))
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
    if not retain_session:
        return {"session": ses}
    sid = uuid.uuid4().hex[:12]
    _SESSIONS[sid] = ses
    if len(_SESSIONS) > 16:   # 只保留最近会话
        _SESSIONS.pop(next(iter(_SESSIONS)))
    return {"sid": sid, "ref_desc": ses["ref_desc"], "count": len(paths)}


def _decode_brush_mask(raw) -> np.ndarray:
    """把前端手刷掩码解码为 0–1 灰度数组（与原图分辨率无关，后端统一 resize）。

    接受 base64 字符串（可带 `data:image/png;base64,` 前缀）编码的灰度 PNG。
    解码失败或格式异常时抛 ValueError，由上层按参数错误处理。
    """
    if raw is None:
        raise ValueError("画笔掩码缺失")
    if not isinstance(raw, str) or not raw or len(raw) >= 8 * 1024 * 1024:
        raise ValueError("画笔掩码无效")
    if raw.startswith('data:') and not raw.startswith('data:image/png;base64,'):
        raise ValueError("画笔掩码必须是 PNG")
    payload = raw.split(",", 1)[-1] if raw.startswith('data:') else raw
    try:
        data = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("画笔掩码编码无效")
    if len(data) < 33 or data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR':
        raise ValueError("画笔掩码必须是 PNG")
    width, height = int.from_bytes(data[16:20], 'big'), int.from_bytes(data[20:24], 'big')
    if not 0 < width <= 8192 or not 0 < height <= 8192 or width * height > 16_777_216:
        raise ValueError("画笔掩码尺寸过大")
    try:
        arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    except cv2.error:
        raise ValueError("画笔掩码解码失败")
    if arr is None or arr.size == 0 or arr.dtype not in (np.uint8, np.uint16):
        raise ValueError("画笔掩码解码失败")
    maximum = float(np.iinfo(arr.dtype).max)
    if arr.ndim == 2:
        return arr.astype(np.float32) / maximum
    if arr.ndim != 3 or arr.shape[2] not in (3, 4):
        raise ValueError("画笔掩码通道无效")
    gray = cv2.cvtColor(arr[..., :3], cv2.COLOR_BGR2GRAY).astype(np.float32) / maximum
    if arr.shape[2] == 4:
        gray *= arr[..., 3].astype(np.float32) / maximum
    return gray


def _process_one(payload: dict) -> dict:
    ses = _SESSIONS.get(payload.get("sid", ""))
    if ses is None:
        raise ValueError("会话已过期，请重新处理")
    return _render_session(ses, payload)


def _render_session(ses: dict, payload: dict, *, output_destination=None) -> dict:
    """Render an explicit session; durable jobs never use the volatile sid cache."""
    # Validate request overrides before any image decoding or detector work.
    adjustments = payload.get("adjustments", {})
    tone_keys = {'exposure', 'contrast', 'highlights', 'shadows', 'temperature', 'saturation'}
    if not isinstance(adjustments, dict) or set(adjustments) - tone_keys:
        raise ValueError("未知调色参数")
    retouch = payload.get("retouch_adjustments", {})
    retouch_keys = {'skin', 'retain', 'clarity', 'face_repair', 'blemish',
                    'local_region', 'brush_mask', 'ai_denoise'}
    if not isinstance(retouch, dict) or set(retouch) - retouch_keys:
        raise ValueError("未知精修参数")
    brush_mask = None
    for key, value in retouch.items():
        if key == 'local_region':
            if not isinstance(value, str) or value not in {'all', 'skin', 'face', 'eyes', 'background', 'brush'}:
                raise ValueError("未知局部区域")
        elif key == 'brush_mask':
            brush_mask = _decode_brush_mask(value) if value else None
        elif (isinstance(value, bool) or not isinstance(value, (int, float))
              or not 0 <= value <= 1):
            raise ValueError(f"{key} 需为 0 到 1 之间的有限数值")
    show_mask = payload.get('show_mask', False)
    if not isinstance(show_mask, bool):
        raise ValueError("show_mask 必须为布尔值")
    try:
        retouch_params = {k: v for k, v in retouch.items() if k != 'brush_mask'}
        params = replace(ses['params'], **{key: float(value) for key, value in adjustments.items()},
                         **retouch_params)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("无效的调整参数") from exc
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

    preview_only = bool(payload.get("preview_only", False))
    out_dir = (THUMB_DIR / "edits" / payload["sid"] if preview_only else
               ses["folder"] / "photopilot_out" / payload["sid"])
    if output_destination is None:
        out_dir.mkdir(parents=True, exist_ok=True)

    do_color = ses.get("do_color", True)
    do_polish = ses.get("do_polish", True)
    region_masks = [] if preview_only and do_polish and show_mask else None
    wb_mode = ses.get("wb_mode", "off")
    wb_str = float(ses.get("wb_strength", 0.95))
    wb_gain = ses.get("wb_gains", {}).get(str(path))

    with PROCESS_LOCK:
        # One decode and one render. Face analysis runs on a downsampled copy,
        # then landmarks are mapped to the chosen export/preview resolution.
        full = load_image(path, max_size=preview_size if preview_only else ses.get("export_size", EXPORT_SIZE))
        work = resize_max(full, 1024)
        if wb_mode == "batch" and wb_gain is not None:
            work = apply_wb_gains(work, wb_gain, wb_str)
        elif wb_mode == "single" and wb_str > 0:
            from .polish import auto_white_balance
            work = auto_white_balance(work, wb_str)
        faces_w = _EDIT_FACE_POOL.submit(analyze_faces, work).result()
        faces_f = scale_faces(faces_w, full.shape[1] / work.shape[1])
        cur_ref = ses.get("scene_refs", {}).get(str(path), ses["reference"])
        if wb_mode in ("batch", "single"):
            params = replace(params, wb=0.0)
        if wb_mode == "batch" and wb_gain is not None:
            full = apply_wb_gains(full, wb_gain, wb_str)
        elif wb_mode == "single" and wb_str > 0:
            from .polish import auto_white_balance
            full = auto_white_balance(full, wb_str)
        graded = (match_color(full, cur_ref, algo=ses["algo"], strength=ses["strength"],
                              preserve_luma=ses["preserve_luma"], skin_protect=ses["skin_protect"],
                              faces=[f.box for f in faces_f]) if do_color and cur_ref is not None else full)
        warnings = []
        mask_options = {'region_masks': region_masks} if region_masks is not None else {}
        if brush_mask is not None:
            mask_options['brush_mask'] = brush_mask
        final = polish_image(graded, params, faces=faces_f, warnings=warnings,
                             **mask_options) if do_polish else graded

    output_name = ses.get("output_names", {}).get(
        str(path), f"{path.stem}_pp{output_suffix(path)}")
    # Preview URLs are immutable so a browser cannot display a stale prior edit.
    dst = (Path(output_destination) if output_destination is not None else
           out_dir / (uuid.uuid4().hex + ".jpg" if preview_only else output_name))
    save_image(final, dst, quality=ses.get("export_quality", 95),
               exif=extract_exif(path),
               icc_profile=extract_icc(path) or srgb_icc_profile())
    print(f"[process] 完成 {path.name}，耗时 {time.time()-started:.1f}s → {dst.name}")
    result = {"src": str(path), "out": str(dst), "preview_only": preview_only,
            "warnings": warnings,
            "width": final.shape[1], "height": final.shape[0],
            "before": _thumb_url(str(path), preview_size),
            "after": f"/api/img?p={urllib.parse.quote(str(dst))}"}
    if region_masks:
        overlay = np.empty((*final.shape[:2], 4), dtype=np.uint8)
        overlay[..., :3] = [0, 122, 255]
        overlay[..., 3] = (np.clip(region_masks[0], 0, 1) * 100).astype(np.uint8)
        mask_path = out_dir / (uuid.uuid4().hex + '.png')
        save_image(overlay, mask_path)
        result['region_mask'] = f"/api/img?p={urllib.parse.quote(str(mask_path))}"
    return result


def _on_export_job_finished(payload: dict) -> None:
    """导出任务到达终态时发系统通知（桌面模式下窗口可能被遮挡/最小化）。"""
    from .platform import notify
    total = payload.get('total', 0)
    failed = payload.get('failed', 0)
    if payload.get('state') == 'completed':
        notify('PhotoPilot', f'批量导出完成：共 {total} 张')
    else:
        notify('PhotoPilot', f'批量导出结束：成功 {total - failed} 张，失败 {failed} 张')


def _get_export_queue():
    global _EXPORT_QUEUE
    with _EXPORT_QUEUE_LOCK:
        if _EXPORT_QUEUE is None:
            _EXPORT_QUEUE = ExportQueue(renderer=_render_export,
                                       notifier=_on_export_job_finished)
        return _EXPORT_QUEUE


def _export_settings(value):
    """Validate small request scalars before decoding any user image."""
    allowed = {'folder', 'paths', 'operation', 'algo', 'strength', 'preserve_luma',
               'skin_protect', 'polish', 'export_size', 'export_quality', 'preset',
               'ref', 'scene_ref', 'color_off', 'wb_mode', 'wb_strength'}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError('导出设置结构无效')
    result = dict(value)
    def path(raw):
        if not isinstance(raw, str) or not raw or len(raw) > 4096 or '\x00' in raw or '://' in raw:
            raise ValueError('导出路径无效')
        # Lexical normalization deliberately does not stat sources: an already
        # acknowledged request remains replayable after its input was removed.
        return os.path.abspath(os.path.expanduser(raw))
    result['folder'] = path(value.get('folder', '.'))
    paths = value.get('paths')
    if not isinstance(paths, list) or not 1 <= len(paths) <= 20000:
        raise ValueError('请选择有效数量的照片')
    result['paths'] = list(dict.fromkeys(path(p) for p in paths))
    for key in ('ref',):
        if result.get(key):
            result[key] = path(result[key])
        elif key in result and result[key] not in (None, ''):
            raise ValueError('目标照片路径无效')
    choices = {'operation': {'color', 'polish', 'both'},
               'algo': {'none', 'oklab', 'reinhard', 'mkl', 'histogram', 'luma_hist'},
               'wb_mode': {'off', 'single', 'batch'}}
    for key, options in choices.items():
        if key in result and (not isinstance(result[key], str) or result[key] not in options):
            raise ValueError(f'导出设置 {key} 无效')
    if result.get('operation') == 'color' and not result.get('ref'):
        raise ValueError('追色需要先选择一张目标照片')
    for key in ('preserve_luma', 'scene_ref', 'color_off'):
        if key in result and type(result[key]) is not bool:
            raise ValueError(f'导出设置 {key} 需要布尔值')
    if result.get('skin_protect') is not None and type(result['skin_protect']) is not bool:
        raise ValueError('肤色保护需要布尔值')
    def number(raw, low, high):
        if type(raw) not in (int, float) or not math.isfinite(raw) or not low <= raw <= high:
            raise ValueError('导出参数数值超出范围')
    for key in ('strength', 'wb_strength'):
        if key in result:
            number(result[key], 0, 1)
    if isinstance(result.get('export_size'), str) and result['export_size'] in {'1024', '2048', '2560', '4096'}:
        result['export_size'] = int(result['export_size'])
    if 'export_size' in result and (result['export_size'] != 'original' and
            (type(result['export_size']) is not int or result['export_size'] not in {1024, 2048, 2560, 4096})):
        raise ValueError('导出尺寸无效')
    if 'export_quality' in result and (type(result['export_quality']) is not int or
                                      not 75 <= result['export_quality'] <= 100):
        raise ValueError('导出质量无效')
    if result.get('preset') is not None and (not isinstance(result['preset'], str) or
                                            result['preset'] not in PRESETS):
        raise ValueError('预设无效')
    polish = result.get('polish', {})
    allowed_polish = set(PolishParams.__dataclass_fields__)
    if not isinstance(polish, dict) or set(polish) - allowed_polish:
        raise ValueError('美化参数未知')
    tones = {'exposure', 'contrast', 'highlights', 'shadows', 'temperature', 'saturation'}
    for key, raw in polish.items():
        if key in {'eyes', 'auto_tone', 'global_skin'}:
            if type(raw) is not bool:
                raise ValueError('美化参数需要布尔值')
        elif key == 'local_region':
            if not isinstance(raw, str) or raw not in {'all', 'global', '全图', 'skin', 'face', 'eyes', 'background'}:
                raise ValueError('局部区域无效')
        else:
            limit = 3 if key == 'exposure' else 1
            number(raw, -limit if key in tones else 0, limit)
    return result


def _source_fingerprint(path):
    p = Path(path)
    st = p.stat()
    if not p.is_file():
        raise ValueError('源照片不是文件')
    return {'size': st.st_size, 'mtime_ns': st.st_mtime_ns}


def _submit_export(payload):
    if not isinstance(payload, dict) or set(payload) != {'request_id', 'settings'}:
        raise ValueError('导出请求结构无效')
    settings = _export_settings(payload['settings'])
    queue = _get_export_queue()
    with _EXPORT_SUBMIT_LOCK:
        prior = queue.lookup_request(payload['request_id'], settings)
        if prior is not None:
            return {'job': _public_export_job(prior)}
        prepared = dict(settings)
        prepared['paths'] = list(dict.fromkeys(str(Path(p).resolve()) for p in settings['paths']))
        prepared['folder'] = str(Path(settings['folder']).resolve())
        if not Path(prepared['folder']).is_dir():
            raise ValueError('导出目录不存在')
        if any(not is_image(p) for p in prepared['paths']):
            raise ValueError('源照片类型不支持')
        sources = {p: _source_fingerprint(p) for p in prepared['paths']}
        reference = settings.get('ref')
        ref_before = _source_fingerprint(reference) if reference else None
        session = _prepare(prepared, retain_session=False)['session']
        if any(_source_fingerprint(p) != fp for p, fp in sources.items()):
            raise ValueError('准备期间源照片发生变化，请重新提交')
        if reference and _source_fingerprint(reference) != ref_before:
            raise ValueError('准备期间目标照片发生变化，请重新提交')
        recipe = {'submission': settings, 'session': freeze_session(session), 'sources': sources}
        return {'job': _public_export_job(queue.submit(recipe, prepared['paths'], payload['request_id']))}


def _posix_directory_fd_available() -> bool:
    """Whether the directory-relative no-follow publication APIs are available."""
    return (os.name == 'posix'
            and hasattr(os, 'O_DIRECTORY')
            and hasattr(os, 'O_NOFOLLOW')
            and hasattr(os, 'link')
            and hasattr(os, 'unlink'))


def _posix_publish_export(folder: Path, job_id: str, name: str,
                          rendered: Path) -> Path:
    """Atomically publish without following or overwriting output paths."""
    destination = folder / 'photopilot_out' / job_id / name
    descriptors = []
    stage = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        folder_fd = os.open(folder, flags)
        descriptors.append(folder_fd)
        for component in ('photopilot_out', job_id):
            try:
                os.mkdir(component, mode=0o700, dir_fd=folder_fd)
            except FileExistsError:
                pass
            folder_fd = os.open(component, flags, dir_fd=folder_fd)
            descriptors.append(folder_fd)
        stage = '.export-' + uuid.uuid4().hex + Path(name).suffix
        fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=folder_fd)
        with os.fdopen(fd, 'wb') as target, rendered.open('rb') as source:
            while block := source.read(1024 * 1024):
                target.write(block)
            target.flush()
            os.fsync(target.fileno())
        if folder.resolve() != folder or destination.parent.resolve() != destination.parent:
            raise ValueError('导出目录发生变化')
        current = destination.parent.stat()
        opened = os.fstat(folder_fd)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError('导出目录发生变化')
        # link(2) provides atomic no-clobber publication: it fails if the final
        # name already exists. The stage and final name share one directory.
        os.link(stage, name, src_dir_fd=folder_fd, dst_dir_fd=folder_fd,
                follow_symlinks=False)
        os.fsync(folder_fd)
    finally:
        if stage is not None:
            try:
                os.unlink(stage, dir_fd=descriptors[-1])
            except FileNotFoundError:
                pass
        for fd in reversed(descriptors):
            os.close(fd)
    return destination


def _windows_publish_export(folder: Path, job_id: str, name: str,
                            rendered: Path, *, no_clobber: bool = False) -> Path:
    """Publish on Windows without POSIX dir_fd APIs or clobbering a finished file.

    Python on Windows lacks ``dir_fd`` support and ``O_NOFOLLOW``. Resolve and
    re-check every directory before creating each component, then use exclusive
    creation for the final destination. The check-then-create sequence is the
    strongest portable guarantee available here; a concurrent attacker that can
    replace directories must also have write access to the user's export folder.
    """
    if not folder.is_absolute() or folder.resolve(strict=True) != folder:
        raise ValueError('导出目录发生变化')
    output_root = folder / 'photopilot_out'
    job_root = output_root / job_id
    for directory in (output_root, job_root):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError('导出目录发生变化')
        if directory.resolve() != directory:
            raise ValueError('导出目录发生变化')
    destination = job_root / name
    stage = job_root / ('.export-' + uuid.uuid4().hex + Path(name).suffix)
    try:
        with rendered.open('rb') as source, stage.open('xb') as target:
            while block := source.read(1024 * 1024):
                target.write(block)
            target.flush()
            os.fsync(target.fileno())
        # Windows Python's os.rename conventionally replaces an existing file.
        # Callers can require an exclusive final name for extra Windows safety.
        if job_root.resolve() != job_root or destination.parent.resolve() != destination.parent:
            raise ValueError('导出目录发生变化')
        if no_clobber:
            with stage.open('rb') as source, destination.open('xb') as target:
                while block := source.read(1024 * 1024):
                    target.write(block)
                target.flush()
                os.fsync(target.fileno())
        else:
            os.rename(stage, destination)
    finally:
        stage.unlink(missing_ok=True)
    return destination


def _publish_export(folder: Path, job_id: str, name: str, rendered: Path) -> Path:
    if _posix_directory_fd_available():
        return _posix_publish_export(folder, job_id, name, rendered)
    return _windows_publish_export(folder, job_id, name, rendered, no_clobber=True)


def _render_export(job, item):
    """Decode frozen settings, then publish with no-clobber I/O."""
    jid, recipe, path = job['id'], job['recipe'], item['path']
    if not isinstance(jid, str) or len(jid) != 32 or any(c not in '0123456789abcdef' for c in jid):
        raise ValueError('导出任务标识无效')
    ses = thaw_session(recipe['session'])
    expected = recipe.get('sources', {}).get(path)
    def check_source():
        if not isinstance(expected, dict) or _source_fingerprint(path) != expected:
            raise ValueError('源照片已改变，请新建任务')
    check_source()
    name = ses['output_names'].get(path)
    if name is None:
        raise ValueError('照片不在导出配方中')
    # Render inside a private temporary directory, never follow a replaced
    # output-directory symlink while the encoder is opening its target path.
    with tempfile.TemporaryDirectory(prefix='photopilot-export-') as temp:
        rendered = _render_session(ses, {'sid': jid, 'path': path},
                                   output_destination=Path(temp) / name)
        check_source()
        destination = _publish_export(ses['folder'], jid, name, Path(rendered['out']))
        check_source()
    return {'src': path, 'out': str(destination), 'width': rendered['width'], 'height': rendered['height']}


def _public_export_job(job, *, summary=False, offset=0, limit=50):
    public = {key: value for key, value in job.items() if key in
              {'id', 'state', 'created_at', 'updated_at', 'counts', 'queue_error'}}
    if summary:
        return public
    items = job.get('items', [])
    paged = 'items_total' in job
    public.update(items=[], items_total=job.get('items_total', len(items)),
                  offset=job.get('offset', offset))
    for raw in (items if paged else items[offset:offset + limit]):
        item = {key: value for key, value in raw.items() if key in {'path', 'index', 'state', 'error'}}
        result = raw.get('result')
        if isinstance(result, dict):
            out = result.get('out')
            valid = False
            try:
                queue = _get_export_queue()
                folder = Path(queue.get_recipe_field(job['id'], 'session', 'folder'))
                name = queue.get_recipe_field(job['id'], 'session', 'output_names', raw['path'])
                jid = job['id']
                if (isinstance(jid, str) and len(jid) == 32
                        and all(c in '0123456789abcdef' for c in jid)
                        and isinstance(name, str) and name not in {'', '.', '..'}
                        and Path(name).name == name and '\x00' not in name
                        and folder.is_absolute() and folder.resolve() == folder):
                    expected = folder / 'photopilot_out' / jid / name
                    valid = (isinstance(out, str) and '\x00' not in out
                             and out == str(expected) and out != raw['path']
                             and expected.resolve() == expected
                             and not any(p.is_symlink() for p in (expected, *expected.parents)))
            except (ValueError, TypeError, OSError, RuntimeError):
                pass
            if valid:
                item['result'] = {'src': raw['path'], 'out': out,
                    'width': result.get('width'), 'height': result.get('height'),
                    'before': _thumb_url(raw['path'], 640), 'after': _thumb_url(out, 640),
                    'download': '/api/img?p=' + urllib.parse.quote(out)}
            else:
                item['error'] = '导出结果路径无效或目录已改变，请重新导出'
        public['items'].append(item)
    return public


def _import_paths(payload: dict) -> dict:
    """桌面模式：把本机已有照片/文件夹直接登记为批次（不拷贝文件，秒级），
    并在服务端一步启动扫描任务（返回 job，前端只轮询）。"""
    paths = [str(Path(p).expanduser().absolute())
             for p in payload.get("paths", []) if str(p).strip()]
    files, folders, source_dirs = [], [], []
    for p in paths:
        if Path(p).is_dir():
            folders.append(p)
            source_dirs.append(p)
        elif Path(p).is_file() and is_image(p):
            files.append(p)
            source_dirs.append(str(Path(p).parent))
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
    scan_dirs = list(dict.fromkeys(source_dirs))
    if not folders:
        scan_dirs = scan_dirs[:3]
    scan_payload = {"jobs": 4,
                    "similarity_profile": payload.get('similarity_profile', 'balanced'),
                    "faces": bool(payload.get("faces", True)),
                    "ai": bool(payload.get("ai", True)),
                    "recursive": bool(payload.get("recursive", False))}
    # 始终使用解析后的文件清单：多文件夹选择不能只把第一个目录交给扫描器，
    # 也避免扫描用户未选择、但后来出现在目录里的新文件。
    scan_payload["folder"] = scan_dirs[0]
    scan_payload["paths"] = files
    job = _scan_start(scan_payload)
    print(f"[import] 批次 {batch}：{len(files)} 张，扫描任务 {job['job']} 已启动")
    return {"batch": batch, "count": len(files), "folder": job["folder"], "folders": folders,
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

    def _allow_local_request(self) -> bool:
        """Block remote web origins and DNS rebinding against file-access APIs."""
        try:
            host = urllib.parse.urlsplit('http://' + self.headers.get('Host', ''))
            port = self.server.server_port
            allowed = (host.hostname in {'127.0.0.1', 'localhost', '::1'}
                       and (host.port or 80) == port and not host.username
                       and not host.path and not host.query and not host.fragment)
            origin_header = self.headers.get('Origin')
            if origin_header:
                origin = urllib.parse.urlsplit(origin_header)
                allowed = allowed and (origin.scheme == 'http'
                           and origin.hostname == host.hostname
                           and (origin.port or 80) == port and not origin.username
                           and not origin.path and not origin.query and not origin.fragment)
        except (ValueError, AttributeError):
            allowed = False
        if not allowed:
            self._json({'error': '仅允许本机 PhotoPilot 界面访问'}, 403)
        return allowed

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
        if not self._allow_local_request():
            return
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
            elif u.path == "/api/exif":
                # 灯箱拍摄参数面板：按需解析源图 EXIF（不含 RAW，见 exif_summary）
                p = Path(q["p"][0])
                if not p.is_file():
                    raise FileNotFoundError(p)
                self._json({"exif": exif_summary(p)})
            elif u.path == "/api/presets":
                self._json({"presets": [{"name": name, **PRESET_META.get(name, {})}
                                         for name in PRESETS]})
            elif u.path == "/api/scan_status":
                self._json(_scan_status(q))
            elif u.path == '/api/regroup_status':
                self._json(SIMILARITY_JOBS.status(q.get('job', [''])[0]))
            elif u.path == "/api/project":
                self._json(get_project_store().load())
            elif u.path == "/api/exports":
                queue = _get_export_queue()
                self._json({'jobs': [_public_export_job(job, summary=True) for job in queue.list(summaries=True)],
                            'queue_error': queue.error})
            elif u.path == "/api/export":
                offset = int(q.get('offset', ['0'])[0])
                limit = int(q.get('limit', ['50'])[0])
                if offset < 0 or not 1 <= limit <= 100:
                    raise ValueError('导出分页范围无效')
                self._json({'job': _public_export_job(_get_export_queue().get(q.get('id', [''])[0],
                                                      offset=offset, limit=limit))})
            else:
                self._json({"error": "not found"}, 404)
        except FileNotFoundError as e:
            self._json({"error": f"文件不存在：{e}"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 400)

    def do_POST(self):
        if not self._allow_local_request():
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n < 0:
                raise ValueError("Content-Length must be nonnegative")
            if self.path == "/api/project" and n > MAX_PROJECT_BYTES:
                self._json({"error": "project checkpoint exceeds size limit"}, 413)
                return
            if self.path == '/api/regroup' and n > MAX_REGROUP_BYTES:
                self._json({'error': '相似识别请求过大，请缩小照片范围'}, 413)
                return
            if self.path in {'/api/exports', '/api/export_action'} and n > MAX_EXPORT_REQUEST_BYTES:
                self._json({'error': 'export request exceeds size limit'}, 413)
                return
            if self.path == '/api/trash' and n > MAX_TRASH_REQUEST_BYTES:
                self._json({'error': 'trash request exceeds size limit'}, 413)
                return
            body = self.rfile.read(n) if n else b""
            if self.path == "/api/project":
                self._json(get_project_store().save(json.loads(body or b"{}")))
            elif self.path == "/api/upload":
                _upload(self, body)
            elif self.path == "/api/scan":
                payload = json.loads(body or b"{}")
                result = _scan_start(payload)
                self._json(result)
            elif self.path == '/api/regroup':
                payload = json.loads(body or b'{}')
                if not isinstance(payload, dict) or set(payload) - {'photos', 'similarity_profile'}:
                    raise ValueError('相似识别请求结构无效')
                self._json(SIMILARITY_JOBS.start(payload.get('photos'), payload.get('similarity_profile', 'balanced')))
            elif self.path == "/api/scan_cancel":
                self._json(_scan_cancel(json.loads(body or b"{}")))
            elif self.path == "/api/import_paths":
                self._json(_import_paths(json.loads(body or b"{}")))
            elif self.path == "/api/rate":
                self._json(_rate(json.loads(body or b"{}")))
            elif self.path == '/api/trash':
                self._json(_trash_photos(json.loads(body or b'{}')))
            elif self.path == "/api/prepare":
                self._json(_prepare(json.loads(body or b"{}")))
            elif self.path == "/api/process_one":
                self._json(_process_one(json.loads(body or b"{}")))
            elif self.path == "/api/exports":
                self._json(_submit_export(json.loads(body or b"{}")))
            elif self.path == "/api/export_action":
                payload = json.loads(body or b'{}')
                if not isinstance(payload, dict) or set(payload) != {'id', 'action'}:
                    raise ValueError('导出操作结构无效')
                job = _get_export_queue().action(payload['id'], payload['action'])
                self._json({'job': _public_export_job(job)})
            elif self.path == "/api/reveal":
                payload = json.loads(body or b'{}')
                if not isinstance(payload, dict) or not isinstance(payload.get('path'), str) \
                        or not payload['path'] or '\x00' in payload['path']:
                    raise ValueError('路径无效')
                p = Path(payload['path']).expanduser()
                if not p.exists():
                    raise ValueError(f'文件不存在：{p}')
                _reveal_in_file_manager(str(p))
                self._json({'ok': True})
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
body{margin:0;display:flex;flex-direction:column;background:var(--bg);color:var(--fg);overflow:hidden;
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
  flex:none;padding:10px 16px;background:var(--panel);backdrop-filter:saturate(180%) blur(24px);
  -webkit-backdrop-filter:saturate(180%) blur(24px);border-bottom:1px solid var(--line)}
.brandMark{width:28px;height:28px;display:grid;place-items:center;flex:none;border-radius:8px;
  background:linear-gradient(145deg,#0a84ff,#5e5ce6);color:#fff;font-size:11px;font-weight:800;
  letter-spacing:-.04em;box-shadow:0 3px 10px rgba(10,132,255,.24)}
header h1{font-size:17px;font-weight:700;letter-spacing:-.02em;margin:0 10px 0 0}
header h1 span{color:var(--acc)}
#folder{flex:1;min-width:100px;padding:8px 14px;border-radius:11px;border:1px solid var(--line);
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
main{display:flex;flex:1;min-height:0;position:relative;z-index:1}
#center{flex:1;display:flex;flex-direction:column;min-width:0;min-height:0}
#toolbar{display:flex;flex-direction:column;flex:none;gap:4px;padding:4px 16px;border-bottom:1px solid var(--line);
  background:rgba(255,255,255,.16)}
.toolbarRow{display:flex;align-items:center;gap:10px;min-width:0;flex-wrap:wrap}
.toolbarRow.primary{min-height:34px;gap:8px}
.toolbarRow.secondary{padding-top:1px}
.batchRow{padding:8px 10px;border-top:1px solid var(--line);gap:8px}
.batchRow .toolSelect{display:flex;align-items:center;gap:6px;color:var(--dim);font-size:12px;white-space:nowrap}
.batchRow .toolSelect select{width:auto;min-width:96px;padding:6px 8px;font-size:12px;border-radius:8px}
.batchSelectionCount{font-size:12px;color:var(--dim);font-variant-numeric:tabular-nums;margin-right:4px}
.batchHint{font-size:11px;color:var(--dim);margin-left:auto}
.btn.trashAction{background:var(--panel-solid);color:#b42318;border:1px solid rgba(180,35,24,.24);box-shadow:none}
.btn.trashAction:hover{background:rgba(180,35,24,.08);box-shadow:none}
@media(prefers-color-scheme:dark){.btn.trashAction{color:#ff8a80;border-color:rgba(255,138,128,.3)}}
.projectRow{font-size:12px;color:var(--dim);gap:8px}
#projectStatus{--project-warning:#8a4a00;flex:1 1 100px;min-width:0;max-height:32px;overflow:auto;line-height:1.35;white-space:pre-wrap;overflow-wrap:anywhere}
#projectStatus[data-state="error"],#projectStatus[data-state="blocked"]{color:var(--project-warning)}
@media(prefers-color-scheme:dark){#projectStatus{--project-warning:#ffc36b}}
.toolbarKicker{color:var(--fg);font-weight:700;font-size:12px}
.toolGroup{display:flex;align-items:center;gap:8px;min-width:0;padding-left:12px;border-left:1px solid var(--line)}
.toolGroupLabel{color:var(--dim);font-size:11px;font-weight:700;letter-spacing:.04em;white-space:nowrap}
#toolbar .toolSelect{display:flex;align-items:center;gap:5px;color:var(--dim);font-size:11px;white-space:nowrap;flex:none}
#toolbar .toolSelect select{width:auto;min-width:78px;padding:6px 8px;font-size:12px;border-radius:8px}
.sizeFilters{padding:10px 0;border-top:1px solid var(--line);margin-top:10px}
#reviewBar{display:flex;align-items:center;flex-wrap:wrap;gap:4px 12px;min-width:0}
#reviewTools{font-size:12px;flex:none;max-height:45vh;overflow:auto}
#reviewTools[open]{flex:1 1 100%}
#reviewTools>summary{cursor:pointer;padding:5px 0;color:var(--fg);font-weight:600;white-space:nowrap}
#reviewTools[open]>.secondary{padding:8px 0}
.projectRow{flex:1 1 240px;min-width:0;margin-left:auto;padding:0;justify-content:flex-end;flex-wrap:nowrap}
.projectRow .btn{font-size:12px;padding:5px 10px}
#regroupStatus{font-size:12px;color:var(--fg);font-variant-numeric:tabular-nums}
#side[hidden]{display:none}
.card:focus-visible{outline:2px solid var(--acc);outline-offset:3px}
.sizeFilters summary{cursor:pointer;font-size:13px;font-weight:600;padding:4px 0}
.sizeFilterFields{display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin-top:10px}
.sizeFilterFields fieldset{border:0;padding:0;margin:0;display:flex;align-items:center;gap:6px;min-width:0}
.sizeFilterFields legend{float:left;margin-right:8px;font-size:12px}
.sizeFilterFields input{width:90px;max-width:22vw;padding:7px 8px;border:1px solid var(--line);border-radius:6px;background:var(--panel-solid);color:var(--fg)}
.sizeFilterFields input:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
#sizeFilterStatus{font-size:12px;line-height:1.6;margin:8px 0 0;color:var(--fg)}
#filterEmpty{padding:28px 20px;text-align:center;line-height:1.7}
#filterEmpty[hidden]{display:none}
#toolbar .toolbarActions{display:flex;align-items:center;gap:8px;margin-left:auto;flex:none}
#toolbar .toolbarActions .btn{padding-left:12px;padding-right:12px}
#viewbar{display:flex;align-items:center;gap:2px;padding:2px;border:1px solid var(--line);
  border-radius:9px;background:var(--track);flex:none}
#viewbar button{border:0;border-radius:7px;background:transparent;color:var(--dim);cursor:pointer;
  padding:5px 9px;font-size:12px;font-weight:650;line-height:1;transition:background .2s,color .2s,box-shadow .2s}
#viewbar button:hover{color:var(--fg)}
#viewbar button.on{background:var(--panel-solid);color:var(--fg);box-shadow:0 1px 4px rgba(0,0,0,.14)}
#scanInfo{white-space:nowrap;font-variant-numeric:tabular-nums;max-width:36vw;overflow:hidden;text-overflow:ellipsis;font-size:12px}
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
#grid{--tile-min:250px;--thumb-min:190px;flex:1;min-height:0;overflow-y:auto;padding:12px 16px 24px;
  display:grid;grid-template-columns:repeat(auto-fill,minmax(var(--tile-min),1fr));
  grid-auto-rows:minmax(calc(var(--thumb-min) + 96px),auto);gap:16px;align-content:start;align-items:start}
#grid[data-density="compact"]{--tile-min:190px;--thumb-min:140px;gap:14px}
#grid[data-density="large"]{--tile-min:300px;--thumb-min:208px;gap:20px}
#grid[data-layout="groups"]{display:block}
.photoGroup{margin:0 0 32px;min-width:0;border-top:1px solid var(--track)}
.groupHeader{display:flex;align-items:center;flex-wrap:wrap;gap:8px 16px;padding:8px 0;
  position:static;background:var(--bg)}
.groupHeading{display:flex;align-items:baseline;flex-wrap:wrap;gap:8px 12px;flex:1;min-width:0}
.groupHeading h3{font-size:14px;margin:0;font-weight:700}
.groupSummary{font-size:12px;color:var(--fg);font-variant-numeric:tabular-nums}
.groupActions{display:flex;flex-wrap:wrap;gap:8px}
.groupActions .btn{font-size:12px;padding:6px 10px;border-radius:8px}
.groupPhotos{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,var(--tile-min)),1fr));
  gap:18px;align-items:start}
.groupPhotos[hidden]{display:none}
#grid[data-density="compact"] .groupPhotos{gap:14px}
#grid[data-density="large"] .groupPhotos{gap:20px}
.groupRecommended{position:absolute;top:44px;left:8px;padding:3px 8px;border-radius:6px;
  color:var(--fg);background:var(--panel-solid);font-size:11px;font-weight:650;pointer-events:none}
#grid[data-layout="groups"] .compareBtn{display:none}
.groupActions .btn:focus-visible{outline:2px solid var(--acc);outline-offset:3px}
#groupNav{padding:0;gap:6px;flex:1 1 auto}
#groupNav[hidden]{display:none}
#groupPosition{font-size:12px;min-width:110px;color:var(--fg);font-variant-numeric:tabular-nums}
#groupNav .btn{font-size:12px;padding:4px 8px}
.reviewKeyHint{font-size:11px;color:var(--dim);margin-left:auto;white-space:nowrap}
@media(max-width:1200px){.reviewKeyHint{display:none}}
.card{position:relative;background:var(--panel-solid);border-radius:12px;overflow:hidden;cursor:pointer;
  border:2.5px solid transparent;box-shadow:var(--shadow);
  display:block;width:100%;height:auto;min-width:0;min-height:calc(var(--thumb-min) + 96px);
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
.badges{display:flex;gap:4px;align-items:center;flex-wrap:wrap;min-width:0}
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
.meta{padding:8px 10px;display:flex;flex-direction:column;gap:6px;min-height:96px}
.cardIdentity{display:flex;align-items:center;gap:8px;min-width:0;width:100%}
.cardIdentity .name{flex:1;min-width:0}
.ratingSummary{flex:none;color:#805200;font-size:11px;font-weight:650;white-space:nowrap}
.ratingSummary.unrated{color:var(--dim);font-weight:400}
.cardQuality{display:flex;align-items:center;gap:8px;min-width:0}
.cardQuality .flags{flex:1}
.card .pill{box-shadow:none;backdrop-filter:none;-webkit-backdrop-filter:none}
@media(prefers-color-scheme:dark){.ratingSummary{color:#ffd36b}.ratingSummary.unrated{color:var(--dim)}}
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
.name{font-size:12px;color:var(--fg);margin:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
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
#side{width:308px;flex:none;margin:14px 14px 14px 0;border-radius:12px;padding:18px;
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
.chk input{position:absolute;width:1px;height:1px;opacity:0;clip-path:inset(50%)}
.chk input:focus-visible+.sw{outline:2px solid var(--acc);outline-offset:3px}
.sw{width:40px;height:24px;border-radius:12px;background:var(--track);position:relative;flex:none;
  transition:background .25s}
.sw::after{content:'';position:absolute;top:2px;left:2px;width:20px;height:20px;border-radius:50%;
  background:#fff;box-shadow:0 1px 4px rgba(0,0,0,.25);transition:transform .3s var(--ease-interactive)}
.chk input:checked+.sw{background:var(--good)}
.chk input:checked+.sw::after{transform:translateX(16px)}
.autoToneRow{margin:10px 0 4px;font-weight:650}
.subhint{color:var(--dim);font-size:11px;line-height:1.4;margin:-1px 0 10px 50px}
.toneActions{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}
.toneActions .btn{font-size:12px;padding:6px 10px}
:focus-visible{outline:2px solid var(--acc);outline-offset:3px}
.chk input:disabled~.sw{opacity:.4}
#actionGroup{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:14px}
#actionGroup .btn{width:100%;margin:0}
#actionHint{color:var(--dim);font-size:11.5px;line-height:1.45;margin-top:8px;text-align:center}
#targetPanel{display:flex;gap:8px;align-items:center;margin-bottom:12px}
#targetInfo{flex:1;min-width:0;color:var(--dim);font-size:12px;line-height:1.35}
#targetInfo.on{color:var(--fg);font-weight:600}
#clearTarget{flex:none;padding:6px 9px;font-size:11px}
#status{margin-top:10px;color:var(--dim);font-size:12px;min-height:16px;text-align:center}
#exportQueue{margin-top:22px;padding-top:16px;border-top:1px solid var(--line)}
#exportQueue h3{font-size:14px;margin:0 0 8px}
#exportQueue .queueHint{font-size:12px;color:var(--dim);line-height:1.5;margin:8px 0;overflow-wrap:anywhere}
#exportQueueError{color:var(--bad);font-size:12px;line-height:1.5;overflow-wrap:anywhere}
.exportJob{padding:12px 0;border-bottom:1px solid var(--line);min-width:0}
.exportJobTitle{font-size:13px;font-weight:650;margin-bottom:5px}
.exportJobCounts,.exportJobFolder{font-size:12px;line-height:1.5;overflow-wrap:anywhere}
.exportJobFolder{color:var(--dim)}
.exportJobActions{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.exportJobActions .btn{padding:6px 8px;font-size:12px;white-space:nowrap}
.exportReviewNav{display:flex;gap:12px;align-items:center;justify-content:center;color:#fff;margin:16px 0}

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
.pair{background:var(--panel-solid);border-radius:12px;padding:12px;margin-bottom:16px;
  box-shadow:var(--shadow-lg);animation:cardIn .55s var(--ease) both}
#results.single-result .pair{display:flex;flex:1;min-height:0;flex-direction:column;margin:0;padding:10px}
.cmp{--pos:50%;position:relative;border-radius:14px;overflow:hidden;user-select:none;touch-action:none;cursor:ew-resize}
.cmp img{width:100%;display:block;pointer-events:none}
#results.single-result .cmp{display:flex;flex:1;min-height:0;align-items:center;justify-content:center;background:#101216}
#results.single-result .cmp img{width:100%;height:100%;object-fit:contain}
#results.single-result .cmp img.b{width:100%;height:100%;object-fit:contain}
#results.single-result .pair>.name{flex:none}
.editorLayout{display:grid;grid-template-columns:minmax(0,1fr) 270px;gap:16px;flex:1;min-height:0}
.editorTools{--dim:#636366;background:var(--panel-solid);padding:18px;border-radius:12px;display:flex;flex-direction:column;min-height:0}
@media(prefers-color-scheme:dark){.editorTools{--dim:#aeaeb2}}
.editorControls{overflow-y:auto;min-height:0;flex:1;padding:0 2px}
.editorFooter{flex-shrink:0;padding-top:10px;border-top:1px solid var(--line)}
.editorTabs{display:flex;gap:4px;margin-bottom:12px;position:sticky;top:0;z-index:1;background:var(--panel-solid)}
.editorTools .editorTabs .btn{width:auto;flex:1;margin:0;padding:8px 5px;font-size:12px}
.editorTabs [aria-selected="true"]{background:var(--acc);color:#fff}
.editorTools [hidden]{display:none}
.editorTools select{width:100%}
.editorMaskToggle{display:flex;align-items:center;gap:8px;margin:12px 0;font-size:13px}
.editorMaskToggle input{width:auto}
.cmp img.region-mask{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;pointer-events:none}
.cmp img.region-mask[hidden]{display:none}
.cmp canvas.brush-canvas{position:absolute;left:0;top:0;pointer-events:auto;cursor:crosshair;touch-action:none;z-index:3}
.cmp canvas.brush-canvas[hidden]{display:none}
.cmp.brush-active{cursor:crosshair}
.brushActions{display:flex;gap:6px;margin:8px 0}
.brushActions .btn{flex:1;margin:0;padding:7px 4px;font-size:12px}
.brushActions .btn.on{background:var(--acc);color:#fff}
.editorTools h3{margin:0 0 16px;font-size:15px}
.editorTools .btn{width:100%;margin-top:10px}
.editorHistory{display:flex;gap:8px;margin-bottom:16px}
.editorTools .editorHistory .btn{flex:1;width:auto;margin:0;padding:8px 10px}
.editorTools .subhint{margin:10px 0;line-height:1.6}
#editWarnings,.regionHint{color:var(--fg);font-size:12px}
.regionHint{line-height:1.5;margin:8px 0 14px}
#editStatus{font-size:12px;overflow-wrap:anywhere;margin-top:12px;min-height:36px}
#editStatus a{color:var(--acc)}
@media(max-width:900px){.editorLayout{grid-template-columns:minmax(0,1fr) 220px}.editorTools{padding:12px}}
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
#lbImg{display:block;width:auto;height:auto;max-width:100%;max-height:100%;object-fit:contain;border-radius:4px;box-shadow:0 12px 60px rgba(0,0,0,.55);
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
#lbExif{color:var(--dim);font-size:12.5px;font-variant-numeric:tabular-nums;display:flex;gap:10px 16px;flex-wrap:wrap;justify-content:center;align-items:center;width:100%}
#lbExif b{color:#fff;font-weight:600}
#lbExif .exsep{color:rgba(255,255,255,.18)}
#lbExif .exgps{color:var(--accent);cursor:pointer}
#lbExif .exgps:hover{text-decoration:underline}
.lbKey{display:inline-block;padding:1px 7px;border-radius:6px;background:rgba(255,255,255,.14);
  color:#fff;font-size:11px;font-weight:600;margin:0 2px}
.card.lbcur{border-color:#ffd60a;box-shadow:0 0 0 4px rgba(255,214,10,.22),var(--shadow-lg)}
.pill.rstar{color:#9a6b00}
.pill.ai{color:#6941c6}
.starBtn,.compareBtn{position:absolute;z-index:3;border:1px solid rgba(255,255,255,.72);cursor:pointer;
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);font-weight:700;transition:transform .18s var(--ease-interactive),background .2s}
.starBtn{top:9px;right:9px;min-width:36px;height:27px;padding:0 5px;border-radius:8px;background:rgba(20,24,32,.62);color:rgba(255,255,255,.85);font-size:13px;line-height:1}
.starBtn.on{background:#ffd60a;border-color:#ffd60a;color:#3b2b00}
.starBtn:hover,.compareBtn:hover{transform:scale(1.08);background:var(--acc);color:#fff;border-color:var(--acc)}
.ratingMenu{position:absolute;z-index:4;top:42px;right:8px;display:flex;align-items:center;gap:3px;
  max-width:calc(100% - 16px);padding:5px;background:var(--panel-solid);border:1px solid var(--line);
  border-radius:9px;box-shadow:var(--shadow-lg)}
.ratingMenu[hidden]{display:none}
.ratingMenu button{border:0;background:transparent;color:#a66b00;cursor:pointer;font-size:12px;font-weight:650;
  padding:4px 5px;border-radius:6px;white-space:nowrap}
.ratingMenu button:hover,.ratingMenu button[aria-pressed="true"]{background:rgba(255,214,10,.2);color:#754900}
.ratingMenu button.clearRating{color:var(--dim);border-left:1px solid var(--line);padding-left:8px}
.starBtn:focus-visible,.ratingMenu button:focus-visible{outline:2px solid var(--acc2);outline-offset:2px}
.compareBtn{position:static;align-self:flex-end;flex:none;border-radius:7px;padding:4px 8px;background:rgba(20,24,32,.68);color:#fff;font-size:10.5px}
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
#compareControls{display:flex;align-items:center;flex-wrap:wrap;gap:10px;padding:0 22px 12px;color:var(--dim);font-size:12px}
#compareControls select{width:auto;padding:5px 8px}
#compareActiveName{max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#fff}
#compareZoom{width:180px}
#compareZoomValue{font-variant-numeric:tabular-nums;color:#fff;min-width:30px}
#compareGrid{flex:1;min-height:0;display:grid;grid-template-columns:repeat(var(--compare-cols,4),minmax(0,1fr));gap:12px;padding:0 22px 22px;align-items:stretch}
.comparePane{position:relative;min-width:0;min-height:180px;border-radius:14px;overflow:hidden;background:#1c1c22;display:flex;align-items:center;justify-content:center}
.comparePane{touch-action:none;cursor:grab}
.comparePane img{max-width:none;max-height:none;flex:none;object-fit:contain;transform-origin:center;user-select:none;pointer-events:none}
.comparePane.active{outline:2px solid #0a84ff;outline-offset:-2px}
.compareImageStatus{position:absolute;inset:0;display:grid;place-items:center;padding:20px;color:#fff;text-align:center}
.compareImageStatus[hidden]{display:none}
.comparePane .compareMeta{position:absolute;left:8px;right:8px;bottom:8px;display:flex;align-items:center;gap:6px;color:#fff;font-size:11px;text-shadow:0 1px 3px #000}
.comparePane .compareMeta span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.comparePane .compareStar{border:0;background:rgba(0,0,0,.5);border-radius:99px;color:#fff;padding:3px 7px;cursor:pointer}
.comparePane .compareStar.on{color:#ffd60a}
@media(max-width:900px){#compareGrid{grid-template-columns:repeat(2,minmax(0,1fr));overflow-y:auto}.comparePane{min-height:220px}}
@media(max-width:900px){#side{position:absolute;right:0;top:0;bottom:0;z-index:25;max-width:calc(100% - 24px)}header{gap:6px;padding:8px}header h1{margin-right:0}#scanInfo{max-width:20vw}}
@media(max-width:560px){header{flex-wrap:wrap}#folder{order:2;flex-basis:100%}#toolbar .toolGroup{flex-wrap:wrap}#compareGrid{grid-template-columns:1fr}.comparePane{min-height:260px}}

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
    accept="image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp,.tif,.tiff,.bmp,.arw,.cr2,.cr3,.nef,.nrw,.dng,.raf,.orf,.rw2,.pef,.srw,.x3f"
    onchange="uploadFiles(this.files);this.value=''">
</header>
<main>
<div id="center">
  <div id="toolbar">
    <div class="toolbarRow primary">
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
      <button class="btn ghost" id="regroupButton" type="button" onclick="regroupVisiblePhotos()" title="利用已有预览重建相似组，不重新评分、不改变选择和星级">识别相似</button>
      <button class="btn ghost" id="sideToggle" type="button" aria-controls="side" aria-expanded="false" onclick="setSidebarOpen($('side').hidden,true)">处理参数</button>
      <span class="reviewKeyHint">空格 开关快览 · 1–5 加星</span>
    </div>
    <div id="reviewBar">
    <details id="reviewTools"><summary title="大小过滤 / 扫描选项 / 加星与删除">筛选与批量操作</summary>
    <details class="sizeFilters" id="sizeFilters">
      <summary>照片大小过滤 · 排除图标和小图</summary>
      <div class="sizeFilterFields">
        <fieldset><legend>原图短边（px）</legend>
          <input id="sizeMinEdge" type="number" value="" min="0" max="200000" step="1" placeholder="不限" aria-label="原图短边下限，像素">
          <span>至</span><input id="sizeMaxEdge" type="number" value="" min="0" max="200000" step="1" placeholder="不限" aria-label="原图短边上限，像素"></fieldset>
        <fieldset><legend>文件大小（MB）</legend>
          <input id="sizeMinMB" type="number" value="" min="0" step="0.01" placeholder="不限" aria-label="文件大小下限，MB">
          <span>至</span><input id="sizeMaxMB" type="number" value="" min="0" step="0.01" placeholder="不限" aria-label="文件大小上限，MB"></fieldset>
        <button type="button" class="btn ghost" onclick="applySizeFilter()">应用范围</button>
        <button type="button" class="btn ghost" onclick="excludeSmallImages()">排除小图（短边 ≥600 px）</button>
        <button type="button" class="btn ghost" onclick="resetSizeFilter()">清除大小条件</button>
      </div>
      <p id="sizeFilterStatus" role="status" aria-live="polite">留空表示不限；范围包含边界。只过滤显示，不删除文件。</p>
    </details>
    <div class="toolbarRow secondary">
      <div class="toolGroup" aria-label="分析选项">
        <span class="toolGroupLabel">分析</span>
        <label class="chk slim" title="跳过可大幅提速，适合先快速粗筛"><input type="checkbox" id="facesOpt" checked><span class="sw"></span>人脸分析</label>
        <label class="chk slim" title="用 NIMA 神经网络美学分（idealo MobileNet，Apache-2.0）加权综合分，约 +90ms/张"><input type="checkbox" id="aiOpt"><span class="sw"></span>美学模型</label>
        <label class="chk slim" title="扫描时递归进入子文件夹（婚礼/旅拍分日文件夹必备）"><input type="checkbox" id="recursiveOpt"><span class="sw"></span>含子文件夹</label>
        <label class="toolSelect" title="下一次扫描生效；严格模式减少误合并，宽松模式允许更多差异"><span>相似程度</span><select id="similarityProfile" aria-label="相似分组严格程度" onchange="projectChanged()"><option value="strict">严格</option><option value="balanced" selected>均衡</option><option value="loose">宽松</option></select></label>
      </div>
      <div class="toolbarActions">
        <button class="btn ghost" onclick="aiSmart()" title="保留无硬伤、非连拍重复且综合分达标的照片">智能筛选</button>
        <button class="btn ghost" onclick="selAll(1)">全选</button>
        <button class="btn ghost" onclick="selAll(0)">恢复推荐</button>
        <button class="btn ghost" onclick="selAll(-1)">全不选</button>
      </div>
    </div>
    <div class="toolbarRow batchRow" aria-label="批量照片操作">
      <span class="batchSelectionCount" id="batchSelectionCount">全部已选 0 · 当前可见 0 · 隐藏 0</span>
      <button class="btn ghost" id="clearHiddenSelection" type="button" hidden onclick="clearHiddenSelection()">取消隐藏选择</button>
      <button class="btn ghost" id="cullUndo" type="button" disabled onclick="undoCulling()" title="撤销筛片操作 · ⌘Z">撤销</button>
      <button class="btn ghost" id="cullRedo" type="button" disabled onclick="redoCulling()" title="重做筛片操作 · ⌘⇧Z">重做</button>
      <button class="btn ghost" id="splitGroups" type="button" onclick="editSimilarGroups('split')" title="将所选照片分别拆成单张，不删除原片">拆组</button>
      <button class="btn ghost" id="mergeGroups" type="button" onclick="editSimilarGroups('merge')" title="仅合并所选照片，不包含未选中的组成员">合组</button>
      <label class="toolSelect" for="batchRating"><span>设星级</span><select id="batchRating" aria-label="批量设置星级">
        <option value="">选择星级</option><option value="1">1 星</option><option value="2">2 星</option>
        <option value="3">3 星</option><option value="4">4 星</option><option value="5">5 星</option>
        <option value="0">清除星级</option></select></label>
      <button class="btn ghost" id="batchRateBtn" type="button" disabled onclick="applyBatchRatingFromControl()">应用</button>
      <button class="btn ghost" id="projectRemoveBtn" type="button" disabled onclick="removeSelectedFromProject()">从项目移除</button>
      <button class="btn trashAction" id="trashBtn" type="button" disabled onclick="deleteSelectedToTrash()">移到废纸篓/回收站</button>
      <span class="batchHint">删除原片可从系统废纸篓/回收站恢复</span>
    </div>
    </details>
  <div class="toolbarRow" id="groupNav" hidden aria-label="相似组导航">
    <span id="groupPosition" role="status" aria-live="polite"></span>
    <button class="btn ghost" id="previousGroup" type="button" onclick="navigateGroup(-1)">上一组</button>
    <button class="btn ghost" id="nextGroup" type="button" onclick="navigateGroup(1)">下一组</button>
    <button class="btn ghost" type="button" onclick="setAllGroupsCollapsed(true)">全部收起</button>
    <button class="btn ghost" type="button" onclick="setAllGroupsCollapsed(false)">全部展开</button>
  </div>
  <div class="toolbarRow projectRow">
    <span id="projectStatus" role="status" aria-live="polite">正在恢复项目…</span>
    <span id="regroupStatus" role="status" aria-live="polite"></span>
    <button class="btn ghost" id="projectSave" type="button" onclick="saveProject(true)">保存项目</button>
    <button class="btn ghost" id="projectRetry" type="button" hidden onclick="restoreProject()">重试恢复</button>
  </div>
  </div>
  </div>
  <div id="scanbar"><div id="scantrack"><i id="scanfill"></i></div>
    <span id="scantext"></span>
    <button class="btn ghost" id="cancelscan" onclick="cancelScan()" style="display:none">取消</button></div>
  <div id="grid" data-density="comfortable"></div>
  <div id="filterEmpty" hidden role="status">没有符合当前筛选条件的照片。照片仍保留在项目中。<br>
    <span id="groupEmptyHint"></span>
    <button type="button" class="btn ghost" onclick="regroupVisiblePhotos()">重新识别相似照片</button>
    <button type="button" class="btn ghost" onclick="clearPhotoFilters()">清除筛选，显示全部</button></div>
  <div id="empty"><div class="orb"></div><h2>把照片拖进来，或选择文件夹</h2>
    <p>支持拖入整个文件夹（含子文件夹）· JPEG / PNG / RAW · 也可直接输入路径</p></div>
</div>
<div id="side" hidden><h2>处理参数</h2>
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
  <div class="sect">基础调色</div>
  <div id="toneControls"></div>
  <div class="toneActions"><button class="btn ghost" type="button" onclick="resetTone()">重置调色</button>
    <button class="btn ghost" type="button" onclick="saveRecipe()">保存参数</button>
    <button class="btn ghost" type="button" onclick="$('recipeFile').click()">载入参数</button></div>
  <input id="recipeFile" type="file" accept=".json,application/json" hidden onchange="loadRecipe(this.files[0]);this.value=''">
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
  <div class="subhint regionHint">局部区域限定清晰度、磨皮和修复；曝光、白平衡及追色作用于全图。未找到区域时会提示，不会凭空生成人脸。</div>
  <div class="sect">导出</div>
  <div class="row"><label for="exportSize">成品尺寸（长边）</label><select id="exportSize">
    <option value="2560">2560px · 日常交付</option><option value="original">原始尺寸 · 高清交付</option>
    <option value="4096">4096px</option><option value="2048">2048px · 快速出图</option></select></div>
  <div class="row"><label for="exportQuality">JPEG 质量</label><select id="exportQuality">
    <option value="95">95 · 高质量</option><option value="90">90 · 较小文件</option><option value="100">100 · 最高质量</option></select></div>
  <div id="actionGroup">
    <button class="btn" id="colorBtn" type="button" onclick="process('color')">追色</button>
    <button class="btn ghost" id="polishBtn" type="button" onclick="process('polish')">美化</button>
    <button class="btn ghost" id="presetBtn" type="button" onclick="process('preset')">套用预设</button>
  </div>
  <div id="actionHint">单张打开大图调节，确认后导出；多张按同一组参数批量处理。</div>
  <button class="btn ghost" id="retryBtn" type="button" hidden onclick="retryFailed()">重试失败照片</button>
  <div id="status" role="status" aria-live="polite">就绪</div>
  <section id="exportQueue" aria-labelledby="exportQueueTitle">
    <h3 id="exportQueueTitle">导出队列</h3>
    <p class="queueHint">多张后台导出，刷新页面不中断。退出软件后，下次打开点击「继续」。暂停会先完成当前照片。</p>
    <div id="exportQueueError" role="status" aria-live="polite"></div>
    <button class="btn ghost" type="button" onclick="refreshExportQueue(true)">刷新队列</button>
    <button class="btn ghost" id="exportPendingRetry" type="button" hidden onclick="confirmPendingExport()">确认上次提交</button>
    <div id="exportQueueList"></div>
    <p class="queueHint">清除记录只移除队列历史，不删除已导出的成品。</p>
  </section>
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
    <button class="btn ghost" id="lbReveal" onclick="revealCurrent()">在 Finder 中显示</button>
    <button class="btn ghost" id="lbClose" onclick="lbHide()">完成 Esc</button></div>
  <div id="lbStage"><div id="lbSpin">加载中…</div>
    <img id="lbImg" src="data:image/gif;base64,R0lGODlhAQABAAD/ACwAAAAAAQABAAACADs=" alt="照片预览">
    <button class="lbNav" id="lbPrev" onclick="lbNav(-1)">‹</button>
    <button class="lbNav" id="lbNext" onclick="lbNav(1)">›</button></div>
  <div id="lbBar">
    <div id="lbStars"></div>
    <div id="lbInfo"></div>
    <div id="lbExif"></div>
    <div style="color:var(--dim);font-size:11.5px">
      <span class="lbKey">←</span><span class="lbKey">→</span>切换
      <span class="lbKey">空格</span>返回选片
      <span class="lbKey">0</span>–<span class="lbKey">5</span>星级（写 XMP）
      <span class="lbKey">Esc</span>关闭</div>
  </div>
</div>
<div id="compareModal" role="dialog" aria-modal="true" aria-labelledby="compareTitle">
  <div id="compareTop"><span id="compareTitle">相似照片对比</span><span id="compareHint">点击照片切换操作目标 · 左右键切换 · 0–5 评级 · 预览最长边 2200 像素（非 RAW 原始像素）</span>
    <button class="btn ghost" id="compareClose" onclick="closeGroupCompare()">完成 Esc</button></div>
  <div id="compareControls"><span>同步缩放 · 滚轮放大，拖动看细节</span><input id="compareZoom" type="range" min="0.1" max="32" step="0.1" value="1" aria-label="同步缩放">
    <span id="compareZoomValue">1.0×</span><button class="btn ghost" type="button" onclick="resetCompareView()">适合画布</button>
    <button class="btn ghost" type="button" onclick="comparePreviewPixels()">100% 预览像素</button><span id="compareActiveName"></span>
    <label>当前星级 <select id="compareRating" onchange="compareRate(this.value)" aria-label="当前对比照片星级">
      <option value="0">未评级</option><option value="1">1 星</option><option value="2">2 星</option><option value="3">3 星</option><option value="4">4 星</option><option value="5">5 星</option></select></label>
    <button id="compareKeep" type="button" class="btn ghost" onclick="compareKeep(true)">保留 P</button>
    <button id="compareReject" type="button" class="btn ghost" onclick="compareKeep(false)">移出保留 X</button></div>
  <div id="compareGrid"></div>
</div>
<div id="toast"></div>

<script>
const BAD=['低分','闭眼'],WARN=['可能模糊','曝光异常','连拍重复','疑似眨眼','AI 低分'];
/* ── 扫描（后台任务 + 增量出图） ── */
let JOB=null,SCAN_ACTIVE=false,since=0,updatesSince=0,ranked=[],library=[],renderedN=0,ringQ=[],targetPath=null;
const libraryByPath=new Map();
let libraryGeneration=0;
let SCAN_SPEC=null;
let selectedPaths=new Set(), selectionTouched=false,BATCH_BUSY=false;
// 视图重排时复用卡片和已加载的缩略图，避免筛选/相似组排序反复重载图片。
const cardCache=new Map();
const groupSections=new Map(),collapsedGroups=new Set();
let VIEW_GROUPS=[],activeGroupKey=null,groupJumpTop=null;
let groupViewGeneration=0;
const CHUNK=240;
const CULL_HISTORY={undo:[],redo:[],applying:false};
// 每次启动扫描/导入都递增；旧请求返回后不得覆盖新任务。
let SCAN_REQ_GEN=0;
const $=id=>document.getElementById(id);
function baseName(path){
  const parts=String(path??'').replace(/\\/g,'/').split('/').filter(Boolean);
  return parts.length?parts[parts.length-1]:'';
}
async function api(path,body){
  const r=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:undefined);
  let j;try{j=await r.json();}catch(error){error.status=r.status;error.responseReceived=true;throw error;}
  if(!r.ok||j.error){const error=new Error(j.error||'请求失败 '+r.status);error.status=r.status;error.responseReceived=true;error.definiteRejected=!r.ok;throw error;}return j;}
const EXPORT_QUEUE={jobs:new Map(),nodes:new Map(),generation:0,actions:new Map(),loading:null,timer:null,failures:0,ready:false,pending:null,reviewGeneration:0};
function queueMessage(message){$('exportQueueError').textContent=message;}
function exportQueueReady(){
  if(EXPORT_QUEUE.ready||EXPORT_QUEUE.pending)return true;
  if(!EXPORT_QUEUE.failures)queueMessage('正在读取导出队列，请稍候；读取完成后可提交新批量任务。');
  return false;
}
function renderExportQueue(){
  $('exportPendingRetry').hidden=!EXPORT_QUEUE.pending;$('exportPendingRetry').disabled=PROCESS_BUSY;
  const list=$('exportQueueList');
  for(const [id,row] of EXPORT_QUEUE.nodes)if(!EXPORT_QUEUE.jobs.has(id)){row.remove?.();EXPORT_QUEUE.nodes.delete(id);}
  if(!EXPORT_QUEUE.jobs.size){list.textContent='暂无导出任务';syncBatchControls();return;}
  if(!EXPORT_QUEUE.nodes.size)list.textContent='';
  const labels={queued:'等待导出',running:'正在导出',paused:'已暂停',completed:'已完成',failed:'有失败项'};
  for(const job of EXPORT_QUEUE.jobs.values()){
    let row=EXPORT_QUEUE.nodes.get(job.id);
    if(!row){
      row=document.createElement('div');row.className='exportJob';row.dataset.exportId=job.id;
      const title=document.createElement('div'),counts=document.createElement('div'),folder=document.createElement('div'),actions=document.createElement('div');
      title.className='exportJobTitle';counts.className='exportJobCounts';folder.className='exportJobFolder';actions.className='exportJobActions';
      row.parts={title,counts,folder,buttons:new Map()};
      for(const [action,label] of [['pause','暂停'],['resume','继续'],['retry','重试失败项'],['review','查看结果'],['forget','清除记录']]){
        const button=document.createElement('button');button.type='button';button.className='btn ghost';button.dataset.action=action;button.dataset.exportId=job.id;button.textContent=label;
        button.onclick=()=>action==='review'?openExportResults(job.id):exportAction(job.id,action);
        row.parts.buttons.set(action,button);actions.appendChild(button);
      }
      row.appendChild(title);row.appendChild(counts);row.appendChild(folder);row.appendChild(actions);list.appendChild(row);EXPORT_QUEUE.nodes.set(job.id,row);
    }
    const counts=job.counts||{},parts=row.parts,terminal=['completed','failed'].includes(job.state);
    parts.title.textContent=(labels[job.state]||job.state)+' · '+(counts.total||0)+' 张';
    parts.counts.textContent=`成功 ${counts.succeeded||0} · 失败 ${counts.failed||0} · 等待 ${counts.pending||0} · 处理中 ${counts.running||0}`;
    parts.folder.textContent=job.output_dir?'成品：'+job.output_dir:'成品保存在原目录的 photopilot_out/ 中';
    const available={pause:['queued','running'].includes(job.state),resume:job.state==='paused',retry:terminal&&counts.failed>0,review:counts.succeeded>0||counts.failed>0,forget:terminal};
    for(const [action,button] of parts.buttons){button.hidden=!available[action];button.disabled=EXPORT_QUEUE.actions.has(job.id);}
  }
  syncBatchControls();
}
function scheduleExportPoll(){
  clearTimeout(EXPORT_QUEUE.timer);
  if(document.visibilityState==='hidden'||EXPORT_QUEUE.failures>=3)return;
  const active=[...EXPORT_QUEUE.jobs.values()].some(job=>['queued','running'].includes(job.state));
  EXPORT_QUEUE.timer=setTimeout(()=>refreshExportQueue(),active?1500:6000);
}
function refreshExportQueue(manual=false){
  if(manual)EXPORT_QUEUE.failures=0;
  if(EXPORT_QUEUE.loading)return EXPORT_QUEUE.loading;
  if(!EXPORT_QUEUE.ready&&!EXPORT_QUEUE.pending)queueMessage('正在读取导出队列，请稍候；读取完成后可提交新批量任务。');
  const generation=EXPORT_QUEUE.generation;
  EXPORT_QUEUE.loading=(async()=>{
    try{
      const response=await api('/api/exports');if(generation!==EXPORT_QUEUE.generation)return;
      EXPORT_QUEUE.jobs=new Map((response.jobs||[]).map(job=>[job.id,job]));EXPORT_QUEUE.failures=0;EXPORT_QUEUE.ready=true;
      queueMessage(response.queue_error||'');renderExportQueue();
    }catch(error){if(generation===EXPORT_QUEUE.generation){EXPORT_QUEUE.failures++;queueMessage('队列暂时无法刷新：'+error.message+'。可点击「刷新队列」重试。');}}
    finally{EXPORT_QUEUE.loading=null;scheduleExportPoll();}
  })();return EXPORT_QUEUE.loading;
}
async function exportAction(id,action){
  if(EXPORT_QUEUE.actions.has(id))return;
  EXPORT_QUEUE.generation++;EXPORT_QUEUE.actions.set(id,action);renderExportQueue();
  try{
    const response=await api('/api/export_action',{id,action});EXPORT_QUEUE.generation++;
    if(response.removed||response.job?.state==='removed')EXPORT_QUEUE.jobs.delete(id);
    else if(response.job)EXPORT_QUEUE.jobs.set(id,response.job);
    queueMessage('');
  }catch(error){EXPORT_QUEUE.generation++;queueMessage('操作未确认：'+error.message+'。请刷新队列查看最新状态。');}
  finally{EXPORT_QUEUE.actions.delete(id);renderExportQueue();scheduleExportPoll();}
}
async function submitExportQueue(settings){
  if(!exportQueueReady())return;
  if(!EXPORT_QUEUE.pending){const request_id=globalThis.crypto?.randomUUID?.()||('web-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2));EXPORT_QUEUE.pending={request_id,settings};}
  const pending=EXPORT_QUEUE.pending;
  try{
    const response=await api('/api/exports',pending);if(!response.job?.id)throw new Error('服务器未返回任务确认');
    EXPORT_QUEUE.generation++;EXPORT_QUEUE.jobs.set(response.job.id,response.job);EXPORT_QUEUE.pending=null;queueMessage('');renderExportQueue();scheduleExportPoll();
    $('status').textContent='已加入导出队列 · 可继续审片或单张修图';
  }catch(error){
    if(error.definiteRejected&&[400,403,413].includes(error.status)){EXPORT_QUEUE.pending=null;queueMessage('导出参数被拒绝：'+error.message+'。调整参数后可重新提交。');}
    else queueMessage('任务提交尚未确认：'+error.message+'。点击「确认上次提交」会用原参数确认同一任务；若已刷新页面，请先查看队列，避免重复提交。');
    renderExportQueue();
    throw error;
  }
}
async function confirmPendingExport(){
  if(!EXPORT_QUEUE.pending||PROCESS_BUSY)return;
  PROCESS_BUSY=true;setActionButtonsDisabled(true);renderExportQueue();
  try{await submitExportQueue(EXPORT_QUEUE.pending.settings);}catch(_){/* The queue retains the actionable error. */}
  finally{PROCESS_BUSY=false;setActionButtonsDisabled(false);renderExportQueue();}
}
function exportMediaURL(value,kind){
  if(typeof value!=='string')return '';
  const prefix=kind==='download'?'/api/img?':'/api/thumb?';return value.startsWith(prefix)&&!/[\r\n]/.test(value)?value:'';
}
async function openExportResults(id,offset=0){
  const generation=++EXPORT_QUEUE.reviewGeneration,processGeneration=PROCESS_GEN;
  try{
    const response=await api('/api/export?id='+encodeURIComponent(id)+'&offset='+offset+'&limit=50');
    if(generation!==EXPORT_QUEUE.reviewGeneration||processGeneration!==PROCESS_GEN)return;
    closeResults();const inner=$('resInner');inner.replaceChildren();$('results').classList.remove('single-result');
    const heading=document.createElement('h2');heading.textContent='导出结果 · '+(response.job.counts?.total||0)+' 张';inner.appendChild(heading);
    const nav=document.createElement('div');nav.className='exportReviewNav';
    const total=response.job.items_total||0,label=document.createElement('span');label.textContent=`${offset+1}–${Math.min(offset+50,total)} / ${total}`;nav.appendChild(label);
    for(const [label,next,disabled] of [['上一页',Math.max(0,offset-50),offset===0],['下一页',offset+50,offset+50>=total],['完成',null,false]]){
      const button=document.createElement('button');button.type='button';button.className='btn ghost';button.textContent=label;button.disabled=disabled;button.onclick=()=>next===null?closeResults():openExportResults(id,next);nav.appendChild(button);
    }inner.appendChild(nav);
    for(const item of (response.job.items||[]).slice(0,50)){
      const pair=document.createElement('div');pair.className='pair';const result=item.result;
      if(result){const before=exportMediaURL(result.before,'preview'),after=exportMediaURL(result.after,'preview'),download=exportMediaURL(result.download,'download');
        pair.innerHTML=`<div class="cmp"><img alt="原始照片" loading="lazy" src="${escapeHtml(before)}"><img alt="导出效果" loading="lazy" class="b" src="${escapeHtml(after)}"><div class="bar"></div><div class="knob">◄►</div><span class="tag l">原始</span><span class="tag r">成品</span></div><div class="name">${escapeHtml(baseName(item.path))}</div>${download?`<a href="${escapeHtml(download)}" download="${escapeHtml(baseName(result.out))}">下载完整成品</a>`:''}`;bindCmp(pair.querySelector('.cmp'));
      }else pair.textContent=baseName(item.path)+' · '+(item.error||item.state);
      inner.appendChild(pair);
    }$('results').classList.add('show');
  }catch(error){if(generation===EXPORT_QUEUE.reviewGeneration)queueMessage('结果暂时无法打开：'+error.message);}
}
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')refreshExportQueue(true);else clearTimeout(EXPORT_QUEUE.timer);});
// 项目数据不包含当前运行会话的扫描 job 或处理 sid。
const EDIT_DRAFTS=new Map();
const PROJECT={ready:false,blocked:false,applying:false,intent:0,revision:0,acked:0,recoveryGeneration:0,restoreRequest:0,
  folder:'',scanIncomplete:false,pendingScan:null,timer:null,writing:null,lastPartial:0};
const pendingRatingPrevious=new Map();
function projectStatus(message,state='pending'){
  const node=$('projectStatus');node.textContent=message;node.dataset.state=state;
}
function projectUnavailableDetails(missing,changed){
  const sample=[...missing.slice(0,5).map(path=>['缺失',path]),...changed.slice(0,5).map(path=>['已变化',path])].slice(0,5);
  const lines=sample.map(([label,path])=>label+'：'+(path.length>160?path.slice(0,157)+'…':path));
  const remaining=missing.length+changed.length-sample.length;
  if(remaining)lines.push(`另 ${remaining} 个文件未列出`);
  return lines.join('\n');
}
function projectSnapshot(){
  flushEditorGesture(false);
  return {version:1,folder:PROJECT.folder,photos:library.map(p=>({...p,
      rating:pendingRatingPrevious.has(p.path)?pendingRatingPrevious.get(p.path):(p.rating||0)})),
    selected:[...selectedPaths].filter(path=>libraryByPath.has(path)||library.some(p=>p.path===path)),
    target:targetPath,selection_touched:selectionTouched,scan_incomplete:PROJECT.scanIncomplete,
    settings:{recipe:recipeValues(),sortMode,filterMode,sizeFilter:{...sizeFilter},sidebarOpen:!$('side').hidden,density:$('grid').dataset.density,similarityProfile:$('similarityProfile').value||'balanced',
      options:Object.fromEntries(['facesOpt','aiOpt','recursiveOpt','preserveLuma','skinProtect'].map(id=>[id,$(id).checked])),
      algo:$('algo').value,exportSize:$('exportSize').value,exportQuality:$('exportQuality').value},
    drafts:Object.fromEntries(EDIT_DRAFTS)};
}
function projectChanged(delay=500){
  if(PROJECT.applying)return;
  PROJECT.intent++;
  PROJECT.revision++;
  if(!PROJECT.ready||PROJECT.blocked||!PROJECT.folder)return;
  projectStatus('项目有更改 · 等待保存');clearTimeout(PROJECT.timer);
  PROJECT.timer=setTimeout(()=>saveProject(),delay);
}
async function saveProject(explicit=false){
  flushEditorGesture(false);
  clearTimeout(PROJECT.timer);PROJECT.timer=null;
  if(!PROJECT.ready){projectStatus('正在恢复项目，请稍后保存');return false;}
  if(PROJECT.blocked)return false;
  if(!PROJECT.folder){projectStatus('选择照片项目后可保存项目');return false;}
  if(explicit)PROJECT.revision++;
  if(PROJECT.writing)return PROJECT.writing;
  if(PROJECT.revision===PROJECT.acked)return true;
  PROJECT.writing=(async()=>{
    while(PROJECT.acked<PROJECT.revision&&!PROJECT.blocked){
      const revision=PROJECT.revision,snapshot=projectSnapshot();
      projectStatus('正在保存项目…');
      try{const response=await api('/api/project',snapshot);
        if(response.ok!==true)throw new Error('服务器未确认保存');
        PROJECT.acked=revision;
        if(PROJECT.revision===revision)projectStatus('项目已保存','saved');
      }catch(error){projectStatus('项目保存失败：'+error.message+' · 点击「保存项目」重试','error');return false;}
    }
    return !PROJECT.blocked;
  })();
  try{return await PROJECT.writing;}finally{PROJECT.writing=null;}
}
function validateProjectSettings(settings){
  if(!settings||typeof settings!=='object'||Array.isArray(settings))throw new Error('项目设置无效');
  validateRecipe(settings.recipe);
  if(settings.sizeFilter!==undefined)validateSizeFilter(settings.sizeFilter);
  if(settings.sidebarOpen!==undefined&&typeof settings.sidebarOpen!=='boolean')throw new Error('项目参数面板设置无效');
  if(settings.similarityProfile!==undefined&&!['strict','balanced','loose'].includes(settings.similarityProfile))throw new Error('项目相似分组设置不合法');
  const enums={sortMode:['smart','score','name','rating'],filterMode:['all','recommended','groups','starred','review'],
    density:['compact','comfortable','large'],algo:[...$('algo').options].map(o=>o.value),
    exportSize:[...$('exportSize').options].map(o=>o.value),exportQuality:[...$('exportQuality').options].map(o=>o.value)};
  for(const [key,values] of Object.entries(enums))if(!values.includes(settings[key]))throw new Error('项目设置不合法：'+key);
  for(const id of ['facesOpt','aiOpt','recursiveOpt','preserveLuma','skinProtect'])if(typeof settings.options?.[id]!=='boolean')throw new Error('项目选项不合法：'+id);
}
async function restoreProject(){
  const request=++PROJECT.restoreRequest,generation=SCAN_REQ_GEN,intent=PROJECT.intent,recovery=PROJECT.recoveryGeneration;
  // 未知的旧检查点先受保护；只有有效的恢复结果或完整的新扫描能解除。
  PROJECT.ready=false;PROJECT.blocked=true;projectStatus('正在恢复项目…');
  try{
    const [response]=await Promise.all([api('/api/project'),PRESETS_READY]);
    if(request!==PROJECT.restoreRequest||recovery!==PROJECT.recoveryGeneration)return;
    const stale=generation!==SCAN_REQ_GEN||intent!==PROJECT.intent;
    if(!response.state){
      PROJECT.blocked=false;if(PROJECT.pendingScan)PROJECT.pendingScan.blockedAtStart=false;
      if(!stale)projectStatus('尚无已保存项目');return;
    }
    const state=response.state;validateProjectSettings(state.settings);
    if(state.version!==1||!Array.isArray(state.photos)||!Array.isArray(state.selected)||typeof state.selection_touched!=='boolean'
      ||typeof state.folder!=='string'||!state.drafts||typeof state.drafts!=='object'||Array.isArray(state.drafts))throw new Error('项目数据无效');
    const missing=response.missing||[],changed=response.changed||[];
    if(!Array.isArray(missing)||!Array.isArray(changed))throw new Error('项目文件状态无效');
    const unavailable=new Set([...missing,...changed]),usable=state.photos.filter(p=>!unavailable.has(p.path));
    PROJECT.blocked=unavailable.size>0;
    if(PROJECT.pendingScan)PROJECT.pendingScan.blockedAtStart=PROJECT.blocked;
    $('projectRetry').hidden=!PROJECT.blocked;
    if(stale){
      if(PROJECT.blocked)projectStatus(`原项目缺失 ${missing.length} 张 · 已变化 ${changed.length} 张。自动保存已暂停；新扫描须完整成功后才能替换原项目。\n`+
        projectUnavailableDetails(missing,changed),'blocked');
      return;
    }
    PROJECT.applying=true;
    applyRecipe(state.settings.recipe);
    $('similarityProfile').value=state.settings.similarityProfile||'balanced';
    for(const id of ['facesOpt','aiOpt','recursiveOpt','preserveLuma','skinProtect'])$(id).checked=state.settings.options[id];
    for(const id of ['algo','exportSize','exportQuality'])$(id).value=state.settings[id];
    sortMode=state.settings.sortMode;filterMode=state.settings.filterMode;
    sizeFilter=validateSizeFilter(state.settings.sizeFilter||emptySizeFilter());syncSizeFilterInputs();
    setSidebarOpen(state.settings.sidebarOpen===true);
    $('sortMode').value=sortMode;$('filterMode').value=filterMode;setDensity(state.settings.density,false);
    replaceProjectLibrary(usable);selectedPaths=new Set(state.selected.filter(path=>libraryByPath.has(path)));
    selectionTouched=state.selection_touched;targetPath=libraryByPath.has(state.target)?state.target:null;
    EDIT_DRAFTS.clear();for(const [path,draft] of Object.entries(state.drafts))EDIT_DRAFTS.set(path,draft);
    PROJECT.folder=state.folder;PROJECT.scanIncomplete=state.scan_incomplete===true;$('folder').value=state.folder;
    PROJECT.blocked=unavailable.size>0;PROJECT.pendingScan=null;PROJECT.acked=PROJECT.revision;
    rebuildView();$('empty').style.display=library.length?'none':'';
    $('projectRetry').hidden=!PROJECT.blocked;
    const interrupted=PROJECT.scanIncomplete?' · 上次扫描中断，请重新扫描以补齐照片':'';
    if(PROJECT.blocked)projectStatus(`已恢复 ${usable.length} 张 · 缺失 ${missing.length} 张 · 已变化 ${changed.length} 张。自动保存已暂停；重试恢复或重新扫描/导入。`+interrupted+'\n'+
      projectUnavailableDetails(missing,changed),'blocked');
    else projectStatus(`项目已恢复 ${usable.length} 张`+interrupted,'restored');
  }catch(error){
    if(request!==PROJECT.restoreRequest||recovery!==PROJECT.recoveryGeneration)return;
    PROJECT.blocked=true;$('projectRetry').hidden=false;
    if(PROJECT.pendingScan)PROJECT.pendingScan.blockedAtStart=true;
    projectStatus('项目恢复失败：'+error.message+' · 原项目已保留，请重试恢复或重新扫描/导入','blocked');
  }finally{
    if(request===PROJECT.restoreRequest){
      PROJECT.applying=false;PROJECT.ready=true;
      if(generation!==SCAN_REQ_GEN&&PROJECT.revision>PROJECT.acked&&PROJECT.folder&&!PROJECT.blocked)projectChanged();
    }
  }
}
function replaceProjectLibrary(photos){
  libraryGeneration++;
  // 旧 XMP 写入仍按路径串行等待，但它的旧星级不得进入新项目快照。
  pendingRatingPrevious.clear();
  if($('compareModal').classList.contains('show'))closeGroupCompare();
  CULL_HISTORY.undo=[];CULL_HISTORY.redo=[];
  libraryByPath.clear();cardCache.clear();ringQ=[];renderedN=0;
  groupSections.clear();collapsedGroups.clear();VIEW_GROUPS=[];activeGroupKey=null;groupJumpTop=null;$('groupNav').hidden=true;
  library=photos.map(mergePhoto);ranked=[];selectedPaths.clear();targetPath=null;selectionTouched=false;
  $('grid').replaceChildren();
}
function beginProjectScan(folder,inputIncomplete=false){
  PROJECT.pendingScan={folder,accepted:false,blockedAtStart:PROJECT.blocked,inputIncomplete};since=0;updatesSince=0;FINAL_STAGE='';
}
function acceptProjectScan(photos){
  if(!photos?.length)return false;
  const pending=PROJECT.pendingScan;
  if(pending&&!pending.accepted){
    replaceProjectLibrary([]);EDIT_DRAFTS.clear();pending.accepted=true;
    PROJECT.folder=pending.folder;PROJECT.scanIncomplete=true;
    $('folder').value=pending.folder;
    if(!pending.blockedAtStart){PROJECT.blocked=false;$('projectRetry').hidden=true;}
    projectChanged();
  }
  return true;
}
function checkpointScan(terminal=false){
  if(!PROJECT.pendingScan?.accepted)return;
  if(terminal||Date.now()-PROJECT.lastPartial>=3000){PROJECT.lastPartial=Date.now();projectChanged(terminal?0:500);}
}
function finishProjectScan(success){
  if(PROJECT.pendingScan?.accepted){
    PROJECT.scanIncomplete=!success;
    if(success){PROJECT.recoveryGeneration++;PROJECT.ready=true;PROJECT.blocked=false;$('projectRetry').hidden=true;}
    else if(PROJECT.pendingScan.blockedAtStart)projectStatus('扫描未完成 · 原项目已保留，自动保存仍暂停，请重新扫描/导入','blocked');
    checkpointScan(true);
  }
  PROJECT.pendingScan=null;
}
function projectUserIntent(event){
  if(PROJECT.applying||event.isTrusted===false)return;
  projectChanged();
}
for(const type of ['input','change'])document.addEventListener(type,projectUserIntent,true);
window.addEventListener('pagehide',()=>{saveProject();});
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='hidden')saveProject();});
const DENSITY_KEY='photopilot-density-v2';
function setDensity(mode,save=true){
  const value=['compact','comfortable','large'].includes(mode)?mode:'comfortable';
  $('grid').dataset.density=value;
  document.querySelectorAll('#viewbar button').forEach(b=>b.classList.toggle('on',b.dataset.density===value));
  if(ranked.length>=1200){rebuildView();if(virtualGroupedView())navigateGroup(0);}
  if(save){try{localStorage.setItem(DENSITY_KEY,value);}catch(e){}projectChanged();}
}
document.querySelectorAll('#viewbar button').forEach(b=>b.onclick=()=>setDensity(b.dataset.density));
try{setDensity(localStorage.getItem(DENSITY_KEY)||'comfortable',false);}catch(e){setDensity('comfortable',false);}
async function pickFolder(){
  if(window.pywebview&&window.pywebview.api&&window.pywebview.api.pick_folder){
    try{
      const dir=await window.pywebview.api.pick_folder();
      if(dir){$('folder').value=dir;
        scan({recursive:$('recursiveOpt').checked});}
    }catch(e){
      const message=e?.message||String(e);
      $('status').textContent='文件夹选择失败：'+message+'。请重试，或将路径粘贴到顶部路径框后点“扫描打分”。';
      toast('文件夹选择失败：'+message);
    }
    return;
  }
  $('pickDir').click();               // 浏览器环境：webkitdirectory 兜底
}
async function pickPhotos(){
  if(window.pywebview&&window.pywebview.api&&window.pywebview.api.pick_photos){
    try{
      const files=await window.pywebview.api.pick_photos();
      if(files&&files.length)await importLocalPaths(files);
    }catch(e){
      const message=e?.message||String(e);
      $('status').textContent='照片选择失败：'+message+'。请重试，或将照片拖入窗口。';
      toast('照片选择失败：'+message);
    }
    return;
  }
  $('pickFiles').click();             // 浏览器环境：走上传
}
function slide(el,out){const label=$(out);if(label)label.textContent=(+el.value).toFixed(2);
  el.style.setProperty('--val',((el.value-el.min)/(el.max-el.min)*100)+'%');}
function toast(msg){const t=$('toast');t.textContent=msg;t.classList.add('show');
  clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove('show'),2600);}
const SORT_KEY='photopilot-sort-v1', FILTER_KEY='photopilot-filter-v1';
let sortMode='smart', filterMode='all';
let activePhotoPath=null;
function setSidebarOpen(open,remember=false){
  $('side').hidden=!open;$('sideToggle').setAttribute('aria-expanded',String(!!open));
  if(remember){projectChanged();if(!open)$('sideToggle').focus?.({preventScroll:true});}
  queueGroupFill();
}
function emptySizeFilter(){return {minEdge:null,maxEdge:null,minMB:null,maxMB:null};}
let sizeFilter=emptySizeFilter();
function validateSizeFilter(value){
  const limits={minEdge:200000,maxEdge:200000,minMB:8589934591,maxMB:8589934591};
  if(!value||typeof value!=='object'||Array.isArray(value)||Object.keys(value).length!==4)throw new Error('大小范围设置无效');
  for(const [key,limit] of Object.entries(limits)){
    const n=value[key];
    if(n!==null&&(typeof n!=='number'||!Number.isFinite(n)||n<0||n>limit||key.endsWith('Edge')&&!Number.isInteger(n)))throw new Error('大小范围必须为有效非负数，像素需为整数');
  }
  if(value.minEdge!==null&&value.maxEdge!==null&&value.minEdge>value.maxEdge||value.minMB!==null&&value.maxMB!==null&&value.minMB>value.maxMB)throw new Error('范围下限不能大于上限');
  return {...value};
}
function sizeFilterActive(){return Object.values(sizeFilter).some(v=>v!==null);}
function matchesSizeFilter(photo){
  const f=sizeFilter;
  if(f.minEdge!==null||f.maxEdge!==null){
    const edge=Math.min(photo.original_width||0,photo.original_height||0);
    if(!(edge>0)||f.minEdge!==null&&edge<f.minEdge||f.maxEdge!==null&&edge>f.maxEdge)return false;
  }
  if(f.minMB!==null||f.maxMB!==null){
    const bytes=photo.file_size;
    if(typeof bytes!=='number'||!Number.isFinite(bytes)||bytes<0||f.minMB!==null&&bytes<f.minMB*1048576||f.maxMB!==null&&bytes>f.maxMB*1048576)return false;
  }
  return true;
}
function syncSizeFilterInputs(){
  for(const [key,id] of Object.entries({minEdge:'sizeMinEdge',maxEdge:'sizeMaxEdge',minMB:'sizeMinMB',maxMB:'sizeMaxMB'}))$(id).value=sizeFilter[key]===null?'':String(sizeFilter[key]);
}
function applySizeFilter(){
  try{
    const value={};
    for(const [key,id] of Object.entries({minEdge:'sizeMinEdge',maxEdge:'sizeMaxEdge',minMB:'sizeMinMB',maxMB:'sizeMaxMB'})){
      const text=$(id).value.trim();value[key]=text===''?null:Number(text);
      if($(id).validity?.badInput)throw new Error('请填写有效的大小范围');
    }
    sizeFilter=validateSizeFilter(value);
  }catch(error){$('sizeFilterStatus').textContent=error.message+'；原筛选条件保持不变。';return false;}
  projectChanged();rebuildView();return true;
}
function resetSizeFilter(){sizeFilter=emptySizeFilter();syncSizeFilterInputs();projectChanged();rebuildView();}
function excludeSmallImages(){
  sizeFilter={minEdge:600,maxEdge:null,minMB:null,maxMB:null};
  filterMode='all';$('filterMode').value='all';syncSizeFilterInputs();rememberView();rebuildView();
}
function clearPhotoFilters(){filterMode='all';$('filterMode').value='all';resetSizeFilter();rememberView();}
function isGroup(p){return Number(p.group_size||1)>=2;}
function groupOrder(items){
  const best=new Map();
  for(const p of items){if(!isGroup(p))continue;const g=String(p.group);
    if(!best.has(g)||p.score>best.get(g).score||p.score===best.get(g).score&&p.path<best.get(g).path)best.set(g,p);}
  return best;
}
function sortPhotos(items){
  const out=items.slice(), best=groupOrder(out);
  if(sortMode==='name')return out.sort((a,b)=>a.path.localeCompare(b.path));
  if(sortMode==='rating')return out.sort((a,b)=>(b.rating||0)-(a.rating||0)||b.score-a.score);
  if(sortMode==='score')return out.sort((a,b)=>b.score-a.score||a.path.localeCompare(b.path));
  return out.sort((a,b)=>{
    const ag=isGroup(a),bg=isGroup(b);
    const aa=ag?best.get(String(a.group)):a,bb=bg?best.get(String(b.group)):b;
    if(aa.path!==bb.path)return bb.score-aa.score||aa.path.localeCompare(bb.path);
    if(ag&&a.group===b.group)return (a.group_rank||99)-(b.group_rank||99)||b.score-a.score;
    return b.score-a.score||a.path.localeCompare(b.path);
  });
}
function filterPhotos(items){
  items=items.filter(matchesSizeFilter);
  if(filterMode==='recommended')return items.filter(autoSel);
  if(filterMode==='groups')return items.filter(isGroup);
  if(filterMode==='starred')return items.filter(p=>(p.rating||0)>0);
  if(filterMode==='review')return items.filter(p=>p.flags&&p.flags.length);
  return items;
}
function groupedView(){return sortMode==='smart'||filterMode==='groups';}
function viewGroups(items){
  const groups=new Map(),singles=[];
  const totals=new Map();
  for(const p of library){if(isGroup(p)){const key='group:'+String(p.group);totals.set(key,(totals.get(key)||0)+1);}}
  for(const p of items){
    if(!isGroup(p)){singles.push(p);continue;}
    const key='group:'+String(p.group);
    if(!groups.has(key))groups.set(key,{key,group:p.group,members:[],total:totals.get(key)||Number(p.group_size)});
    groups.get(key).members.push(p);
  }
  const result=Array.from(groups.values());
  result.forEach((g,i)=>{g.title='相似组 '+String(i+1).padStart(2,'0');});
  if(singles.length)result.push({key:'singles',title:'无相似照片',group:null,members:singles,total:singles.length});
  return result;
}
function updateGroupSummaries(){
  for(const g of VIEW_GROUPS){
    const entry=groupSections.get(g.key);if(!entry)continue;
    const selected=g.members.reduce((n,p)=>n+(selectedPaths.has(p.path)?1:0),0);
    entry.summary.textContent=(g.members.length<g.total?`显示 ${g.members.length} / ${g.total} 张`:`${g.members.length} 张`)+` · 已选 ${selected}`;
    entry.select.textContent=selected===g.members.length?'取消整组选中':'整组选中';
    const collapsed=collapsedGroups.has(g.key);
    entry.body.hidden=collapsed;entry.collapse.textContent=collapsed?'展开':'收起';
    entry.collapse.setAttribute('aria-expanded',String(!collapsed));
  }
}
function selectViewGroup(key){
  if(CULL_HISTORY.applying)return;
  const before=new Set(selectedPaths);
  const g=VIEW_GROUPS.find(g=>g.key===key);if(!g)return;
  const clear=g.members.every(p=>selectedPaths.has(p.path));selectionTouched=true;
  for(const p of g.members){
    if(clear)selectedPaths.delete(p.path);else selectedPaths.add(p.path);
    const card=cardCache.get(p.path);if(card)card.classList.toggle('sel',!clear);
  }
  recordSelection(before,'整组选择');updateGroupSummaries();updateStats();projectChanged();
}
function toggleGroupCollapse(key){
  if(collapsedGroups.has(key))collapsedGroups.delete(key);else collapsedGroups.add(key);
  if(!collapsedGroups.has(key))renderThroughGroup(key);
  updateGroupSummaries();queueGroupFill();
}
function renderThroughGroup(key){
  if(virtualGroupedView()){renderVirtualGroups(key);return;}
  let offset=0;
  for(const g of VIEW_GROUPS){
    if(g.key===key){
      const end=Math.min(ranked.length,Math.max(renderedN,offset+Math.min(CHUNK,g.members.length)));
      if(end>renderedN){appendGroupedCards(renderedN,end);renderedN=end;flushRings();}
      return;
    }
    offset+=g.members.length;
  }
}
function syncGroupNavigation(followScroll=false){
  const enabled=groupedView()&&VIEW_GROUPS.some(g=>g.group!==null);
  $('groupNav').hidden=!enabled;if(!enabled)return;
  if(!VIEW_GROUPS.some(g=>g.key===activeGroupKey))activeGroupKey=VIEW_GROUPS[0].key;
  if(followScroll&&groupJumpTop!==null&&Math.abs($('grid').scrollTop-groupJumpTop)>1)groupJumpTop=null;
  if(followScroll&&groupJumpTop===null&&$('grid').getBoundingClientRect){
    const top=$('grid').getBoundingClientRect().top;
    let current=null;
    for(const g of VIEW_GROUPS){const section=groupSections.get(g.key)?.section;
      if(section&&section.getBoundingClientRect().top<=top+24)current=g.key;else if(section)break;}
    // Short final groups cannot reach the viewport top: scrolling is clamped.
    const grid=$('grid');
    if(grid.scrollTop>0&&(virtualGroupedView()||renderedN>=ranked.length)&&grid.scrollTop+grid.clientHeight>=grid.scrollHeight-2)
      current=VIEW_GROUPS[VIEW_GROUPS.length-1].key;
    if(current)activeGroupKey=current;
  }
  const index=VIEW_GROUPS.findIndex(g=>g.key===activeGroupKey);
  $('groupPosition').textContent=`${VIEW_GROUPS[index].title} · ${index+1} / ${VIEW_GROUPS.length}`;
  $('previousGroup').disabled=index<=0;$('nextGroup').disabled=index>=VIEW_GROUPS.length-1;
}
function navigateGroup(direction){
  if(!groupedView()||!VIEW_GROUPS.length)return false;
  syncGroupNavigation();
  const index=VIEW_GROUPS.findIndex(g=>g.key===activeGroupKey),next=index+direction;
  if(next<0||next>=VIEW_GROUPS.length)return false;
  const g=VIEW_GROUPS[next];collapsedGroups.delete(g.key);renderThroughGroup(g.key);activeGroupKey=g.key;
  updateGroupSummaries();syncGroupNavigation();
  const section=groupSections.get(g.key)?.section,grid=$('grid');
  if(section?.getBoundingClientRect&&grid.getBoundingClientRect){
    const top=grid.scrollTop+section.getBoundingClientRect().top-grid.getBoundingClientRect().top;
    grid.scrollTop=Math.max(0,top);
    // Keep the requested group when the browser clamps the jump at the bottom.
    // Manual scrolling away releases this anchor and follows the viewport again.
    groupJumpTop=grid.scrollTop;
  }
  queueGroupFill();return true;
}
function setAllGroupsCollapsed(collapsed){
  if(!groupedView())return;
  groupJumpTop=null;
  for(const g of VIEW_GROUPS){
    if(collapsed)collapsedGroups.add(g.key);else collapsedGroups.delete(g.key);
    // Collapsed groups need only their headings, not all their photo nodes.
    if(collapsed&&!groupSections.has(g.key)){
      const entry=makeGroupSection(g);groupSections.set(g.key,entry);$('grid').appendChild(entry.section);
    }
    // Restore the incremental photo stream on unfold. Overview-only headings
    // have no cards yet; keeping them creates hundreds of empty expanded groups.
    const entry=groupSections.get(g.key);
    if(!collapsed&&!virtualGroupedView()&&entry&&entry.body.children.length===0){
      entry.section.remove();groupSections.delete(g.key);
    }
  }
  updateGroupSummaries();syncGroupNavigation();queueGroupFill();
}
function virtualGroupedView(){return groupedView()&&ranked.length>=1200;}
function virtualFlatView(){return !groupedView()&&ranked.length>=1200;}
function renderVirtualFlat(){
  if(!virtualFlatView())return;
  const grid=$('grid'),m=virtualGroupMetrics();
  const rows=Math.ceil(ranked.length/m.columns);
  const first=Math.min(rows-1,Math.max(0,Math.floor((grid.scrollTop-500)/m.row)));
  const visible=Math.min(Math.ceil((grid.clientHeight+1000)/m.row)+1,Math.floor(480/m.columns));
  const start=first*m.columns,end=Math.min(ranked.length,(first+visible)*m.columns);
  const windowKey=[groupViewGeneration,start,end,m.columns,m.height].join(':');
  if(grid.dataset.virtualWindow===windowKey)return;
  grid.dataset.virtualWindow=windowKey;
  const fragment=document.createDocumentFragment();
  const spacer=height=>{if(height<=0)return;const node=document.createElement('div');node.setAttribute('aria-hidden','true');node.style.cssText='grid-column:1/-1;height:'+height+'px;min-height:0';fragment.appendChild(node);};
  grid.style.gridAutoRows='auto';grid.style.alignContent='start';
  spacer(first*m.row-m.gap);
  for(let i=start;i<end;i++){const card=cardFor(ranked[i],i,false);card.style.height=m.height+'px';fragment.appendChild(card);}
  spacer((rows-Math.ceil(end/m.columns))*m.row-m.gap);
  grid.replaceChildren(fragment);
  for(const [path,card] of cardCache)if(!card.isConnected&&cardCache.size>720)cardCache.delete(path);
  renderedN=end-start;flushRings();
}
function virtualGroupMetrics(){
  const grid=$('grid'),density=grid.dataset.density;
  const [minimum,thumb,gap]=density==='compact'?[190,140,14]:density==='large'?[300,208,20]:[230,160,18];
  const width=Math.max(1,grid.clientWidth-40),columns=Math.max(1,Math.floor((width+gap)/(minimum+gap)));
  return {columns,gap,height:thumb+164,row:thumb+164+gap};
}
function setupVirtualGroups(){
  const metrics=virtualGroupMetrics();
  for(const g of VIEW_GROUPS){
    const entry=makeGroupSection(g);groupSections.set(g.key,entry);$('grid').appendChild(entry.section);
    entry.virtual=true;entry.body.style.height=Math.max(0,Math.ceil(g.members.length/metrics.columns)*metrics.row-metrics.gap)+'px';
    entry.body.style.gridAutoRows=metrics.height+'px';entry.body.style.alignContent='start';
  }
  updateGroupSummaries();
}
function renderVirtualGroups(focusKey=null){
  if(!virtualGroupedView())return;
  const grid=$('grid'),viewport=grid.getBoundingClientRect(),metrics=virtualGroupMetrics(),plans=[];
  let budget=480;
  // Read geometry before changing card nodes; never materialize a preceding prefix.
  for(const g of VIEW_GROUPS){
    const entry=groupSections.get(g.key);if(!entry)continue;
    const rect=entry.body.getBoundingClientRect(),focused=g.key===focusKey;
    let start=0,end=0;
    if(!collapsedGroups.has(g.key)&&(focused||rect.bottom>=viewport.top-500&&rect.top<=viewport.bottom+500)){
      const firstRow=focused?0:Math.max(0,Math.floor((viewport.top-500-rect.top)/metrics.row));
      start=Math.min(g.members.length,firstRow*metrics.columns);
      const rows=Math.ceil((grid.clientHeight+1000)/metrics.row)+1;
      end=Math.min(g.members.length,start+Math.min(CHUNK,rows*metrics.columns),start+budget);
      budget-=end-start;
    }
    plans.push({g,entry,start,end});
  }
  for(const {g,entry,start,end} of plans){
    if(entry.start===start&&entry.end===end)continue;
    const fragment=document.createDocumentFragment();
    for(let i=start;i<end;i++)fragment.appendChild(cardFor(g.members[i],0,false));
    entry.body.replaceChildren(fragment);entry.start=start;entry.end=end;
    entry.body.style.paddingTop=(end>start?Math.floor(start/metrics.columns)*metrics.row:0)+'px';
  }
  // Ratings/selections/drafts live in library records, not this disposable cache.
  for(const [path,card] of cardCache)if(!card.isConnected&&cardCache.size>720)cardCache.delete(path);
  renderedN=document.querySelectorAll('#grid .card').length;flushRings();
}
function queueGroupFill(){
  const generation=groupViewGeneration;
  requestAnimationFrame(()=>{
    if(generation!==groupViewGeneration)return;
    if(virtualGroupedView()){renderVirtualGroups();return;}
    if(virtualFlatView()){renderVirtualFlat();return;}
    const grid=$('grid');
    if(groupedView()&&renderedN<ranked.length&&grid.scrollTop+grid.clientHeight>=grid.scrollHeight-600)renderChunk(false);
  });
}
function makeGroupSection(g){
  const section=document.createElement('section');section.className='photoGroup';section.dataset.groupKey=g.key;
  const header=document.createElement('div');header.className='groupHeader';
  const heading=document.createElement('div');heading.className='groupHeading';
  const title=document.createElement('h3');title.textContent=g.title;
  const summary=document.createElement('span');summary.className='groupSummary';
  heading.appendChild(title);heading.appendChild(summary);header.appendChild(heading);
  const actions=document.createElement('div');actions.className='groupActions';
  const button=(text,fn)=>{const b=document.createElement('button');b.type='button';b.className='btn ghost';b.textContent=text;b.onclick=fn;actions.appendChild(b);return b;};
  if(g.group!==null)button('组内对比',()=>openGroupCompare(g.group));
  const select=button('整组选中',()=>selectViewGroup(g.key));
  const collapse=button('收起',()=>toggleGroupCollapse(g.key));
  const body=document.createElement('div');body.className='groupPhotos';body.id='photo-group-'+encodeURIComponent(g.key);
  collapse.setAttribute('aria-controls',body.id);header.appendChild(actions);
  section.appendChild(header);section.appendChild(body);
  return {section,body,title,summary,select,collapse};
}
function appendGroupedCards(start,end){
  let offset=0;
  for(const g of VIEW_GROUPS){
    const from=Math.max(start,offset),to=Math.min(end,offset+g.members.length);
    if(from<to){
      let entry=groupSections.get(g.key);
      if(!entry){entry=makeGroupSection(g);groupSections.set(g.key,entry);$('grid').appendChild(entry.section);}
      for(let i=from;i<to;i++)entry.body.appendChild(cardFor(ranked[i],i,false));
    }
    offset+=g.members.length;if(offset>=end)break;
  }
  updateGroupSummaries();
}
function rebuildView(){
  groupViewGeneration++;
  groupJumpTop=null;
  ranked=sortPhotos(filterPhotos(library));
  const end=Math.min(ranked.length,Math.max(CHUNK,renderedN));
  const grid=$('grid');grid.dataset.layout=groupedView()?'groups':'flat';
  grid.style.gridAutoRows='';grid.style.alignContent='';
  for(const card of cardCache.values())card.style.height='';
  groupSections.clear();VIEW_GROUPS=[];
  if(groupedView()){
    VIEW_GROUPS=viewGroups(ranked);ranked=VIEW_GROUPS.flatMap(g=>g.members);
    grid.replaceChildren();
    if(virtualGroupedView()){setupVirtualGroups();renderVirtualGroups();}
    else appendGroupedCards(0,end);
  }else if(virtualFlatView()){
    renderVirtualFlat();
  }else{
    const frag=document.createDocumentFragment();
    for(let i=0;i<end;i++)frag.appendChild(cardFor(ranked[i],i,false));
    grid.replaceChildren(frag);
  }
  if(!virtualGroupedView()&&!virtualFlatView())renderedN=end;queueGroupFill();
  syncGroupNavigation();
  flushRings();updateTargetUI();updateStats();
}
function rememberView(){try{localStorage.setItem(SORT_KEY,sortMode);localStorage.setItem(FILTER_KEY,filterMode);}catch(e){}projectChanged();}
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
  updateGroupSummaries();
  syncBatchControls();
  const total=library.length;
  $('filterEmpty').hidden=!total||ranked.length>0;
  $('groupEmptyHint').textContent=filterMode==='groups'&&!library.some(isGroup)?
    (PROJECT.scanIncomplete?'上次扫描中断，分组尚未完成；可直接重建分组，不必重新评分。':'当前尚无相似分组，可使用“识别相似”重新检测。'):'';
  const unknown=library.filter(p=>(sizeFilter.minEdge!==null||sizeFilter.maxEdge!==null)&&!(p.original_width>0&&p.original_height>0)||
    (sizeFilter.minMB!==null||sizeFilter.maxMB!==null)&&typeof p.file_size!=='number').length;
  $('sizeFilterStatus').textContent=(sizeFilterActive()?`大小条件匹配 ${library.filter(matchesSizeFilter).length} / ${total} 张。范围外照片不会参与批量加星、移除或删除。`:'大小不限；只过滤显示，不删除文件。')+
    (unknown?` ${unknown} 张缺少原图尺寸或文件大小，当前暂不显示；重新扫描可补齐。`:'')+' 与上方照片筛选同时生效；1 MB = 1024 KB。';
  if(!total){$('scanInfo').textContent='';return;}
  const visible=ranked.length;
  const selection=selectionCounts();
  const recommended=library.filter(autoSel).length;
  $('scanInfo').textContent=`共 ${total} 张 · 当前显示 ${visible} · 当前可见已选 ${selection.visible} · 全部已选 ${selection.total}`;
  $('scanInfo').title=`共 ${total} 张 · 推荐 ${recommended} 张 · 隐藏已选 ${selection.hidden} 张`;
  // 窗口标题随项目更新（WKWebView/多窗口/Spotlight 切换时有用）
  const folder=baseName(PROJECT.folder)||'PhotoPilot';
  document.title=`${folder} · ${total} 张 — PhotoPilot`;
}
function updateTargetUI(){
  const info=$('targetInfo'),clear=$('clearTarget');
  if(targetPath){
    info.textContent='目标：'+baseName(targetPath);
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
  targetPath=path;updateTargetUI();projectChanged();
  toast('已设为追色目标：'+baseName(path));
}
function clearTarget(){
  targetPath=null;updateTargetUI();projectChanged();toast('已清除追色目标');
}
function setActionButtonsDisabled(disabled){
  ['colorBtn','polishBtn','presetBtn'].forEach(id=>{const b=$(id);if(b)b.disabled=disabled;});
  syncBatchControls();
}
function aiSmart(){
  if(CULL_HISTORY.applying)return;
  const before=new Set(selectedPaths);
  const keep=ranked.filter(p=>!p.flags.some(f=>BAD.includes(f)||f==='连拍重复')
    &&(p.ai!=null&&p.ai>=5.2||p.ai==null&&p.score>=0.55));
  selectionTouched=true;selectedPaths=new Set(keep.map(p=>p.path));
  document.querySelectorAll('.card').forEach(c=>c.classList.toggle('sel',selectedPaths.has(c.dataset.path)));
  const n=keep.length;
  recordSelection(before,'智能筛选');updateStats();projectChanged();
  toast(`智能筛选：保留 ${n} / ${ranked.length} 张${keep.length?' · 快审微调即可':''}`);}
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
  const isTarget=p.path===targetPath,targetButton=card.querySelector('.targetBtn');
  card.classList.toggle('target',isTarget);
  if(targetButton){targetButton.classList.toggle('on',isTarget);targetButton.textContent=isTarget?'目标照片':'设为目标';targetButton.setAttribute('aria-pressed',String(isTarget));}
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
  let group=badges.querySelector('.groupBadge');
  if(isGroup(p)){
    if(!group){group=document.createElement('span');group.className='pill groupBadge';badges.prepend(group);}
    group.textContent=`相似 ${p.group_rank}/${p.group_size}`;
  }else if(group)group.remove();
  let best=card.querySelector('.groupRecommended');
  if(p.group_best&&isGroup(p)){
    if(!best){best=document.createElement('span');best.className='groupRecommended';card.appendChild(best);}
    best.textContent='组内推荐';
  }else if(best)best.remove();
  let cmp=card.querySelector('.compareBtn');
  if(p.group_best&&isGroup(p)){
    if(!cmp){cmp=document.createElement('button');cmp.type='button';cmp.className='compareBtn';card.querySelector('.meta').appendChild(cmp);}
    cmp.textContent=`比较 ${Math.min(4,p.group_size)} 张`;
    cmp.onclick=e=>{e.stopPropagation();openGroupCompare(p.group);};
  }else if(cmp)cmp.remove();
  card.classList.toggle('sel',selectedPaths.has(p.path));
  syncCardRating(card,p);
}

function mergePhoto(incoming){
  let photo=libraryByPath.get(incoming.path);
  if(photo){const rating=photo.rating;Object.assign(photo,incoming);photo.rating=rating;}
  else{photo={...incoming};libraryByPath.set(photo.path,photo);}
  return photo;
}

function cardFor(p,idx=0,animate=false){
  let card=cardCache.get(p.path);
  if(!card){
    card=makeCard(p,idx,animate);
    cardCache.set(p.path,card);
  }else{
    updateCardInPlace(card,p);
  }
  card.classList.toggle('sel',selectedPaths.has(p.path));
  return card;
}

function makeCard(p,idx,animate=true){
  const ringC=scoreRingColor(p), flags=flagsHtml(p);
  const photoName=escapeHtml(baseName(p.path)),thumbUrl=escapeHtml(p.thumb);
  const d=document.createElement('div');
  d.className='card'+(selectedPaths.has(p.path)?' sel':'');d.dataset.path=p.path;
  d.tabIndex=0;d.setAttribute('aria-label',baseName(p.path)+'，按空格快览');
  d.onfocus=()=>{activePhotoPath=p.path;};
  if(animate)d.style.animationDelay=Math.min((idx%24)*25,400)+'ms';
  else d.style.animation='none';
  const group=p.group_size>=2?`<span class="pill groupBadge">相似 ${p.group_rank}/${p.group_size}</span>`:'';
  const compare=p.group_best&&p.group_size>=2?`<button class="compareBtn" type="button" title="并排比较这组照片">比较 ${Math.min(4,p.group_size)} 张</button>`:'';
  const recommend=p.group_best&&isGroup(p)?'<span class="groupRecommended">组内推荐</span>':'';
  d.innerHTML=`<div class="th"><img loading="lazy" src="${thumbUrl}" alt="${photoName}" onload="this.parentElement.classList.add('ld')"></div>
    <button class="targetBtn${targetPath===p.path?' on':''}" type="button" aria-pressed="${targetPath===p.path?'true':'false'}" title="将这张照片作为追色目标">${targetPath===p.path?'目标照片':'设为目标'}</button>
    <button class="starBtn${p.rating?' on':''}" type="button" aria-haspopup="true" aria-expanded="false" aria-label="设置星级 · 当前 ${p.rating||0} 星" title="设置星级 · 当前 ${p.rating||0} 星">${p.rating?'★'+p.rating:'☆'}</button>
    <div class="ratingMenu" role="group" aria-label="设置照片星级" hidden>
      <button type="button" data-rating="1" aria-pressed="${p.rating===1?'true':'false'}" title="1 星">1★</button>
      <button type="button" data-rating="2" aria-pressed="${p.rating===2?'true':'false'}" title="2 星">2★</button>
      <button type="button" data-rating="3" aria-pressed="${p.rating===3?'true':'false'}" title="3 星">3★</button>
      <button type="button" data-rating="4" aria-pressed="${p.rating===4?'true':'false'}" title="4 星">4★</button>
      <button type="button" data-rating="5" aria-pressed="${p.rating===5?'true':'false'}" title="5 星">5★</button>
      <button type="button" class="clearRating" data-rating="0" aria-pressed="${!p.rating?'true':'false'}" title="清除星级">清除</button>
    </div>
    ${recommend}
    <div class="ck">✓</div>
    <div class="meta">
      <div class="cardIdentity"><div class="name" title="${photoName}">${photoName}</div><span class="ratingSummary${p.rating?'':' unrated'}">${p.rating?'★ '+p.rating:'未评级'}</span></div>
      <div class="cardQuality"><div class="ring" style="--ring-c:${ringC};--p:0" data-p="${p.score*100}"><b>${p.score.toFixed(2).slice(1)}</b></div><div class="flags">${flags}</div></div>
      <div class="badges">${group}${p.faces?`<span class="pill face">${p.faces} 脸${p.blinks?' · '+p.blinks+' 闭眼':''}</span>`:''}${p.ai!=null?`<span class="pill ai">AI ${p.ai.toFixed(1)}</span>`:''}</div>${compare}
    </div>`;
  d.onclick=()=>{if(CULL_HISTORY.applying)return;activePhotoPath=p.path;d.focus?.({preventScroll:true});const before=new Set(selectedPaths);selectionTouched=true;d.classList.toggle('sel');
    if(d.classList.contains('sel'))selectedPaths.add(p.path);else selectedPaths.delete(p.path);
    recordSelection(before,'选择照片');
    updateStats();projectChanged();};
  d.querySelector('.targetBtn').onclick=e=>{e.stopPropagation();setTarget(p.path);};
  d.querySelector('.starBtn').onclick=e=>{e.stopPropagation();toggleRatingMenu(p,d);};
  d.querySelectorAll('.ratingMenu [data-rating]').forEach(choice=>choice.onclick=e=>{
    e.stopPropagation();chooseCardRating(p,Number(choice.dataset.rating),d);
  });
  const cmp=d.querySelector('.compareBtn');
  if(cmp)cmp.onclick=e=>{e.stopPropagation();openGroupCompare(p.group);};
  d.ondblclick=()=>{const i=ranked.findIndex(x=>x.path===p.path);if(i>=0)lbShow(i);};
  d.addEventListener('contextmenu',e=>{
    e.preventDefault();e.stopPropagation();
    revealPath(p.path);
  });
  ringQ.push(d.querySelector('.ring'));
  return d;}

async function scan(opt){
  const f=$('folder').value.trim();
  if(!f&&!(opt&&opt.paths))return toast('请先输入路径、拖入照片或点「文件夹…」');
  SCAN_ACTIVE=true;syncBatchControls();
  const req=++SCAN_REQ_GEN;
  const previousJob=JOB;
  if(previousJob)api('/api/scan_cancel',{job:previousJob}).catch(()=>{});
  POLL_GEN++;JOB=null;POLL_FAIL=0;
  $('scanBtn').disabled=true;$('empty').style.display='none';
  beginProjectScan(null,!!opt?.incomplete);
  $('scanbar').classList.add('show');$('cancelscan').style.display='inline-block';
  $('scanfill').style.width='0%';$('scantext').textContent='正在读取照片列表…';POLL_FAIL=0;RECOVERED=!!(opt&&opt.recovery);
  SCAN_SPEC={paths:opt&&opt.paths,recursive:opt&&opt.recursive,incomplete:!!opt?.incomplete};
  try{
    const j=await api('/api/scan',{folder:f||'.',jobs:4,
      paths:opt&&opt.paths?opt.paths:undefined,
      faces:$('facesOpt').checked,ai:$('aiOpt').checked,
      similarity_profile:$('similarityProfile').value||'balanced',
      recursive:opt&&opt.recursive!==undefined?opt.recursive:$('recursiveOpt').checked});
    if(req!==SCAN_REQ_GEN){api('/api/scan_cancel',{job:j.job}).catch(()=>{});return;}
    PROJECT.pendingScan.folder=j.folder;$('folder').value=j.folder;
    JOB=j.job;syncBatchControls();startPoll();
  }catch(e){
    if(req!==SCAN_REQ_GEN)return;
    finishProjectScan(false);scanReset();toast('出错：'+e.message);
  }}

function scanReset(){
  $('scanBtn').disabled=false;
  $('scanbar').classList.remove('show');
  $('cancelscan').style.display='none';
  JOB=null;SCAN_ACTIVE=false;syncBatchControls();}

let POLL_FAIL=0, POLL_GEN=0, RECOVERED=false, FINAL_STAGE='';
function startPoll(){POLL_GEN++;const g=POLL_GEN;poll(g);}   // 新任务=新一代；旧循环自动作废
async function poll(g){
  if(!JOB||g!==POLL_GEN)return;                 // 代际不符：僵尸循环退出
  try{
    const requestedJob=JOB;
    const j=await api('/api/scan_status?job='+JOB+'&since='+since+'&updates_since='+updatesSince+'&final_stage='+FINAL_STAGE);
    if(g!==POLL_GEN||JOB!==requestedJob)return;
    updatesSince=j.updates_since??updatesSince;
    POLL_FAIL=0;
    const g2=$('grid');
    if(j.photos&&j.photos.length){
      acceptProjectScan(j.photos);
      const frag=document.createDocumentFragment();
      for(const incoming of j.photos){const p=mergePhoto(incoming);ranked.push(p);library.push(p);
        if(!selectionTouched&&autoSel(p))selectedPaths.add(p.path);
        if(ranked.length<=Math.max(CHUNK,renderedN)){
          frag.appendChild(cardFor(p,ranked.length,true));renderedN=ranked.length;}}
      if(groupedView())rebuildView();else{g2.dataset.layout='flat';g2.appendChild(frag);}
      since+=j.photos.length;flushRings();checkpointScan();}
    if(j.updates&&j.updates.length){         // 精扫段：评分原地更新
      for(const u of j.updates){
        const p=libraryByPath.get(u.path);
        if(!p)continue;
        mergePhoto(u);
        updateCardInPlace(cardCache.get(p.path),p);
      }
      flushRings();updateStats();checkpointScan();
      $('scantext').textContent='AI 精扫中 '+j.refine_done+'/'+j.refine_total+' …（可先选片）';
      $('scanInfo').textContent='精扫 '+j.refine_done+'/'+j.refine_total;}
    const pct=j.total?Math.min(100,j.done/j.total*100):0;
    $('scanfill').style.width=pct+'%';
    const eta=j.done>2?fmtT(j.elapsed/j.done*(j.total-j.done)):'…';
    if(!j.finished){
      $('scantext').textContent=`扫描中 ${j.done}/${j.total} · 剩余约 ${eta}`
        +(j.skipped?` · 跳过 ${j.skipped}`:'')
        +(j.recursive_auto?' · 已自动搜索子文件夹':'');
      $('scanInfo').textContent=`${j.done}/${j.total}`;
      setTimeout(()=>poll(g),600);return;}
    const finalStage=j.refine_pending?'fast':'refined';
    if(j.final&&j.final.length&&FINAL_STAGE!==finalStage){   // 无可用新照片时保留旧项目
      acceptProjectScan(j.final);
      FINAL_STAGE=finalStage;
      library=j.final.map(mergePhoto);
      if(!selectionTouched)selectedPaths=new Set(library.filter(autoSel).map(p=>p.path));
      rebuildView();checkpointScan();}
    if(j.refine_pending){                      // 极速段完成，精扫继续
      $('scantext').textContent=`出图完成 ${ranked.length} 张 · AI 精扫中（可先选片）`;
      setTimeout(()=>poll(g),1200);return;}
    const noReadablePhotos=(typeof PROJECT==='undefined'||!PROJECT.pendingScan?.accepted)&&!(j.final&&j.final.length);
    const retainedCount=library.length;
    $('scantext').textContent=(j.cancelled?'已取消 · ':'')+`完成 ${ranked.length} 张`
      +(j.skipped?` · 跳过 ${j.skipped}`:'')
      +(j.recursive_auto?' · 已自动搜索子文件夹':'');
    updateStats();
    finishProjectScan(!j.finalize_error&&!j.cancelled&&!j.skipped&&j.done>=j.total&&!PROJECT.pendingScan?.inputIncomplete);
    scanReset();
    if(j.finalize_error){
      $('status').textContent='扫描失败：'+j.finalize_error+' · 请检查目录权限后重试';
      toast('扫描失败：'+j.finalize_error);return;
    }
    if(noReadablePhotos){
      $('scantext').textContent=retainedCount?`扫描完成 0 张 · 原有 ${retainedCount} 张已保留`:'扫描完成 0 张 · 目录无可读取照片';
      $('scanInfo').textContent=retainedCount?`${retainedCount} 张已保留`:'0 张';
      const recursiveNote=j.recursive_auto?'（已检查子文件夹）':'';
      toast(retainedCount?`此目录没有可读取的照片${recursiveNote}；原有 ${retainedCount} 张照片已保留`
        :j.recursive_auto?'此目录及子文件夹都没有可读取的照片':'此目录没有可读取的照片');
      return;
    }
    toast(j.cancelled?'已取消，已保留已扫部分':j.recursive_auto
      ?`已自动扫描子文件夹，共读取 ${ranked.length} 张照片`:library.length?'扫描完成':'此目录没有可读取的照片');
  }catch(e){
    if(g!==POLL_GEN)return;                    // 旧循环的失败不处理
    POLL_FAIL=(POLL_FAIL||0)+1;
    if(POLL_FAIL<=3){$('scantext').textContent='扫描状态查询重试中…';setTimeout(()=>poll(g),1200);return;}
    // 任务丢失（引擎重启等）：有目录上下文时自动重扫一次
    if(!RECOVERED&&$('folder').value.trim()){
      RECOVERED=true;POLL_FAIL=0;
      toast('扫描任务丢失，自动重新扫描…');
      $('status').textContent='任务丢失，自动恢复中…';
      scan({...SCAN_SPEC,recovery:true});return;}
    finishProjectScan(false);scanReset();POLL_FAIL=0;
    $('scantext').textContent='';
    $('status').textContent='扫描中断：'+e.message+' · 请重新点「扫描打分」';
    toast('扫描中断：'+e.message);
  }}

function cancelScan(){if(JOB){$('scantext').textContent='正在取消…';api('/api/scan_cancel',{job:JOB}).catch(e=>toast('取消失败：'+e.message));}}

function renderChunk(animate=true){
  if(virtualGroupedView()){renderVirtualGroups();return;}
  if(virtualFlatView()){renderVirtualFlat();return;}
  if(groupedView()&&VIEW_GROUPS.length&&VIEW_GROUPS.every(g=>collapsedGroups.has(g.key)))return;
  const g=$('grid');const frag=document.createDocumentFragment();
  const end=Math.min(ranked.length,renderedN+CHUNK);
  if(groupedView())appendGroupedCards(renderedN,end);
  else{for(let i=renderedN;i<end;i++)frag.appendChild(cardFor(ranked[i],i,animate));g.appendChild(frag);}
  renderedN=end;flushRings();queueGroupFill();}

const grid=$('grid');
let renderScrollQueued=false;
grid.addEventListener('scroll',()=>{
  if(renderScrollQueued)return;
  renderScrollQueued=true;
  requestAnimationFrame(()=>{
    renderScrollQueued=false;
    syncGroupNavigation(true);
    if(virtualGroupedView()){renderVirtualGroups();return;}
    if(virtualFlatView()){renderVirtualFlat();return;}
    if(renderedN<ranked.length
       &&grid.scrollTop+grid.clientHeight>=grid.scrollHeight-600)renderChunk();
  });
});
let virtualResizeQueued=false;
window.addEventListener('resize',()=>{
  if(ranked.length<1200||virtualResizeQueued)return;
  virtualResizeQueued=true;
  requestAnimationFrame(()=>{virtualResizeQueued=false;rebuildView();});
});
// 窗口尺寸记忆：resize 防抖后回传原生侧持久化（pywebview 不可用时静默）
let _geomTimer=null;
window.addEventListener('resize',()=>{
  clearTimeout(_geomTimer);
  _geomTimer=setTimeout(()=>{
    try{
      if(window.pywebview&&window.pywebview.api&&window.pywebview.api.save_geometry){
        window.pywebview.api.save_geometry(window.innerWidth,window.innerHeight);
      }
    }catch(e){}
  },600);
});

function selAll(mode){
  if(CULL_HISTORY.applying)return;
  const before=new Set(selectedPaths);
  selectionTouched=true;
  if(mode==1)selectedPaths=new Set(ranked.map(p=>p.path));
  else if(mode==0)selectedPaths=new Set(ranked.filter(autoSel).map(p=>p.path));
  else selectedPaths.clear();
  document.querySelectorAll('.card').forEach(c=>{
    c.classList.toggle('sel',selectedPaths.has(c.dataset.path));});
  recordSelection(before,'批量选择');updateStats();projectChanged();
}

/* 分段控件 */
function bindSeg(id,onPick){
  const seg=$(id);const n=seg.querySelectorAll('button').length;
  seg.style.setProperty('--segn',n);
  seg.querySelectorAll('button').forEach((b,i)=>b.onclick=()=>{
    seg.querySelectorAll('button').forEach(x=>x.classList.remove('on'));b.classList.add('on');
    seg.querySelector('.thumb').style.transform=`translateX(${i*100}%)`;
    onPick&&onPick(b.dataset.v);});}
bindSeg('wbSeg',v=>{$('rowWbStrength').hidden=v=='off';projectChanged();});

/* ── 导入：原生选择 / 拖拽 → 预处理 → 上传 → 自动扫描 ── */
const RAW_EXT=new Set(['arw','cr2','cr3','nef','nrw','dng','raf','orf','rw2','pef','srw','x3f']);
const OK_EXT=new Set(['jpg','jpeg','png','webp','tif','tiff','bmp',...RAW_EXT]);
async function prepareBlob(file){
  const ext=(file.name.split('.').pop()||'').toLowerCase();
  if(!OK_EXT.has(ext))return null;
  return file; // Preserve original pixels, extension, EXIF and ICC; resize only previews.
}

let uploadAbort=null;
async function uploadFiles(list){
  const files=[...list].filter(f=>OK_EXT.has((f.name.split('.').pop()||'').toLowerCase()));
  if(!files.length)return toast('没有可导入的照片文件');
  const req=++SCAN_REQ_GEN;POLL_GEN++;
  if(JOB)api('/api/scan_cancel',{job:JOB}).catch(()=>{});
  JOB=null;
  const batch='import-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,8);
  let folder=null,ok=0,done=0;
  setActionButtonsDisabled(true);
  const CONC=4;let idx=0;
  const worker=async()=>{
    while(idx<files.length&&req===SCAN_REQ_GEN){
      const i=idx++;
      $('status').textContent=`导入中 ${done}/${files.length} · ${files[i].name}`;
      try{
        const blob=await prepareBlob(files[i]);
        if(!blob)continue;
        const relativeName=files[i]._photoPilotRelativePath||files[i].webkitRelativePath||files[i].name;
        const r=await fetch('/api/upload',{method:'POST',
          headers:{'X-Filename':encodeURIComponent(relativeName),'X-Batch':batch},
          body:blob});
        const j=await r.json();
        if(j.error){toast('跳过 '+files[i].name+'：'+j.error);continue;}
        folder=j.folder;ok++;
      }catch(e){toast('导入出错：'+e.message)}
      finally{done++;}
    }
  };
  await Promise.all(Array.from({length:Math.min(CONC,files.length)},worker));
  if(req!==SCAN_REQ_GEN)return;
  setActionButtonsDisabled(false);
  if(!ok){$('status').textContent='';return toast('没有照片被导入');}
  $('folder').value=folder;
  const hasNested=files.some(f=>(f._photoPilotRelativePath||f.webkitRelativePath||'').includes('/'));
  $('status').textContent=`导入完成 ${ok} 张，正在扫描…`;
  scan({recursive:$('recursiveOpt').checked||hasNested,incomplete:ok<files.length});
}

async function importLocalPaths(paths){
  // 桌面模式：本机路径直导（不拷贝不重编码）+ 服务端一步启动扫描
  SCAN_ACTIVE=true;syncBatchControls();
  const req=++SCAN_REQ_GEN;
  if(JOB)api('/api/scan_cancel',{job:JOB}).catch(()=>{});
  POLL_GEN++;JOB=null;POLL_FAIL=0;RECOVERED=false;
  beginProjectScan(null);
  try{
    const j=await api('/api/import_paths',{paths,
      recursive:$('recursiveOpt').checked,
      faces:$('facesOpt').checked,ai:$('aiOpt').checked,similarity_profile:$('similarityProfile').value||'balanced'});
    if(req!==SCAN_REQ_GEN){api('/api/scan_cancel',{job:j.job}).catch(()=>{});return;}
    $('status').textContent=`已导入 ${j.count} 张（本机直导）`;
    $('folder').value=j.folder;PROJECT.pendingScan.folder=j.folder;
    $('scanBtn').disabled=true;$('empty').style.display='none';
    SCAN_SPEC={paths:j.files,recursive:$('recursiveOpt').checked};
    $('scanbar').classList.add('show');$('cancelscan').style.display='inline-block';
    $('scanfill').style.width='0%';$('scantext').textContent='正在读取照片…';POLL_FAIL=0;RECOVERED=false;
    JOB=j.job;syncBatchControls();startPoll();         // 服务端已开扫，这里只轮询
  }catch(e){
    if(req!==SCAN_REQ_GEN)return;
    finishProjectScan(false);scanReset();toast('导入失败：'+e.message);
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

let PROCESS_BUSY=false,PROCESS_GEN=0,lastFailedPaths=[],lastOperation=null,CURRENT_EDITOR=null;
const TONE_CONTROLS=[
  ['exposure','曝光 EV',-3,3,.05],['contrast','对比度',-1,1,.05],
  ['highlights','高光',-1,1,.05],['shadows','阴影',-1,1,.05],
  ['temperature','冷暖',-1,1,.05],['saturation','饱和度',-1,1,.05]
];
const RETOUCH_CONTROLS=[['skin','磨皮'],['retain','纹理保留'],['clarity','清晰度'],['face_repair','人脸修复'],['blemish','瑕疵修复'],['ai_denoise','AI 降噪']];
const RETOUCH_KEYS=[...RETOUCH_CONTROLS.map(([key])=>key),'local_region','brush_mask'];
const RETOUCH_REGIONS=['all','skin','face','eyes','background','brush'];
function retouchValues(){return {...Object.fromEntries(RETOUCH_CONTROLS.map(([key])=>[key,+$('edit-retouch-'+key).value])),local_region:$('editRegion').value,brush_mask:CURRENT_EDITOR?.brushMask??null};}
function bindEditorGesture(input){
  input.onpointerdown=()=>beginEditorGesture('pointer');
  input.onkeydown=e=>{if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End','PageUp','PageDown'].includes(e.key))beginEditorGesture('keyboard');};
  input.onchange=()=>{if(!CURRENT_EDITOR?.gesture?.kind)flushEditorGesture();};
  input.onpointerup=input.onpointercancel=input.onblur=()=>flushEditorGesture();
  input.onkeyup=e=>{if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End','PageUp','PageDown'].includes(e.key))flushEditorGesture();};
}
function buildRetouchControls(editor){
  for(const [key,label] of RETOUCH_CONTROLS){
    const row=document.createElement('div');row.className='row';const id='edit-retouch-'+key;
    row.innerHTML=`<label for="${id}">${label}<span class="val" id="${id}-value"></span></label><input type="range" id="${id}" min="0" max="1" step="0.01" value="${editor.retouch[key]}">`;
    const input=row.querySelector('input');bindEditorGesture(input);
    input.oninput=()=>{slide(input,id+'-value');queueEditorRetouch();};
    $('editRetouchSliders').appendChild(row);slide(input,id+'-value');
    if(key==='ai_denoise'){
      const hint=document.createElement('div');hint.className='subhint';hint.id='aiDenoiseHint';
      hint.textContent='AI 降噪（NAFNet）按整图运行，CPU 较慢，默认关闭；模型缺失时自动跳过。适合高 ISO / 暗光噪点明显的照片。';
      $('editRetouchSliders').appendChild(hint);
    }
  }
  $('editRegion').value=editor.retouch.local_region;
  $('editRegion').onchange=()=>{flushEditorGesture();queueEditorRetouch();flushEditorGesture();};
  $('editMask').checked=false;$('editMask').onchange=toggleEditorMask;
  syncEditorRetouch(editor);selectEditorPanel('tone');
}
function selectEditorPanel(panel){
  const editor=CURRENT_EDITOR;if(!editor||editor.operation==='color')return;
  const portrait=panel==='portrait';
  $('editTonePanel').hidden=portrait;$('editPortraitControls').hidden=!portrait;
  $('editToneTab').setAttribute('aria-selected',String(!portrait));$('editPortraitTab').setAttribute('aria-selected',String(portrait));
}
function toneValues(prefix='tone-'){
  return Object.fromEntries(TONE_CONTROLS.map(([key])=>[key,+$(prefix+key).value]));
}
function buildToneControls(container,prefix,values={},onChange=null){
  for(const [key,label,min,max,step] of TONE_CONTROLS){
    const row=document.createElement('div');row.className='row';
    row.innerHTML=`<label for="${prefix+key}">${label}<span class="val" id="${prefix+key}-value"></span></label>
      <input type="range" id="${prefix+key}" min="${min}" max="${max}" step="${step}" value="${values[key]||0}">`;
    const input=row.querySelector('input');
    input.oninput=()=>{slide(input,prefix+key+'-value');onChange&&onChange();};
    if(prefix==='edit-')bindEditorGesture(input);
    container.appendChild(row);slide(input,prefix+key+'-value');
  }
}
function resetTone(){
  if(CURRENT_EDITOR?.exporting)return;
  for(const [key] of TONE_CONTROLS){const el=$('tone-'+key);el.value=0;slide(el,'tone-'+key+'-value');}
  projectChanged();toast('基础调色已重置');
}
function recipeValues(){
  return {version:1,tone:toneValues(),preset:$('presetSelect').value,
    sliders:Object.fromEntries(['skin','retain','clarity','wb','faceRepair','blemish','strength','wbStrength'].map(key=>[key,+$(key).value])),
    autoTone:$('autoTone').checked,localRegion:$('localRegion').value,
    wbMode:$('wbSeg').querySelector('button.on').dataset.v};
}
function saveRecipe(){
  const url=URL.createObjectURL(new Blob([JSON.stringify(recipeValues(),null,2)],{type:'application/json'}));
  const link=document.createElement('a');link.href=url;link.download='PhotoPilot-参数.json';link.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);toast('已保存原创调色与美化参数');
}
function validateRecipe(recipe){
  if(!recipe||recipe.version!==1)throw new Error('不支持此参数版本');
  const sliderIds=['skin','retain','clarity','wb','faceRepair','blemish','strength','wbStrength'];
  if(!recipe.sliders||typeof recipe.sliders!=='object'||Array.isArray(recipe.sliders)
    ||Object.keys(recipe.sliders).some(key=>!sliderIds.includes(key)))throw new Error('参数控件不合法');
  const values={...Object.fromEntries(TONE_CONTROLS.map(([key])=>['tone-'+key,recipe.tone?.[key]??0])),...recipe.sliders};
  for(const [id,value] of Object.entries(values)){
    const input=$(id);
    if(!input||input.type!=='range'||typeof value!=='number'||!Number.isFinite(value)||value<+input.min||value>+input.max)throw new Error('参数范围不合法');
  }
  if(![...$('presetSelect').options].some(o=>o.value===recipe.preset)||typeof recipe.autoTone!=='boolean'
    ||!['all','skin','face','eyes','background'].includes(recipe.localRegion)||!['batch','single','off'].includes(recipe.wbMode))throw new Error('参数选项不合法');
  return values;
}
function applyRecipe(recipe){
  const values=validateRecipe(recipe);
  // 先选预设，再直接写入保存值；恢复时不触发预设的默认参数覆盖。
  $('presetSelect').value=recipe.preset;
  for(const [id,value] of Object.entries(values)){const el=$(id);el.value=value;el.dispatchEvent(new Event('input'));}
  $('autoTone').checked=recipe.autoTone;$('localRegion').value=recipe.localRegion;
  const buttons=[...$('wbSeg').querySelectorAll('button')],index=buttons.findIndex(b=>b.dataset.v===recipe.wbMode);
  buttons.forEach((button,i)=>button.classList.toggle('on',i===index));
  $('wbSeg').querySelector('.thumb').style.transform=`translateX(${index*100}%)`;
  $('rowWbStrength').hidden=recipe.wbMode==='off';
}
async function loadRecipe(file){
  if(!file||CURRENT_EDITOR?.exporting)return;
  try{
    if(file.size>65536)throw new Error('参数文件过大');
    const text=await file.text();
    if(CURRENT_EDITOR?.exporting)return;
    const recipe=JSON.parse(text);applyRecipe(recipe);projectChanged();
    toast('参数已载入，可批量应用到选中照片');
  }catch(e){toast('无法载入参数：'+e.message);}
}
function retryFailed(){if(lastFailedPaths.length)process(lastOperation,lastFailedPaths.slice());}

const TONE_HISTORY_LIMIT=50;
function validTone(tone){
  return tone&&typeof tone==='object'&&!Array.isArray(tone)
    &&Object.keys(tone).length===TONE_CONTROLS.length
    &&TONE_CONTROLS.every(([key,,min,max])=>typeof tone[key]==='number'&&Number.isFinite(tone[key])&&tone[key]>=min&&tone[key]<=max);
}
function sameTone(a,b){return TONE_CONTROLS.every(([key])=>a[key]===b[key]);}
function validRetouch(value){
  return value&&typeof value==='object'&&!Array.isArray(value)&&Object.keys(value).length===RETOUCH_KEYS.length
    &&RETOUCH_CONTROLS.every(([key])=>typeof value[key]==='number'&&Number.isFinite(value[key])&&value[key]>=0&&value[key]<=1)
    &&RETOUCH_REGIONS.includes(value.local_region)
    &&(value.brush_mask===null||(typeof value.brush_mask==='string'&&value.brush_mask.length<8*1024*1024));
}
function sameRetouch(a,b){return RETOUCH_KEYS.every(key=>a[key]===b[key]);}
function sameEditorState(editor,tone,retouch){return sameTone(editor.tone,tone)&&sameRetouch(editor.retouch,retouch);}
function editorBaseSignature(settings){
  const {folder,paths,export_size,export_quality,polish,...base}=settings;
  return JSON.stringify({...base,polish:Object.fromEntries(Object.entries(polish).filter(([key])=>!TONE_CONTROLS.some(([tone])=>tone===key)))});
}
function editorDraft(path,operation,signature,initialRetouch){
  const draft=EDIT_DRAFTS.get(path);
  if(!draft||![1,2].includes(draft.version)||draft.operation!==operation||draft.baseSignature!==signature
    ||!validTone(draft.tone)||!Array.isArray(draft.history)||!draft.history.length||draft.history.length>TONE_HISTORY_LIMIT
    ||!Number.isInteger(draft.index)||draft.index<0||draft.index>=draft.history.length
    ||!draft.history.every(validTone)||!sameTone(draft.tone,draft.history[draft.index]))return null;
  const retouch=draft.version===1?initialRetouch:draft.retouch;
  const retouchHistory=draft.version===1?draft.history.map(()=>({...initialRetouch})):draft.retouchHistory;
  if(!validRetouch(retouch)||!Array.isArray(retouchHistory)||retouchHistory.length!==draft.history.length
    ||!retouchHistory.every(validRetouch)||!sameRetouch(retouch,retouchHistory[draft.index]))return null;
  return {tone:{...draft.tone},history:draft.history.map(tone=>({...tone})),retouch:{...retouch},retouchHistory:retouchHistory.map(value=>({...value})),index:draft.index};
}
function persistEditorDraft(editor){
  if(!editor||editor.operation==='color')return;
  EDIT_DRAFTS.set(editor.path,{version:2,operation:editor.operation,baseSignature:editor.baseSignature,
    tone:{...editor.tone},history:editor.history.map(tone=>({...tone})),retouch:{...editor.retouch},retouchHistory:editor.retouchHistory.map(value=>({...value})),index:editor.index});
}
function updateEditorHistory(){
  const editor=CURRENT_EDITOR,disabled=!editor||editor.operation==='color'||editor.exporting;
  $('editUndo').disabled=disabled||!(editor.index>0||!sameEditorState(editor,editor.history[editor.index],editor.retouchHistory[editor.index]));
  $('editRedo').disabled=disabled||!!editor.gesture||editor.index>=editor.history.length-1;
  $('editReset').disabled=disabled;
}
function syncEditorTone(editor){
  for(const [key] of TONE_CONTROLS){
    for(const prefix of ['edit-','tone-']){const input=$(prefix+key);input.value=editor.tone[key];slide(input,prefix+key+'-value');}
  }
}
function syncEditorRetouch(editor){
  for(const [key] of RETOUCH_CONTROLS){const input=$('edit-retouch-'+key);input.value=editor.retouch[key];slide(input,'edit-retouch-'+key+'-value');}
  $('editRegion').value=editor.retouch.local_region;
  syncBrushUI();
}
function beginEditorGesture(kind=null){
  const editor=CURRENT_EDITOR;if(!editor||editor.operation==='color'||editor.exporting)return;
  if(kind==='keyboard'&&editor.gesture?.kind==='keyboard')return;
  flushEditorGesture();
  editor.gesture={kind,entry:null,baseIndex:editor.index,baseHistory:editor.history.map(tone=>({...tone})),baseRetouchHistory:editor.retouchHistory.map(value=>({...value}))};
}
// Snapshots commit the latest value without splitting a still-active drag or key repeat.
function flushEditorGesture(end=true){
  const editor=CURRENT_EDITOR;if(!editor||editor.operation==='color')return;
  const gesture=editor.gesture;
  if(gesture){
    if(sameEditorState(editor,gesture.baseHistory[gesture.baseIndex],gesture.baseRetouchHistory[gesture.baseIndex])){
      editor.history=gesture.baseHistory.map(tone=>({...tone}));editor.retouchHistory=gesture.baseRetouchHistory.map(value=>({...value}));editor.index=gesture.baseIndex;gesture.entry=null;
    }else if(gesture.entry!==null){
      editor.history[gesture.entry]={...editor.tone};editor.index=gesture.entry;
      editor.retouchHistory[gesture.entry]={...editor.retouch};
    }else{
      editor.history=editor.history.slice(0,editor.index+1);editor.history.push({...editor.tone});
      editor.retouchHistory=editor.retouchHistory.slice(0,editor.index+1);editor.retouchHistory.push({...editor.retouch});
      if(editor.history.length>TONE_HISTORY_LIMIT){editor.history.shift();editor.retouchHistory.shift();}
      editor.index=editor.history.length-1;gesture.entry=editor.index;
    }
    if(end)editor.gesture=null;
    persistEditorDraft(editor);
  }
  updateEditorHistory();
}
function moveEditorHistory(delta){
  const editor=CURRENT_EDITOR;if(!editor||editor.operation==='color'||editor.exporting)return;
  flushEditorGesture();
  const index=editor.index+delta;if(index<0||index>=editor.history.length)return;
  editor.index=index;editor.tone={...editor.history[index]};editor.retouch={...editor.retouchHistory[index]};editor.brushMask=editor.retouch.brush_mask??null;syncEditorTone(editor);syncEditorRetouch(editor);
  editor.revision++;hideEditorMask(editor);persistEditorDraft(editor);updateEditorHistory();scheduleEditorPreview(editor);projectChanged();
}
function undoEditorTone(){moveEditorHistory(-1);}
function redoEditorTone(){moveEditorHistory(1);}
function editorShortcut(event){
  const editor=CURRENT_EDITOR;
  if(!editor||!$('results').classList.contains('show')||editor.operation==='color'||!(event.metaKey||event.ctrlKey)||event.altKey)return false;
  const target=event.target||document.activeElement;
  if(target?.isContentEditable||target?.closest?.('[contenteditable="true"], [contenteditable=""]')
    ||/textarea/i.test(target?.tagName||'')||(/input/i.test(target?.tagName||'')&&!['range','checkbox'].includes(target.type)))return false;
  const key=event.key.toLowerCase();if(key!=='z'&&key!=='y')return false;
  event.preventDefault();if(key==='y'||event.shiftKey)redoEditorTone();else undoEditorTone();return true;
}

async function process(operation,retryPaths=null){
  if(PROCESS_BUSY)return;
  let sel=retryPaths||ranked.filter(p=>selectedPaths.has(p.path)).map(p=>p.path);
  if(!sel.length)return toast('未选中任何照片');
  if(operation==='color'){
    if(!targetPath)return toast('请选择一张目标照片：点击卡片上的「设为目标」');
    sel=sel.filter(p=>p!==targetPath);
    if(!sel.length)return toast('请再选至少一张要追色的照片');
  }
  if(operation==='preset' && !$('presetSelect').value)return toast('请选择一个风格预设');
  if(sel.length>1&&!exportQueueReady())return;
  const polish={skin:+$('skin').value,retain:+$('retain').value,
                clarity:+$('clarity').value,wb:+$('wb').value,
                face_repair:+$('faceRepair').value,blemish:+$('blemish').value,
                local_region:$('localRegion').value,
                auto_tone:$('autoTone').checked,ai_denoise:0,...toneValues()};
  let prep;
  flushEditorGesture();clearTimeout(editorTimer);hideEditorMask(CURRENT_EDITOR);CURRENT_EDITOR=null;
  const generation=++PROCESS_GEN;
  PROCESS_BUSY=true;lastFailedPaths=[];lastOperation=operation;$('retryBtn').hidden=true;
  setActionButtonsDisabled(true);$('prog').classList.add('show');
  $('pnum').textContent='0 / '+sel.length;
  $('progDesc').textContent=operation==='color'?'解析目标照片…':operation==='preset'?'准备风格预设…':'准备美化参数…';$('pfill').style.width='0%';
  try{
    const preset=operation==='preset'?$('presetSelect').value:null;
    const settings={folder:$('folder').value.trim(),paths:sel,
      operation:operation==='preset'?'both':operation,
      preset,
      algo:operation==='color'?$('algo').value:(operation==='preset'?'oklab':'none'),strength:operation==='color'||operation==='preset'?+$('strength').value:0,
      preserve_luma:$('preserveLuma').checked,
      skin_protect:$('skinProtect').checked,
      ref:operation==='color'?targetPath:null,
      wb_mode:operation==='color'?'off':$('wbSeg').querySelector('button.on').dataset.v,
      wb_strength:+$('wbStrength').value,
      export_size:$('exportSize').value,export_quality:+$('exportQuality').value,polish};
    const signature=editorBaseSignature(settings),single=sel.length===1;
    if(!single){await submitExportQueue(settings);return;}
    const openingRetouch=Object.fromEntries(RETOUCH_KEYS.map(key=>[key,key==='brush_mask'?null:polish[key]]));
    const draft=single&&operation!=='color'?editorDraft(sel[0],operation,signature,openingRetouch):null;
    const initialTone=draft?.tone||toneValues();
    const initialRetouch=draft?.retouch||openingRetouch;
    prep=await api('/api/prepare',settings);
    if(generation!==PROCESS_GEN)return;
    $('progDesc').textContent=prep.ref_desc;
    const inner=$('resInner');
    $('results').classList.toggle('single-result',single);
    const refDesc=escapeHtml(prep.ref_desc);
    inner.innerHTML=single
      ?`<h2 id="resTitle"><span>单张修图 · ${refDesc}</span><button class="btn ghost resultClose" type="button" onclick="closeResults()">完成 Esc</button></h2>
        <div class="editorLayout"><div id="editStage" style="display:flex;min-height:0"></div>
          <aside class="editorTools"><h3>大图调节</h3>
            <div id="editWarnings" class="subhint" role="status" aria-live="polite" hidden></div>
            <div class="editorControls">
            ${operation!=='color'?`<div class="editorTabs" role="tablist" aria-label="编辑参数">
              <button class="btn ghost" id="editToneTab" type="button" role="tab" aria-controls="editTonePanel" aria-selected="true" onclick="selectEditorPanel('tone')">基础调色</button>
              <button class="btn ghost" id="editPortraitTab" type="button" role="tab" aria-controls="editPortraitControls" aria-selected="false" onclick="selectEditorPanel('portrait')">人像与局部</button></div>`:''}
            <div id="editTonePanel" role="tabpanel" aria-labelledby="editToneTab"><div id="editToneControls"></div>
            <button class="btn ghost" id="editReset" type="button" onclick="resetEditorTone()">重置调色</button></div>
            ${operation!=='color'?`<div id="editPortraitControls" role="tabpanel" aria-labelledby="editPortraitTab" hidden>
              <div id="editRetouchSliders"></div><div class="row"><label for="editRegion">局部区域</label><select id="editRegion"><option value="all">全图</option><option value="skin">肤色区域</option><option value="face">人脸区域</option><option value="eyes">眼周区域</option><option value="background">背景区域</option><option value="brush">画笔区域</option></select></div>
              <div id="editBrushControls" hidden>
                <div class="row"><label>画笔大小 <span class="val" id="editBrushSize-value">60</span></label><input type="range" id="editBrushSize" min="8" max="300" step="2" value="60" oninput="slide(this,'editBrushSize-value')"></div>
                <div class="row"><label>羽化 <span class="val" id="editBrushFeather-value">0.5</span></label><input type="range" id="editBrushFeather" min="0" max="1" step="0.05" value="0.5" oninput="slide(this,'editBrushFeather-value')"></div>
                <div class="brushActions"><button class="btn ghost" id="editBrushPaint" type="button" onclick="setBrushMode('paint')">画笔</button><button class="btn ghost" id="editBrushErase" type="button" onclick="setBrushMode('erase')">橡皮擦</button><button class="btn ghost" id="editBrushClear" type="button" onclick="clearBrushMask()">清除</button></div>
                <div class="subhint">在左侧预览图上按住拖动即可手刷区域；仅作用清晰度/磨皮/修复，曝光白平衡与追色仍作用于全图。</div>
              </div>
              <label class="editorMaskToggle" title="蓝色仅表示区域范围；磨皮/修脸还受人脸和肤色识别限制；不会导出"><input id="editMask" type="checkbox" aria-describedby="editMaskHelp">显示区域蒙版</label>
              <div class="subhint" id="editMaskHelp">蓝色仅表示区域范围；磨皮/修脸还受人脸和肤色识别限制；不会导出</div>
              <button class="btn ghost" id="editRetouchReset" type="button" onclick="resetEditorRetouch()">恢复人像参数</button></div>`:''}
            <div class="subhint">拖动图片上的分割线对比。当前是预览；导出采用左侧选择的成品尺寸。调色参数会同步到批量处理。</div></div>
            <div class="editorFooter"><div class="editorHistory" role="group" aria-label="编辑历史">
              <button class="btn ghost" id="editUndo" type="button" onclick="undoEditorTone()" disabled title="撤销编辑（⌘/Ctrl Z）">撤销</button>
              <button class="btn ghost" id="editRedo" type="button" onclick="redoEditorTone()" disabled title="重做编辑（⌘/Ctrl Shift Z 或 Ctrl Y）">重做</button></div>
            <button class="btn" id="editExport" type="button" onclick="exportEditor()">导出成品</button>
            <div id="editStatus" role="status" aria-live="polite">预览已就绪</div></div></aside></div>`
      :`<h2 id="resTitle">处理结果 · ${refDesc} <button class="btn ghost" type="button" onclick="closeResults()">完成 Esc</button></h2>`;
    let done=0,success=0;
    for(const p of sel){
      $('pnum').textContent=`${done} / ${prep.count}`;
      $('pfill').style.width=(done/prep.count*100)+'%';
      try{
        const r=await api('/api/process_one',{sid:prep.sid,path:p,preview_size:single?1600:640,preview_only:single,
          ...(single&&operation!=='color'?{adjustments:{...initialTone},retouch_adjustments:{...initialRetouch}}:{})});
        if(generation!==PROCESS_GEN)return;
        done++;success++;
        const pair=document.createElement('div');pair.className='pair';
        pair.innerHTML=`<div class="cmp"><img alt="原始照片" src="${escapeHtml(r.before)}"><img alt="编辑预览" class="b" src="${escapeHtml(r.after)}">
          ${single&&operation!=='color'?'<img id="editRegionMask" alt="区域范围蒙版" class="region-mask" hidden>':''}
          ${single&&operation!=='color'?'<canvas id="editBrushCanvas" class="brush-canvas" hidden></canvas>':''}
          <div class="bar"></div><div class="knob">◄►</div>
          <span class="tag l">原始</span><span class="tag r">成品</span></div>
          <div class="name" style="padding:8px 4px 0">${escapeHtml(baseName(p))}</div>`;
        bindCmp(pair.querySelector('.cmp'));
        if(single){
          $('editStage').appendChild(pair);
          CURRENT_EDITOR={sid:prep.sid,path:p,revision:0,renderEpoch:0,busy:false,previewPending:false,exporting:false,operation,
            settings,baseSignature:signature,tone:{...initialTone},history:draft?.history||[{...initialTone}],
            initialRetouch:{...openingRetouch},retouch:{...initialRetouch},retouchHistory:draft?.retouchHistory||[{...initialRetouch}],
            index:draft?.index||0,gesture:null,showMask:false,maskFrame:null,brushMask:initialRetouch.brush_mask??null};
          if(operation!=='color'){
            buildToneControls($('editToneControls'),'edit-',initialTone,queueEditorPreview);
            buildRetouchControls(CURRENT_EDITOR);hideEditorMask(CURRENT_EDITOR);
            syncEditorTone(CURRENT_EDITOR);
          }
          else $('editToneControls').textContent='追色结果 · 目标照片保持不变';
          updateEditorHistory();
          showEditorWarnings(r);
        }else inner.appendChild(pair);
      }catch(e){if(generation!==PROCESS_GEN)return;done++;lastFailedPaths.push(p);console.warn(e);toast(baseName(p)+'：'+e.message);}
    }
    $('pnum').textContent=`${done} / ${prep.count}`;
    $('pfill').style.width='100%';
    $('prog').classList.remove('show');
    if(success)$('results').classList.add('show');
    $('retryBtn').hidden=!lastFailedPaths.length;
    $('status').textContent=single&&success?'单张预览已打开 · 确认效果后导出':`成功 ${success}/${prep.count}`
      +(lastFailedPaths.length?` · 失败 ${lastFailedPaths.length}，可重试`:' · 成品在 photopilot_out/');
    if(success)toast(single?'预览已就绪 · 可直接调节并导出':`成功导出 ${success} 张`);
  }catch(e){if(generation===PROCESS_GEN){$('prog').classList.remove('show');toast('出错：'+e.message);$('status').textContent='';}}
  finally{PROCESS_BUSY=false;setActionButtonsDisabled(false);$('prog').classList.remove('show');renderExportQueue();}
}

let editorTimer=null;
function hideEditorMask(editor){
  if(!editor||editor.operation==='color')return;
  editor.maskFrame=null;$('editRegionMask').hidden=true;
}
// ---------- 局部画笔 ----------
let BRUSH_MODE='paint';       // paint | erase
function brushCanvas(){return $('editBrushCanvas');}
function brushModeEl(){return $('editBrushPaint');}
function brushEraseEl(){return $('editBrushErase');}
function setBrushMode(mode){
  if(CURRENT_EDITOR?.exporting)return;
  BRUSH_MODE=mode;
  brushModeEl().classList.toggle('on',mode==='paint');
  brushEraseEl().classList.toggle('on',mode==='erase');
}
function brushVisible(){
  const editor=CURRENT_EDITOR;
  return !!(editor&&editor.operation!=='color'&&editor.retouch.local_region==='brush');
}
function syncBrushUI(){
  const visible=brushVisible();
  $('editBrushControls').hidden=!visible;
  const canvas=brushCanvas();if(canvas)canvas.hidden=!visible;
  if(visible)setupBrushCanvas();
}
function setupBrushCanvas(){
  const canvas=brushCanvas();if(!canvas)return;
  if(canvas._brushDrawing)return;
  const cmp=canvas.closest('.cmp');if(!cmp)return;
  const img=cmp.querySelector('img.b');
  // canvas 覆盖在预览图上，尺寸对齐 object-fit:contain 后的实际图片区域，
  // 保证掩码长宽比与原图一致（后端统一 resize 到原图分辨率，不变形）。
  const rect=cmp.getBoundingClientRect();
  if(rect.width<=0||rect.height<=0)return;
  let dw=rect.width,dh=rect.height,dx=0,dy=0;
  if(img&&img.naturalWidth>0&&img.naturalHeight>0){
    const scale=Math.min(rect.width/img.naturalWidth,rect.height/img.naturalHeight);
    dw=img.naturalWidth*scale;dh=img.naturalHeight*scale;
    dx=(rect.width-dw)/2;dy=(rect.height-dh)/2;
  }
  canvas.style.left=dx+'px';canvas.style.top=dy+'px';
  canvas.style.width=dw+'px';canvas.style.height=dh+'px';
  canvas.width=Math.round(dw);canvas.height=Math.round(dh);
  const ctx=canvas.getContext('2d');
  const editor=CURRENT_EDITOR,mask=editor?.brushMask,epoch=(canvas._maskEpoch||0)+1;canvas._maskEpoch=epoch;
  canvas._maskLoading=!!mask;canvas.style.cursor=mask?'wait':'crosshair';
  ctx.clearRect(0,0,canvas.width,canvas.height);
  if(mask){
    const m=new Image();
    m.onload=()=>{if(CURRENT_EDITOR===editor&&editor.brushMask===mask&&canvas._maskEpoch===epoch){ctx.drawImage(m,0,0,canvas.width,canvas.height);canvas._maskLoading=false;canvas.style.cursor='crosshair';}};
    m.onerror=()=>{if(CURRENT_EDITOR===editor&&canvas._maskEpoch===epoch){canvas._maskLoading=false;canvas.style.cursor='crosshair';$('editStatus').textContent='画笔蒙版加载失败，请撤销或清除后重试';}};
    m.src=mask;
  }
  if(canvas._bound)return;
  canvas._bound=true;
  let drawing=false,last=null,activeEditor=null;
  const pos=e=>{
    const r=canvas.getBoundingClientRect();
    return {x:(e.clientX-r.left)*canvas.width/r.width,y:(e.clientY-r.top)*canvas.height/r.height};
  };
  const stroke=from=>{
    if(!last||!from)return;
    const size=+($('editBrushSize')?.value||60);
    const feather=+($('editBrushFeather')?.value||0.5);
    const c=canvas.getContext('2d');
    c.save();
    c.globalCompositeOperation=BRUSH_MODE==='erase'?'destination-out':'source-over';
    c.strokeStyle='rgba(255,255,255,1)';
    c.lineWidth=size;
    c.lineCap='round';c.lineJoin='round';
    c.shadowColor='rgba(255,255,255,1)';
    c.shadowBlur=size*feather;
    c.beginPath();c.moveTo(last.x,last.y);c.lineTo(from.x,from.y);c.stroke();
    c.beginPath();c.arc(from.x,from.y,size/2,0,Math.PI*2);c.fillStyle='rgba(255,255,255,1)';c.fill();
    c.restore();
  };
  canvas.onpointerdown=e=>{
    if(!brushVisible()||CURRENT_EDITOR?.exporting||canvas._maskLoading)return;
    // The before/after splitter also listens on the parent. Keep it from
    // stealing pointer capture, otherwise real pointerup never commits a stroke.
    e.preventDefault();e.stopPropagation();
    activeEditor=CURRENT_EDITOR;
    canvas._maskEpoch=(canvas._maskEpoch||0)+1;canvas._brushDrawing=true;
    canvas.setPointerCapture(e.pointerId);drawing=true;last=pos(e);stroke(last);last=pos(e);
  };
  canvas.onpointermove=e=>{
    if(!drawing||CURRENT_EDITOR!==activeEditor||activeEditor.exporting||!brushVisible())return;const p=pos(e);stroke(p);last=p;
  };
  const end=()=>{
    if(!drawing)return;drawing=false;canvas._brushDrawing=false;last=null;
    const owner=activeEditor;activeEditor=null;if(CURRENT_EDITOR===owner)commitBrushMask();
  };
  canvas.onpointerup=canvas.onpointercancel=end;
}
function commitBrushMask(){
  const editor=CURRENT_EDITOR;if(!editor||editor.operation==='color'||editor.exporting)return;
  const canvas=brushCanvas();if(!canvas)return;
  const data=canvas.toDataURL('image/png');
  editor.brushMask=data;
  editor.retouch={...editor.retouch,brush_mask:data};
  editor.revision++;hideEditorMask(editor);
  if(!editor.gesture)beginEditorGesture();
  updateEditorHistory();scheduleEditorPreview(editor);projectChanged();
}
function clearBrushMask(){
  const editor=CURRENT_EDITOR;if(!editor||editor.operation==='color'||editor.exporting)return;
  const canvas=brushCanvas();
  if(canvas)canvas._maskEpoch=(canvas._maskEpoch||0)+1;
  if(canvas){canvas._maskLoading=false;canvas.style.cursor='crosshair';}
  if(canvas)canvas.getContext('2d').clearRect(0,0,canvas.width,canvas.height);
  editor.brushMask=null;editor.retouch={...editor.retouch,brush_mask:null};
  editor.revision++;hideEditorMask(editor);
  if(!editor.gesture)beginEditorGesture();
  updateEditorHistory();scheduleEditorPreview(editor);projectChanged();
}
function toggleEditorMask(){
  const editor=CURRENT_EDITOR;if(!editor||editor.operation==='color')return;
  if(editor.exporting){$('editMask').checked=editor.showMask;return;}
  editor.showMask=$('editMask').checked;editor.revision++;hideEditorMask(editor);scheduleEditorPreview(editor);
}
function renderEditorFrame(editor,result,revision,epoch){
  const image=$('editStage').querySelector('img.b');
  hideEditorMask(editor);image.onload=null;image.src=result.after;
  // 画笔层需在图片加载后按其自然尺寸对齐（object-fit:contain 的 letterbox）
  image.addEventListener('load',function brushSetup(){
    if(CURRENT_EDITOR!==editor||editor.revision!==revision||editor.renderEpoch!==epoch)return;
    if(brushVisible())setupBrushCanvas();
  },{once:true});
  if(brushVisible())requestAnimationFrame(()=>setupBrushCanvas());
  if(!editor.showMask||typeof result.region_mask!=='string'||!result.region_mask)return;
  // Each detached image belongs to one response, so an old decode can never
  // mark a newer frame as loaded. The displayed image must also finish loading.
  const frame={revision,epoch,after:result.after,mask:result.region_mask,afterLoaded:false,displayLoaded:false,maskLoaded:false,displayMaskLoaded:false};
  editor.maskFrame=frame;
  const displayedMask=$('editRegionMask');
  const current=()=>CURRENT_EDITOR===editor&&editor.maskFrame===frame&&editor.revision===revision&&editor.renderEpoch===epoch&&editor.showMask&&!editor.exporting;
  const reveal=()=>{
    if(!current()||!frame.afterLoaded||!frame.displayLoaded||!frame.maskLoaded||!frame.displayMaskLoaded)return;
    displayedMask.hidden=false;
  };
  image.onload=()=>{
    if(!current()||(image.getAttribute('src')||image.src)!==frame.after||(image.currentSrc&&image.currentSrc!==image.src))return;
    frame.displayLoaded=true;reveal();
  };
  const after=new Image(),mask=new Image();
  after.onload=()=>{if(!current())return;frame.afterLoaded=true;reveal();};
  mask.onload=()=>{if(!current())return;frame.maskLoaded=true;reveal();};
  after.onerror=mask.onerror=()=>{if(current())hideEditorMask(editor);};
  image.onerror=()=>{if(current())hideEditorMask(editor);};
  displayedMask.onload=()=>{
    if(!current()||(displayedMask.getAttribute('src')||displayedMask.src)!==frame.mask
      ||(displayedMask.currentSrc&&displayedMask.currentSrc!==displayedMask.src))return;
    frame.displayMaskLoaded=true;reveal();
  };
  displayedMask.onerror=()=>{if(current())hideEditorMask(editor);};
  displayedMask.src=frame.mask;
  after.src=frame.after;mask.src=frame.mask;
  if(image.complete&&image.naturalWidth>0)image.onload();
  if(displayedMask.complete&&displayedMask.naturalWidth>0)displayedMask.onload();
}
function showEditorWarnings(result){
  const node=$('editWarnings');
  const warnings=Array.isArray(result.warnings)?result.warnings.filter(value=>typeof value==='string'):[];
  node.textContent=warnings.join(' ');node.hidden=!warnings.length;
}
function scheduleEditorPreview(editor){
  editor.previewPending=true;
  clearTimeout(editorTimer);
  editorTimer=setTimeout(()=>{if(CURRENT_EDITOR===editor)refreshEditorPreview();},350);
}
function queueEditorPreview(){
  const editor=CURRENT_EDITOR;if(!editor||editor.operation==='color')return;
  const tone=toneValues('edit-');
  if(editor.exporting||!validTone(tone)){syncEditorTone(editor);return;}
  if(sameTone(tone,editor.tone))return;
  if(!editor.gesture)beginEditorGesture();
  editor.tone={...tone};editor.revision++;hideEditorMask(editor);syncEditorTone(editor);updateEditorHistory();
  scheduleEditorPreview(editor);projectChanged();
}
function queueEditorRetouch(){
  const editor=CURRENT_EDITOR;if(!editor||editor.operation==='color')return;
  const retouch=retouchValues();
  if(editor.exporting||!validRetouch(retouch)){syncEditorRetouch(editor);return;}
  if(sameRetouch(retouch,editor.retouch))return;
  if(!editor.gesture)beginEditorGesture();
  editor.retouch={...retouch};editor.revision++;hideEditorMask(editor);syncEditorRetouch(editor);updateEditorHistory();
  scheduleEditorPreview(editor);projectChanged();
}
async function refreshEditorPreview(){
  const editor=CURRENT_EDITOR;if(!editor||editor.busy||editor.exporting)return;
  clearTimeout(editorTimer);editor.previewPending=false;
  editor.busy=true;const revision=editor.revision,epoch=editor.renderEpoch;
  const adjustments={...editor.tone};
  $('editStatus').textContent='正在更新预览…';
  try{
    const result=await api('/api/process_one',{sid:editor.sid,path:editor.path,preview_only:true,
      preview_size:1600,adjustments:editor.operation==='color'?{}:adjustments,
      ...(editor.operation!=='color'?{retouch_adjustments:{...editor.retouch},...(editor.showMask?{show_mask:true}:{})}:{})});
    if(CURRENT_EDITOR!==editor||revision!==editor.revision||epoch!==editor.renderEpoch||editor.exporting)return;
    renderEditorFrame(editor,result,revision,epoch);
    showEditorWarnings(result);
    $('editStatus').textContent='预览已更新 · 确认后导出';
  }catch(e){if(CURRENT_EDITOR===editor&&revision===editor.revision&&epoch===editor.renderEpoch&&!editor.exporting){hideEditorMask(editor);$('editStatus').textContent='预览失败：'+e.message;}}
  finally{editor.busy=false;if(CURRENT_EDITOR===editor&&!editor.exporting&&editor.previewPending)refreshEditorPreview();}
}
function resetEditorTone(){
  if(!CURRENT_EDITOR||CURRENT_EDITOR.operation==='color'||CURRENT_EDITOR.exporting)return;
  flushEditorGesture();
  for(const [key] of TONE_CONTROLS){const el=$('edit-'+key);el.value=0;slide(el,'edit-'+key+'-value');}
  queueEditorPreview();flushEditorGesture();
}
function resetEditorRetouch(){
  const editor=CURRENT_EDITOR;if(!editor||editor.operation==='color'||editor.exporting)return;
  flushEditorGesture();beginEditorGesture();
  editor.retouch={...editor.initialRetouch};editor.brushMask=editor.retouch.brush_mask??null;syncEditorRetouch(editor);editor.revision++;hideEditorMask(editor);
  flushEditorGesture();scheduleEditorPreview(editor);projectChanged();
}
function lockEditorExport(editor){
  const ids=['editExport','editReset','editUndo','editRedo','exportSize','exportQuality','recipeFile',
    'skin','retain','clarity','wb','faceRepair','blemish','strength','wbStrength','autoTone','localRegion','presetSelect','algo','preserveLuma','skinProtect'];
  for(const [key] of TONE_CONTROLS)ids.push('tone-'+key,...(editor.operation==='color'?[]:['edit-'+key]));
  if(editor.operation!=='color')ids.push(...RETOUCH_CONTROLS.map(([key])=>'edit-retouch-'+key),'editRegion','editMask','editRetouchReset','editBrushSize','editBrushFeather','editBrushPaint','editBrushErase','editBrushClear');
  editor.lockedControls=new Map([...ids.map($),...$('wbSeg').querySelectorAll('button'),...document.querySelectorAll('.toneActions button')].filter(Boolean).map(input=>[input,input.disabled]));
  for(const input of editor.lockedControls.keys())input.disabled=true;
}
function unlockEditorExport(editor){
  if(!editor?.lockedControls)return;
  for(const [input,disabled] of editor.lockedControls)input.disabled=disabled;
  editor.lockedControls=null;
}
async function exportEditor(){
  const editor=CURRENT_EDITOR;if(!editor||editor.exporting)return;
  flushEditorGesture();clearTimeout(editorTimer);editor.previewPending=false;editor.renderEpoch++;hideEditorMask(editor);
  const adjustments=editor.operation==='color'?{}:{...editor.tone};
  const retouch=editor.operation==='color'?null:{...editor.retouch};
  const settings={...editor.settings,paths:[editor.path],export_size:$('exportSize').value,export_quality:+$('exportQuality').value};
  if(retouch)settings.polish={...editor.settings.polish,...Object.fromEntries(Object.entries(retouch).filter(([key])=>key!=='brush_mask'))};
  editor.exporting=true;lockEditorExport(editor);updateEditorHistory();
  $('editStatus').textContent='正在渲染成品…';
  try{
    // Prepared sessions freeze output settings. Prepare the selected size/quality before exporting.
    const prepared=await api('/api/prepare',settings);
    if(CURRENT_EDITOR!==editor)return;
    const r=await api('/api/process_one',{sid:prepared.sid,path:editor.path,adjustments,...(retouch?{retouch_adjustments:retouch}:{})});
    if(CURRENT_EDITOR!==editor)return;
    showEditorWarnings(r);
    $('editStatus').textContent=`已导出 ${r.width} × ${r.height} · `;
    const link=document.createElement('a');link.href=r.after;link.download=baseName(r.out);link.textContent='下载成品';$('editStatus').appendChild(link);
    $('status').textContent='已导出成品：'+r.out;toast('成品已保存');
  }catch(e){if(CURRENT_EDITOR===editor)$('editStatus').textContent='导出失败：'+e.message;}
  finally{editor.exporting=false;unlockEditorExport(editor);if(CURRENT_EDITOR===editor)updateEditorHistory();}
}

function closeResults(){
  EXPORT_QUEUE.reviewGeneration++;
  flushEditorGesture();clearTimeout(editorTimer);hideEditorMask(CURRENT_EDITOR);unlockEditorExport(CURRENT_EDITOR);CURRENT_EDITOR=null;PROCESS_GEN++;
  $('prog').classList.remove('show');
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

const pendingRatings=new Set();
function syncCardRating(card,p){
  if(card){
    const button=card.querySelector('.starBtn');
    if(button){button.disabled=pendingRatings.has(p.path);button.classList.toggle('on',!!p.rating);button.textContent=p.rating?'★'+p.rating:'☆';
      button.title=`设置星级 · 当前 ${p.rating||0} 星`;button.setAttribute('aria-label',button.title);}
    const menu=card.querySelector('.ratingMenu');
    if(menu)menu.querySelectorAll('[data-rating]').forEach(choice=>choice.setAttribute('aria-pressed',String(Number(choice.dataset.rating)===(p.rating||0))));
    const summary=card.querySelector('.ratingSummary');
    if(summary){summary.textContent=p.rating?'★ '+p.rating:'未评级';summary.classList.toggle('unrated',!p.rating);}
  }
}
function syncRatingUI(p){
  syncCardRating(cardCache.get(p.path),p);
  document.querySelectorAll('.comparePane').forEach(pane=>{
    if(pane.dataset.path!==p.path)return;
    const button=pane.querySelector('.compareStar');
    if(button){button.classList.toggle('on',!!p.rating);button.textContent=p.rating?'★':'☆';
      button.setAttribute('aria-pressed',p.rating?'true':'false');}
  });
  if($('lb').classList.contains('show')&&ranked[LBI]?.path===p.path)lbRenderStars(p.rating||0);
  syncCompareMetadata();
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
function closeRatingMenus(){
  document.querySelectorAll('.ratingMenu').forEach(menu=>{
    menu.hidden=true;const card=menu.closest('.card'),button=card&&card.querySelector('.starBtn');
    if(button)button.setAttribute('aria-expanded','false');
  });
}
document.addEventListener('pointerdown',e=>{
  const target=e.target;
  if(target?.closest?.('.ratingMenu')||target?.closest?.('.starBtn'))return;
  closeRatingMenus();
});
function toggleRatingMenu(p,card){
  const menu=card&&card.querySelector('.ratingMenu'),button=card&&card.querySelector('.starBtn');
  if(!menu||!button)return;
  const open=menu.hidden;closeRatingMenus();menu.hidden=!open;button.setAttribute('aria-expanded',String(open));
}
function chooseCardRating(p,rating,card){
  const menu=card&&card.querySelector('.ratingMenu'),button=card&&card.querySelector('.starBtn');
  if(menu)menu.hidden=true;if(button)button.setAttribute('aria-expanded','false');
  return setRating(p,rating);
}
async function setRating(p,rating,options={}){
  if(CULL_HISTORY.applying&&!options.historyReplay)return false;
  const next=Number(rating);
  if(!Number.isInteger(next)||next<0||next>5)throw new Error('星级必须是 0–5');
  if(pendingRatings.has(p.path))return false;
  if((p.rating||0)===next)return true;
  const generation=libraryGeneration;
  const isCurrent=()=>generation===libraryGeneration&&(libraryByPath.get(p.path)===p||(!libraryByPath.has(p.path)&&library.includes(p)));
  pendingRatings.add(p.path);
  const previousRating=p.rating||0;
  pendingRatingPrevious.set(p.path,previousRating);
  p.rating=next;syncRatingUI(p);
  try{
    await api('/api/rate',{path:p.path,rating:next,score:p.score,flags:p.flags,
      sharpness:p.sharpness,exposure:p.exposure,faces:p.faces});
    // XMP 写入已经成功，但旧项目回执不得改变后来加载的项目。
    if(!isCurrent())return true;
    pendingRatingPrevious.delete(p.path);if(!options.deferView)refreshRatedView(p);projectChanged();
    if(options.recordHistory!==false)pushCullingHistory({type:'ratings',label:'设置星级',changes:[{path:p.path,before:previousRating,after:next}]});
    if(!options.silent)toast(next?'已设置 '+next+' 星':'已清除星级');
    return true;
  }catch(e){if(isCurrent()){p.rating=previousRating;syncRatingUI(p);if(!options.silent)toast('写评分失败：'+e.message);}return false;}
  finally{if(isCurrent())pendingRatingPrevious.delete(p.path);pendingRatings.delete(p.path);if(isCurrent())syncRatingUI(p);syncCullingHistoryButtons();}
}
async function toggleStar(p){
  return setRating(p,p.rating?0:5);
}
async function applyBatchRating(rating){
  if(BATCH_BUSY||CULL_HISTORY.applying)return 0;
  const next=Number(rating);
  if(!Number.isInteger(next)||next<0||next>5){toast('请选择 1–5 星，或选择清除星级');return 0;}
  const photos=selectedPhotos();
  if(!photos.length){toast('请先选择照片');return 0;}
  if(selectionCounts(photos).hidden&&!window.confirm(`为全部选中的 ${photos.length} 张照片${next?'设置 '+next+' 星':'清除星级'}？${selectionScopeNotice(photos)}`))return 0;
  BATCH_BUSY=true;syncBatchControls();
  const generation=libraryGeneration;
  const previous=photos.map(p=>p.rating||0);
  try{
    const results=new Array(photos.length);let cursor=0;
    await Promise.all(Array.from({length:Math.min(8,photos.length)},async()=>{
      while(cursor<photos.length&&generation===libraryGeneration){const index=cursor++;results[index]=await setRating(photos[index],next,{silent:true,deferView:true,recordHistory:false});}
    }));
    if(generation!==libraryGeneration)return results.filter(Boolean).length;
    const succeeded=results.filter(Boolean).length;
    pushCullingHistory({type:'ratings',label:'批量星级',changes:photos.flatMap((p,i)=>results[i]&&previous[i]!==next?[{path:p.path,before:previous[i],after:next}]:[])});
    if(sortMode==='rating'||filterMode==='starred')rebuildView();
    toast(succeeded===photos.length?`已为 ${succeeded} 张照片设置 ${next?next+' 星':'清除星级'}`:
      `评分完成 ${succeeded}/${photos.length} 张；失败项可重试`);
    return succeeded;
  }finally{BATCH_BUSY=false;syncBatchControls();}
}
function applyBatchRatingFromControl(){
  const value=$('batchRating').value;
  if(value==='')return toast('请先选择 1–5 星或清除星级');
  return applyBatchRating(Number(value));
}
function syncBatchControls(){
  const selection=selectionCounts(),count=selection.total;
  $('batchSelectionCount').textContent=`${sizeFilterActive()?'大小范围内已选':'全部已选'} ${count} · 当前可见 ${selection.visible} · 隐藏 ${selection.hidden}`;
  $('batchSelectionCount').title=sizeFilterActive()?'大小范围外的照片不会参与批量操作；其他筛选隐藏的已选照片会在确认时提示':'批量加星、从项目移除、移到废纸篓/回收站均作用于全部选中照片，包括筛选隐藏的照片';
  const busy=BATCH_BUSY||CULL_HISTORY.applying;
  $('clearHiddenSelection').hidden=!selection.hidden;
  $('clearHiddenSelection').disabled=busy;
  $('batchRating').disabled=!count||busy;
  $('batchRateBtn').disabled=!count||busy;
  const editorBusy=!!(CURRENT_EDITOR?.exporting&&selectedPaths.has(CURRENT_EDITOR.path));
  const exportBusy=[...EXPORT_QUEUE.jobs.values()].some(job=>['queued','running','paused'].includes(job.state));
  const commonReasons=[];
  if(!count)commonReasons.push('请先在网格中选中照片');
  if(busy)commonReasons.push('批量操作正在进行，请等待完成');
  if(PROJECT.blocked)commonReasons.push('项目处于「恢复受保护」状态（有原片缺失或已变化）：请先点上方「重试恢复」，或重新扫描 / 导入');
  if(editorBusy)commonReasons.push('所选照片正在导出，请等待导出完成');
  const removeBlocked=commonReasons.length>0;
  $('projectRemoveBtn').disabled=removeBlocked;
  $('projectRemoveBtn').title=removeBlocked?'暂不可用 · '+commonReasons.join('；'):'从项目清单移除所选照片；本地原片与 XMP 文件保留，可撤销';
  const trashReasons=[...commonReasons];
  if(PROCESS_BUSY)trashReasons.push('色彩 / 磨皮批处理正在运行');
  if(SCAN_ACTIVE||JOB)trashReasons.push('扫描正在进行');
  if(exportBusy)trashReasons.push('导出队列存在未完成任务（含「已暂停」）：请先在导出队列完成，或点「清除记录」');
  const trashBlocked=trashReasons.length>0;
  $('trashBtn').disabled=trashBlocked;
  $('trashBtn').title=trashBlocked?'暂不可用 · '+trashReasons.join('；'):'把所选照片的本地原片（连同同名 XMP）移到系统废纸篓 / 回收站，之后可从废纸篓恢复';
  $('splitGroups').disabled=!count||busy||SCAN_ACTIVE||!!JOB||PROJECT.blocked;
  $('mergeGroups').disabled=count<2||busy||SCAN_ACTIVE||!!JOB||PROJECT.blocked;
  syncCullingHistoryButtons();
}
function syncCullingHistoryButtons(){
  const busy=CULL_HISTORY.applying||BATCH_BUSY||SCAN_ACTIVE||!!JOB||PROCESS_BUSY||!!CURRENT_EDITOR||PROJECT.blocked||pendingRatings.size>0;
  for(const [id,key] of [['cullUndo','undo'],['cullRedo','redo']]){
    const button=$(id);if(!button)continue;const entry=CULL_HISTORY[key].at(-1);
    button.disabled=busy||!entry;button.title=entry?(key==='undo'?'撤销：':'重做：')+entry.label:'暂无筛片历史';
  }
}
function pushCullingHistory(entry){
  if(CULL_HISTORY.applying||entry.type==='ratings'&&!entry.changes.length)return;
  CULL_HISTORY.undo.push(entry);if(CULL_HISTORY.undo.length>40)CULL_HISTORY.undo.shift();CULL_HISTORY.redo=[];
  syncCullingHistoryButtons();
}
function recordSelection(before,label){
  const changed=[...new Set([...before,...selectedPaths])].filter(path=>before.has(path)!==selectedPaths.has(path));
  if(changed.length)pushCullingHistory({type:'selection',label,changes:changed.map(path=>({path,before:before.has(path),after:selectedPaths.has(path)}))});
}
async function replayCullingHistory(redo=false){
  if(CULL_HISTORY.applying||BATCH_BUSY||SCAN_ACTIVE||JOB||PROCESS_BUSY||CURRENT_EDITOR||PROJECT.blocked||pendingRatings.size)return false;
  const from=redo?CULL_HISTORY.redo:CULL_HISTORY.undo,to=redo?CULL_HISTORY.undo:CULL_HISTORY.redo,entry=from.at(-1);
  if(!entry)return false;
  const generation=libraryGeneration,isCurrent=()=>generation===libraryGeneration;
  CULL_HISTORY.applying=true;syncBatchControls();
  try{
    if(entry.type==='selection'){
      for(const change of entry.changes)if(libraryByPath.has(change.path)){
        if(change[redo?'after':'before'])selectedPaths.add(change.path);else selectedPaths.delete(change.path);
      }
      selectionTouched=true;for(const [path,card] of cardCache)card.classList.toggle('sel',selectedPaths.has(path));
      updateStats();projectChanged();
    }else if(entry.type==='ratings'){
      let success=true;
      for(const change of entry.changes){
        if(!isCurrent())return false;
        const photo=libraryByPath.get(change.path)||library.find(p=>p.path===change.path);
        if(!photo||!await setRating(photo,change[redo?'after':'before'],{recordHistory:false,historyReplay:true,silent:true,deferView:true}))success=false;
        if(!isCurrent())return false;
      }
      if(sortMode==='rating'||filterMode==='starred')rebuildView();
      if(!success){toast('部分星级写入失败，历史已保留，请检查磁盘后重试');return false;}
    }else if(entry.type==='remove'){
      const paths=entry.state.library.map(p=>p.path);
      if(redo){
        const state=detachSelectedPhotos(library.filter(p=>paths.includes(p.path)));
        if(!isCurrent())return false;
        const saved=await saveProject(true);
        if(!isCurrent())return false;
        if(!saved){restoreDetachedPhotos(state,paths);return false;}
      }else{
        restoreDetachedPhotos(entry.state,paths);
        if(!isCurrent())return false;
        const saved=await saveProject(true);
        if(!isCurrent())return false;
        if(!saved){detachSelectedPhotos(library.filter(p=>paths.includes(p.path)));return false;}
      }
    }else if(entry.type==='groups'){
      for(const change of entry.changes){const p=libraryByPath.get(change.path);if(p)Object.assign(p,change[redo?'after':'before']);}
      rebuildView();projectChanged();
    }
    if(!isCurrent())return false;
    from.pop();to.push(entry);toast((redo?'已重做：':'已撤销：')+entry.label);return true;
  }finally{CULL_HISTORY.applying=false;syncBatchControls();}
}
function undoCulling(){return replayCullingHistory(false);}
function redoCulling(){return replayCullingHistory(true);}
function groupMetadata(p){return {group:p.group,group_size:p.group_size,group_rank:p.group_rank,group_best:p.group_best,flags:[...p.flags]};}
const REGROUP={busy:false};
async function regroupVisiblePhotos(){
  if(REGROUP.busy)return false;
  if(SCAN_ACTIVE||JOB||BATCH_BUSY||CULL_HISTORY.applying||PROJECT.blocked)return toast('请等待扫描、批量操作或项目恢复完成后识别相似'),false;
  const matched=library.filter(matchesSizeFilter),oldGroups=new Set(matched.filter(isGroup).map(p=>p.group));
  const scope=library.filter(p=>matchesSizeFilter(p)||isGroup(p)&&oldGroups.has(p.group));
  if(!scope.length)return toast('当前大小范围内没有照片，请先调整大小过滤'),false;
  if(scope.length>20000)return toast('每次最多识别 20000 张，请先用大小过滤缩小范围'),false;
  if(oldGroups.size&&!window.confirm(`重新识别 ${scope.length} 张照片（含已有组成员）？会重建这些照片的分组，可撤销；保留选择和星级，不修改原片。`))return false;
  const generation=libraryGeneration,signatures=new Map(scope.map(p=>[p.path,JSON.stringify(groupMetadata(p))]));
  const current=()=>generation===libraryGeneration&&!SCAN_ACTIVE&&!JOB&&!BATCH_BUSY&&!CULL_HISTORY.applying&&scope.every(p=>libraryByPath.get(p.path)===p&&JSON.stringify(groupMetadata(p))===signatures.get(p.path));
  REGROUP.busy=true;$('regroupButton').disabled=true;$('regroupStatus').textContent=`正在识别相似：0 / ${scope.length}`;
  try{
    const started=await api('/api/regroup',{photos:scope.map(({path,width,height,score})=>({path,width,height,score})),similarity_profile:$('similarityProfile').value||'balanced'});
    let result;
    do{
      if(!current())throw new Error('图库或分组已改变，本次结果未应用');
      result=await api('/api/regroup_status?job='+encodeURIComponent(started.job));
      if(!current())throw new Error('图库或分组已改变，本次结果未应用');
      $('regroupStatus').textContent=`正在识别相似：${result.done||0} / ${result.total||scope.length}`;
      if(!result.finished)await new Promise(resolve=>setTimeout(resolve,400));
    }while(!result.finished);
    if(result.error)throw new Error(result.error);
    if(!Array.isArray(result.photos)||!result.photos.length)throw new Error('没有可读取的预览，原分组已保留');
    const seen=new Set();
    for(const p of result.photos){
      if(!p||!signatures.has(p.path)||seen.has(p.path)||!Number.isInteger(p.group)||p.group<0||!Number.isInteger(p.group_size)||p.group_size<1||!Number.isInteger(p.group_rank)||p.group_rank<1||p.group_rank>p.group_size||typeof p.group_best!=='boolean')throw new Error('相似识别结果无效，原分组已保留');
      seen.add(p.path);
    }
    const before=new Map(library.map(p=>[p.path,groupMetadata(p)])),affected=new Set(scope.filter(p=>p.group>=0).map(p=>p.group));
    const used=new Set(library.filter(p=>!seen.has(p.path)&&p.group>=0).map(p=>p.group));let next=0;
    const ids=new Map();
    for(const p of result.photos){if(!ids.has(p.group)){while(used.has(next))next++;ids.set(p.group,next);used.add(next++);}const photo=libraryByPath.get(p.path);photo.group=ids.get(p.group);affected.add(photo.group);}
    const members=new Map();
    for(const p of library){if(!affected.has(p.group))continue;if(!members.has(p.group))members.set(p.group,[]);members.get(p.group).push(p);}
    for(const group of members.values()){
      group.sort((a,b)=>b.score-a.score||a.path.localeCompare(b.path));
      group.forEach((p,i)=>{p.group_size=group.length;p.group_rank=i+1;p.group_best=i===0;p.flags=p.flags.filter(f=>f!=='连拍重复');if(i>0&&group.length>1)p.flags.push('连拍重复');});
    }
    const changes=library.flatMap(p=>{const prior=before.get(p.path),after=groupMetadata(p);return JSON.stringify(prior)===JSON.stringify(after)?[]:[{path:p.path,before:prior,after}];});
    pushCullingHistory({type:'groups',label:'重新识别相似',changes});rebuildView();projectChanged();
    $('regroupStatus').textContent=`相似识别完成 · ${new Set(result.photos.filter(p=>p.group_size>=2).map(p=>p.group)).size} 组`+(result.skipped?` · ${result.skipped} 张未读取`:'');
    return true;
  }catch(error){$('regroupStatus').textContent='相似识别未应用：'+error.message;return false;}
  finally{REGROUP.busy=false;$('regroupButton').disabled=false;}
}
function editSimilarGroups(kind){
  if(!['split','merge'].includes(kind)||BATCH_BUSY||CULL_HISTORY.applying||SCAN_ACTIVE||JOB||PROJECT.blocked)return false;
  const photos=selectedPhotos();if(photos.length<(kind==='merge'?2:1))return toast(kind==='merge'?'请至少选择两张照片':'请先选择照片'),false;
  if(selectionCounts(photos).hidden&&!window.confirm(`修改全部所选 ${photos.length} 张照片的分组？${selectionScopeNotice(photos)}`))return false;
  const before=new Map(library.map(p=>[p.path,groupMetadata(p)]));
  const affectedGroups=new Set(photos.filter(p=>p.group>=0).map(p=>p.group)),affectedPaths=new Set(photos.map(p=>p.path));
  const used=new Set(library.map(p=>p.group));let candidate=0;
  const nextId=()=>{while(used.has(candidate))candidate++;used.add(candidate);return candidate++;};
  if(kind==='merge'){const group=nextId();for(const p of photos)p.group=group;}
  else for(const p of photos)p.group=nextId();
  for(const p of photos)affectedGroups.add(p.group);
  const groups=new Map();
  for(const p of library){if(p.group>=0?!affectedGroups.has(p.group):!affectedPaths.has(p.path))continue;
    const key=p.group<0?p.path:String(p.group);if(!groups.has(key))groups.set(key,[]);groups.get(key).push(p);}
  for(const members of groups.values()){
    members.sort((a,b)=>b.score-a.score||a.path.localeCompare(b.path));
    members.forEach((p,i)=>{p.group_size=members.length;p.group_rank=i+1;p.group_best=i===0;
      p.flags=p.flags.filter(flag=>flag!=='连拍重复');if(i>0&&members.length>1)p.flags.push('连拍重复');});
  }
  const changes=library.flatMap(p=>{const prior=before.get(p.path),after=groupMetadata(p);return JSON.stringify(prior)===JSON.stringify(after)?[]:[{path:p.path,before:prior,after}];});
  pushCullingHistory({type:'groups',label:kind==='merge'?'手动合组':'手动拆组',changes});
  rebuildView();projectChanged();toast(kind==='merge'?'已合并所选照片；可撤销':'已拆分所选照片；可撤销');return true;
}
function cullingHistoryShortcut(e){
  const target=e.target||document.activeElement;
  if(!(e.metaKey||e.ctrlKey)||e.altKey||e.repeat||e.isComposing||!['z','y'].includes(e.key.toLowerCase())||/^(INPUT|TEXTAREA|SELECT)$/i.test(target?.tagName||'')||target?.isContentEditable)return false;
  if(['results','lb','compareModal'].some(id=>$(id).classList.contains('show')))return false;
  const redo=e.key.toLowerCase()==='y'||e.shiftKey;
  if(!(redo?CULL_HISTORY.redo:CULL_HISTORY.undo).length)return false;
  e.preventDefault();replayCullingHistory(redo);return true;
}
function selectedPhotos(){return library.filter(p=>selectedPaths.has(p.path)&&matchesSizeFilter(p));}
function selectionCounts(photos=selectedPhotos()){
  const visiblePaths=new Set(ranked.map(p=>p.path));
  const visible=photos.reduce((n,p)=>n+(visiblePaths.has(p.path)?1:0),0);
  return {total:photos.length,visible,hidden:photos.length-visible};
}
function selectionScopeNotice(photos){
  const counts=selectionCounts(photos);
  return `当前可见 ${counts.visible} 张，隐藏 ${counts.hidden} 张。`+
    (counts.hidden?'隐藏照片也包含在本次操作中；如不需要，请取消后点击「取消隐藏选择」。':'');
}
function clearHiddenSelection(){
  if(BATCH_BUSY||CULL_HISTORY.applying)return;
  const before=new Set(selectedPaths);
  const visiblePaths=new Set(ranked.map(p=>p.path));
  const count=selectionCounts().hidden;
  selectedPaths=new Set([...selectedPaths].filter(path=>visiblePaths.has(path)));
  selectionTouched=true;
  for(const [path,card] of cardCache)card.classList.toggle('sel',selectedPaths.has(path));
  recordSelection(before,'取消隐藏选择');updateStats();syncBatchControls();projectChanged();toast(`已取消 ${count} 张隐藏照片的选择，当前可见选择保留`);
}
function detachSelectedPhotos(photos){
  const paths=new Set(photos.map(p=>p.path));
  if(CURRENT_EDITOR&&paths.has(CURRENT_EDITOR.path))closeResults();
  const state={library,selected:new Set(selectedPaths),target:targetPath,selectionTouched,
    drafts:new Map([...EDIT_DRAFTS].filter(([path])=>paths.has(path)))};
  library=library.filter(p=>!paths.has(p.path));
  for(const path of paths){libraryByPath.delete(path);cardCache.delete(path);EDIT_DRAFTS.delete(path);selectedPaths.delete(path);}
  if(paths.has(targetPath))targetPath=null;
  selectionTouched=true;ranked=sortPhotos(filterPhotos(library));
  rebuildView();$('empty').style.display=library.length?'none':'';updateStats();projectChanged(0);
  return state;
}
function restoreDetachedPhotos(state,paths){
  const restore=new Set(paths);
  const additions=state.library.filter(p=>restore.has(p.path));
  library=[...library,...additions.filter(p=>!libraryByPath.has(p.path))];
  for(const photo of additions)libraryByPath.set(photo.path,photo);
  for(const [path,draft] of state.drafts)if(restore.has(path))EDIT_DRAFTS.set(path,draft);
  for(const path of state.selected)if(restore.has(path))selectedPaths.add(path);
  if(state.target&&restore.has(state.target))targetPath=state.target;
  selectionTouched=state.selectionTouched;ranked=sortPhotos(filterPhotos(library));
  rebuildView();$('empty').style.display=library.length?'none':'';updateStats();projectChanged(0);
}
async function removeSelectedFromProject(){
  if(BATCH_BUSY||CULL_HISTORY.applying)return 0;
  if(PROJECT.blocked)return toast('项目恢复受保护中，请先完成恢复或完整扫描'),0;
  const photos=selectedPhotos();if(!photos.length)return toast('请先选择照片'),0;
  if(photos.some(p=>pendingRatings.has(p.path)))return toast('正在写入所选照片的 XMP 评分，请稍后重试'),0;
  if(CURRENT_EDITOR?.exporting&&photos.some(p=>p.path===CURRENT_EDITOR.path))return toast('所选照片正在导出，请完成后再移除'),0;
  if(!window.confirm(`从 PhotoPilot 项目中移除全部选中的 ${photos.length} 张照片？${selectionScopeNotice(photos)}本地原片与 XMP 评分文件都会保留。`))return 0;
  BATCH_BUSY=true;const state=detachSelectedPhotos(photos);syncBatchControls();
  try{
    const saved=await saveProject(true);
    if(!saved){restoreDetachedPhotos(state,photos.map(p=>p.path));return 0;}
    pushCullingHistory({type:'remove',label:'从项目移除',state:{...state,library:photos}});
    toast(`已从项目移除 ${photos.length} 张；可撤销，本地原片未删除`);return photos.length;
  }finally{BATCH_BUSY=false;syncBatchControls();}
}
async function deleteSelectedToTrash(){
  if(BATCH_BUSY||CULL_HISTORY.applying)return 0;
  if(PROJECT.blocked)return toast('项目恢复受保护中，请先完成恢复或完整扫描'),0;
  const photos=selectedPhotos();if(!photos.length)return toast('请先选择照片'),0;
  if(CURRENT_EDITOR?.exporting&&photos.some(p=>p.path===CURRENT_EDITOR.path))return toast('所选照片正在导出，请完成后再移到废纸篓/回收站'),0;
  if(PROCESS_BUSY||SCAN_ACTIVE||JOB||[...EXPORT_QUEUE.jobs.values()].some(job=>['queued','running','paused'].includes(job.state)))
    return toast('扫描或导出正在使用照片，请完成后再移到废纸篓/回收站'),0;
  if(photos.some(p=>pendingRatings.has(p.path)))return toast('正在写入所选照片的 XMP 评分，请稍后重试'),0;
  if(!window.confirm(`把全部选中的 ${photos.length} 张本地原片移到系统废纸篓/回收站？${selectionScopeNotice(photos)}同名 XMP sidecar（含星级和可能的其他编辑元数据）也会一起移动；之后可从废纸篓/回收站恢复。`))return 0;
  BATCH_BUSY=true;const state=detachSelectedPhotos(photos);syncBatchControls();
  try{
    if(!await saveProject(true)){
      restoreDetachedPhotos(state,photos.map(p=>p.path));return 0;
    }
    let response;
    try{response=await api('/api/trash',{paths:photos.map(p=>p.path)});}
    catch(error){
      if(error.definiteRejected){restoreDetachedPhotos(state,photos.map(p=>p.path));await saveProject(true);toast('废纸篓/回收站操作被拒绝：'+error.message);return 0;}
      CULL_HISTORY.undo=[];CULL_HISTORY.redo=[];
      toast('废纸篓/回收站操作结果未确认；照片已从项目移除，请检查系统废纸篓/回收站和原目录');return 0;
    }
    const failed=Array.isArray(response.failed)?response.failed:[];
    const removed=new Set(Array.isArray(response.removed)?response.removed:[]);
    if(removed.size){CULL_HISTORY.undo=[];CULL_HISTORY.redo=[];}
    const failedPaths=new Set(failed.map(item=>item&&item.path).filter(path=>photos.some(p=>p.path===path)));
    for(const photo of photos)if(!removed.has(photo.path)&&!failedPaths.has(photo.path))failedPaths.add(photo.path);
    if(failedPaths.size){restoreDetachedPhotos(state,failedPaths);await saveProject(true);}
    if(failed.length)toast(`已移到废纸篓/回收站 ${removed.size} 张；${failed.length} 张失败并留在项目中`);
    else toast(`已将 ${removed.size} 张原片移到系统废纸篓/回收站`);
    const details=[...(response.warnings||[]).map(item=>item.message),
      ...failed.map(item=>`${baseName(item.path)}：${item.error}`)];
    if(details.length)$('status').textContent=details.join('；');
    return removed.size;
  }finally{BATCH_BUSY=false;syncBatchControls();}
}

let compareGroupId=null,compareItems=[],compareActivePath=null,compareSession=0,compareResize=null;
let compareView={zoom:1,x:0,y:0};
function groupItems(group){
  return library.filter(p=>String(p.group)===String(group)).sort((a,b)=>(a.group_rank||99)-(b.group_rank||99)).slice(0,4);
}
function compareDimensions(pane){
  const img=pane.querySelector('img');
  return {width:pane.clientWidth,height:pane.clientHeight,naturalWidth:img?.naturalWidth||0,naturalHeight:img?.naturalHeight||0};
}
function compareImageGeometry(size,zoom,x,y){
  if(!size.width||!size.height||!size.naturalWidth||!size.naturalHeight)return {width:0,height:0,x:0,y:0};
  const fit=Math.min(size.width/size.naturalWidth,size.height/size.naturalHeight);
  const width=size.naturalWidth*fit,height=size.naturalHeight*fit;
  const dx=Math.max(0,(width*zoom-size.width)/2),dy=Math.max(0,(height*zoom-size.height)/2);
  return {width,height,x:Math.max(-dx,Math.min(dx,x*width*zoom)),y:Math.max(-dy,Math.min(dy,y*height*zoom))};
}
function comparePixelZoom(size){
  if(!size.width||!size.height||!size.naturalWidth||!size.naturalHeight)return 1;
  const fit=Math.min(size.width/size.naturalWidth,size.height/size.naturalHeight);
  return 1/(fit*Math.max(1,Number(typeof devicePixelRatio==='undefined'?1:devicePixelRatio)||1));
}
function comparePanes(){return document.querySelectorAll('.comparePane');}
function syncCompareMetadata(){
  for(const pane of comparePanes()){
    const p=compareItems.find(item=>item.path===pane.dataset.path);if(!p)continue;
    pane.classList.toggle('active',p.path===compareActivePath);
    pane.setAttribute('aria-label',`${baseName(p.path)}${p.path===compareActivePath?' · 当前操作照片':''}`);
    const star=pane.querySelector('.compareStar');
    if(star){star.textContent=p.rating?'★'+p.rating:'☆';star.classList.toggle('on',!!p.rating);star.setAttribute('aria-pressed',String(!!p.rating));}
  }
  const p=compareItems.find(item=>item.path===compareActivePath);
  if($('compareActiveName'))$('compareActiveName').textContent=p?'当前：'+baseName(p.path):'';
  if($('compareRating'))$('compareRating').value=p?.rating||0;
  if($('compareKeep'))$('compareKeep').setAttribute('aria-pressed',String(!!p&&selectedPaths.has(p.path)));
  if($('compareReject'))$('compareReject').setAttribute('aria-pressed',String(!!p&&!selectedPaths.has(p.path)));
}
function activateCompare(path){if(!compareItems.some(p=>p.path===path))return;compareActivePath=path;syncCompareMetadata();}
function clearCompareHandlers(){
  for(const pane of comparePanes()){
    pane.onpointermove=null;
    if(pane._comparePointer!==undefined&&pane.hasPointerCapture?.(pane._comparePointer))pane.releasePointerCapture(pane._comparePointer);
    delete pane._comparePointer;
    const img=pane.querySelector('img');if(img){img.onload=null;img.onerror=null;}
  }
  if(compareResize){compareResize.disconnect();compareResize=null;}
}
function renderGroupCompare(items){
  clearCompareHandlers();const session=++compareSession;
  compareItems=items;compareActivePath=items[0]?.path||null;
  const grid=$('compareGrid');grid.innerHTML='';grid.style.setProperty('--compare-cols',items.length);
  $('compareTitle').textContent=`相似照片对比 · ${items.length} 张`;
  for(const p of items){
    const pane=document.createElement('div');pane.className='comparePane';pane.dataset.path=p.path;pane.tabIndex=0;
    pane.innerHTML=`<img alt="${escapeHtml(baseName(p.path))}"><span class="compareImageStatus" role="status">加载预览…</span>
      <div class="compareMeta"><span title="${escapeHtml(baseName(p.path))}">${escapeHtml(baseName(p.path))} · ${Number(p.score||0).toFixed(2)}</span>
      <button class="compareStar" type="button" aria-label="切换五星标记">☆</button></div>`;
    const img=pane.querySelector('img'),status=pane.querySelector('.compareImageStatus');
    img.onload=()=>{if(session!==compareSession)return;status.hidden=true;updateCompareView();};
    img.onerror=()=>{if(session!==compareSession)return;status.textContent='预览失败，请检查照片是否可访问';};
    img.src='/api/raw_img?p='+encodeURIComponent(p.path)+'&s=2200';
    pane.querySelector('.compareStar').onclick=()=>{activateCompare(p.path);compareRate(p.rating?0:5);};
    pane.onfocus=()=>activateCompare(p.path);pane.onclick=()=>activateCompare(p.path);
    bindComparePan(pane);grid.appendChild(pane);
  }
  if(typeof ResizeObserver!=='undefined'){compareResize=new ResizeObserver(()=>updateCompareView());compareResize.observe(grid);}
  syncCompareMetadata();
}
function openGroupCompare(group){
  const items=groupItems(group);if(items.length<2)return toast('这张照片没有可比较的相似组');
  compareGroupId=group;$('compareModal').classList.add('show');renderGroupCompare(items);resetCompareView();
}
function openSelectedCompare(){
  const selected=ranked.filter(p=>selectedPaths.has(p.path));
  if(selected.length<2||selected.length>4)return toast('请先选择 2–4 张照片进行对比');
  compareGroupId=null;$('compareModal').classList.add('show');renderGroupCompare(selected.slice(0,4));resetCompareView();
}
function closeGroupCompare(){
  ++compareSession;clearCompareHandlers();$('compareModal').classList.remove('show');compareGroupId=null;compareItems=[];compareActivePath=null;
  $('compareGrid').innerHTML='';
}
function updateCompareView(){
  if(compareView.pixelPath){
    const pane=Array.from(comparePanes()).find(p=>p.dataset.path===compareView.pixelPath);
    if(pane)compareView.zoom=comparePixelZoom(compareDimensions(pane));
  }
  compareView.zoom=Math.max(.1,Math.min(32,Number(compareView.zoom)||1));
  const limit=Math.max(0,.5-.5/compareView.zoom);
  compareView.x=Math.max(-limit,Math.min(limit,compareView.x));compareView.y=Math.max(-limit,Math.min(limit,compareView.y));
  $('compareZoom').value=compareView.zoom;$('compareZoomValue').textContent=compareView.pixelPath?'100% 预览像素':compareView.zoom.toFixed(1)+'× 适屏';
  for(const pane of comparePanes()){
    const img=pane.querySelector('img'),g=compareImageGeometry(compareDimensions(pane),compareView.zoom,compareView.x,compareView.y);
    if(!img||!g.width)continue;
    img.style.width=g.width+'px';img.style.height=g.height+'px';
    img.style.transform=`translate(${g.x}px,${g.y}px) scale(${compareView.zoom})`;
  }
}
function resetCompareView(){compareView={zoom:1,x:0,y:0};updateCompareView();}
function comparePreviewPixels(){
  const pane=Array.from(comparePanes()).find(p=>p.dataset.path===compareActivePath);if(!pane)return;
  const size=compareDimensions(pane);if(!size.naturalWidth)return toast('预览尚未加载，请稍候');
  compareView={zoom:comparePixelZoom(size),x:0,y:0,pixelPath:compareActivePath};updateCompareView();
}
async function compareRate(rating){
  const p=compareItems.find(item=>item.path===compareActivePath);if(!p)return false;
  const ok=await setRating(p,Number(rating));syncCompareMetadata();return ok;
}
function compareKeep(keep){
  if(CULL_HISTORY.applying)return;
  const before=new Set(selectedPaths);
  const p=compareItems.find(item=>item.path===compareActivePath);if(!p)return;
  selectionTouched=true;if(keep)selectedPaths.add(p.path);else selectedPaths.delete(p.path);
  const card=cardCache.get(p.path);if(card)card.classList.toggle('sel',keep);
  recordSelection(before,'对比保留');updateStats();projectChanged();syncBatchControls();syncCompareMetadata();
  toast(keep?'当前照片已保留':'当前照片已移出保留（未删除原片）');
}
function compareShortcut(e){
  if(!$('compareModal').classList.contains('show'))return false;
  if(e.repeat||e.isComposing||e.metaKey||e.ctrlKey||e.altKey||e.shiftKey)return false;
  const target=e.target||document.activeElement;
  if(/^(INPUT|TEXTAREA|SELECT)$/i.test(target?.tagName||'')||target?.isContentEditable)return false;
  if(e.key==='Escape'){e.preventDefault();closeGroupCompare();return true;}
  if(/^[0-5]$/.test(e.key)){e.preventDefault();compareRate(Number(e.key));return true;}
  if(e.key.toLowerCase()==='p'||e.key.toLowerCase()==='x'){e.preventDefault();compareKeep(e.key.toLowerCase()==='p');return true;}
  if(e.key==='ArrowLeft'||e.key==='ArrowRight'){
    const index=compareItems.findIndex(p=>p.path===compareActivePath),next=index+(e.key==='ArrowLeft'?-1:1);
    if(next>=0&&next<compareItems.length)activateCompare(compareItems[next].path);e.preventDefault();return true;
  }
  return false;
}
function bindComparePan(pane){
  pane.onpointerdown=e=>{
    activateCompare(pane.dataset.path);
    if(e.target.closest('button')||compareView.zoom<=1)return;
    pane.setPointerCapture(e.pointerId);pane._comparePointer=e.pointerId;let x=e.clientX,y=e.clientY;
    pane.onpointermove=event=>{
      const g=compareImageGeometry(compareDimensions(pane),compareView.zoom,0,0);if(!g.width)return;
      compareView.x+=(event.clientX-x)/(g.width*compareView.zoom);compareView.y+=(event.clientY-y)/(g.height*compareView.zoom);
      x=event.clientX;y=event.clientY;updateCompareView();
    };
  };
  pane.onpointerup=pane.onpointercancel=e=>{
    pane.onpointermove=null;
    if(pane.hasPointerCapture?.(e.pointerId))pane.releasePointerCapture(e.pointerId);delete pane._comparePointer;
  };
  pane.onwheel=e=>{e.preventDefault();activateCompare(pane.dataset.path);delete compareView.pixelPath;compareView.zoom*=Math.exp(-e.deltaY*.002);updateCompareView();};
  pane.ondblclick=resetCompareView;
}
$('compareZoom').oninput=e=>{delete compareView.pixelPath;compareView.zoom=+e.target.value;updateCompareView();};
$('compareModal').onclick=e=>{if(e.target===$('compareModal'))closeGroupCompare();};

/* ── 快审灯箱：双击卡片进入，键盘批量审片，星级写 XMP sidecar ── */
let LBI=-1,lbPreviewSeq=0,lbPrefetchTimer=null;
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
function lbHide(){
  const path=ranked[LBI]?.path;
  ++lbPreviewSeq;++lbExifSeq;clearTimeout(lbPrefetchTimer);$('lb').classList.remove('show');
  cardCache.get(path)?.focus?.({preventScroll:true});
}
function lbCard(p){return cardCache.get(p.path);}
let lbExifSeq=0;
function lbRenderExif(ex){
  const box=$('lbExif');
  if(!ex){box.textContent='';return;}
  const parts=[];
  if(ex.camera)parts.push(`<span>${escapeHtml(ex.camera)}</span>`);
  if(ex.lens)parts.push(`<span>${escapeHtml(ex.lens)}</span>`);
  if(ex.aperture)parts.push(`<span>光圈 <b>${ex.aperture}</b></span>`);
  if(ex.exposure)parts.push(`<span>快门 <b>${ex.exposure}</b></span>`);
  if(ex.iso!==undefined)parts.push(`<span>ISO <b>${ex.iso}</b></span>`);
  if(ex.focal)parts.push(`<span>焦段 <b>${ex.focal}</b></span>`);
  if(ex.focal_equiv)parts.push(`<span>${ex.focal_equiv}</span>`);
  if(ex.datetime)parts.push(`<span>${escapeHtml(ex.datetime)}</span>`);
  if(ex.resolution)parts.push(`<span>${ex.resolution}</span>`);
  if(ex.gps){
    const [lat,lon]=ex.gps;
    const maps='https://www.openstreetmap.org/?mlat='+lat+'&mlon='+lon+'#map=15/'+lat+'/'+lon;
    parts.push(`<span class="exgps" title="在地图查看" onclick="window.open('${maps}')">📍 ${lat}, ${lon}</span>`);}
  box.innerHTML=parts.join('<span class="exsep">·</span>');
}
function lbLoadExif(){
  const p=ranked[LBI];if(!p)return;
  const seq=++lbExifSeq;
  $('lbExif').textContent='';
  api('/api/exif?p='+encodeURIComponent(p.path)).then(j=>{
    if(seq!==lbExifSeq)return;         // 已翻页，丢弃过期响应
    lbRenderExif(j.exif);
  }).catch(()=>{if(seq===lbExifSeq)$('lbExif').textContent='';});
}
function lbUpdate(reload){
  const p=ranked[LBI];if(!p)return;
  $('lbName').textContent=baseName(p.path);
  $('lbIdx').textContent=(LBI+1)+' / '+ranked.length;
  lbRenderStars(p.rating||0);
  lbLoadExif();
  $('lbInfo').innerHTML=`<span>综合分 <b>${p.score.toFixed(2)}</b></span>`
    +(p.faces?`<span>人脸 <b>${p.faces}</b></span>`:'')
    +(p.flags.length?p.flags.map(f=>`<span>${escapeHtml(f)}</span>`).join(''):'<span><b>通过</b></span>');
  if(reload){
    const sequence=++lbPreviewSeq;clearTimeout(lbPrefetchTimer);
    const img=$('lbImg');img.alt=baseName(p.path)+' 预览';
    const current=()=>sequence===lbPreviewSeq&&$('lb').classList.contains('show')&&ranked[LBI]?.path===p.path;
    img.classList.remove('ld');$('lbSpin').style.display='block';$('lbSpin').textContent='正在载入清晰预览…';
    img.onload=()=>{if(current()){img.classList.add('ld');$('lbSpin').style.display='none';}};
    img.onerror=()=>{if(current())$('lbSpin').textContent='缩略图暂不可用，正在载入清晰预览…';};
    img.src=p.thumb||'/api/thumb?p='+encodeURIComponent(p.path)+'&s=360';
    const full=new Image();
    full.onload=()=>{if(!current())return;img.src=full.src;img.classList.add('ld');$('lbSpin').style.display='none';};
    full.onerror=()=>{if(current()){$('lbSpin').textContent='清晰预览暂不可用；可继续查看缩略图';$('lbSpin').style.display='block';}};
    full.src='/api/raw_img?p='+encodeURIComponent(p.path);
    lbPrefetchTimer=setTimeout(()=>{
      if(!current())return;const nb=ranked[(LBI+1)%ranked.length];
      if(nb&&nb.path!==p.path){const pre=new Image();pre.src='/api/raw_img?p='+encodeURIComponent(nb.path);}
    },350);
  }
  document.querySelectorAll('.card.lbcur').forEach(c=>c.classList.remove('lbcur'));
  const card=lbCard(p);if(card){card.classList.add('lbcur');
    if(card.scrollIntoView)card.scrollIntoView({block:'nearest'});}}
function lbNav(d){
  if(!ranked.length)return;
  LBI=(LBI+d+ranked.length)%ranked.length;lbUpdate(true);}
async function revealPath(path){
  if(!path)return;
  try{await api('/api/reveal',{path});}
  catch(e){toast('无法定位文件：'+e.message);}
}
function revealCurrent(){
  const p=ranked[LBI];if(!p)return;
  revealPath(p.path);
}
async function lbRate(r){
  const p=ranked[LBI];if(!p)return false;
  return setRating(p,r);
}

function quickLookSpaceEvent(e){
  if(e.key!==' '||e.repeat||e.isComposing||e.metaKey||e.ctrlKey||e.altKey||e.shiftKey)return false;
  const target=e.target||document.activeElement;
  if(/^(INPUT|TEXTAREA|SELECT|BUTTON|SUMMARY|A)$/i.test(target?.tagName||'')||target?.isContentEditable||target?.closest?.('button,a,input,textarea,select,summary,[contenteditable="true"]'))return false;
  return true;
}
function gridQuickLookShortcut(e){
  if(!quickLookSpaceEvent(e))return false;
  if(['results','lb','compareModal'].some(id=>$(id).classList.contains('show'))||CURRENT_EDITOR||BATCH_BUSY)return false;
  let index=ranked.findIndex(p=>p.path===activePhotoPath);
  if(index<0)index=ranked.findIndex(p=>selectedPaths.has(p.path));
  if(index<0)return false;
  e.preventDefault();lbShow(index);return true;
}
function gridRatingShortcut(e){
  if(e.repeat||e.isComposing||e.metaKey||e.ctrlKey||e.altKey||e.shiftKey||! /^[0-5]$/.test(e.key))return false;
  const target=e.target||document.activeElement;
  if(/^(INPUT|TEXTAREA|SELECT)$/i.test(target?.tagName||'')||target?.isContentEditable)return false;
  if(['results','lb','compareModal'].some(id=>$(id).classList.contains('show'))||CURRENT_EDITOR||BATCH_BUSY)return false;
  e.preventDefault();applyBatchRating(Number(e.key));return true;
}
document.addEventListener('keydown',e=>{
  const tag=(document.activeElement&&document.activeElement.tagName)||'';
  if($('compareModal').classList.contains('show')){
    compareShortcut(e);
    return;
  }
  if($('lb').classList.contains('show')){
    if(e.key==='Escape')lbHide();
    else if(e.key==='ArrowRight'||(e.key.toLowerCase()==='j'&&!/input|select/i.test(tag)))lbNav(1);
    else if(e.key==='ArrowLeft'||(e.key.toLowerCase()==='k'&&!/input|select/i.test(tag)))lbNav(-1);
    else if(quickLookSpaceEvent(e)){e.preventDefault();lbHide();}
    else if(/^[0-5]$/.test(e.key)&&!/input|select/i.test(tag))lbRate(+e.key);
    return;}
  if($('results').classList.contains('show')&&e.key==='Escape'){
    closeResults();return;
  }
  if(editorShortcut(e))return;
  if(cullingHistoryShortcut(e))return;
  if(gridQuickLookShortcut(e))return;
  if(gridRatingShortcut(e))return;
  if(e.key==='Escape'&&!$('side').hidden){setSidebarOpen(false,true);return;}
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
const PRESET_INITIAL_INTENT=PROJECT.intent;
const PRESETS_READY=api('/api/presets').then(j=>{
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
  s.onchange=()=>{applyPresetTuning(s.value);projectChanged();};
  if(PROJECT.intent===PRESET_INITIAL_INTENT)applyPresetTuning(s.value);
}).catch(()=>{});
updateTargetUI();
buildToneControls($('toneControls'),'tone-');
const rangeLabels={strength:'vStrength',skin:'vSkin',retain:'vRetain',clarity:'vClarity',wb:'vWb',faceRepair:'vFaceRepair',blemish:'vBlemish',wbStrength:'vWbStr'};
for(const [id,label] of Object.entries(rangeLabels)){$(id).oninput=()=>slide($(id),label);slide($(id),label);}
const INIT_FOLDER=__INIT_FOLDER_JSON__;
if(INIT_FOLDER){restoreProject();projectStatus('已指定照片目录，正在扫描');$('folder').value=INIT_FOLDER;scan();}
else restoreProject();
refreshExportQueue();
</script></body></html>"""


INIT_FOLDER: str | None = None   # serve(folder) 注入，页面加载即自动扫描


def _acquire_app_instance_lock(lock_path: Path | None = None):
    """桌面模式单实例锁；关闭窗口/进程退出后由 OS 自动释放。"""
    path = Path(lock_path) if lock_path else cache_dir() / "app.lock"
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


def _reveal_in_file_manager(path: str) -> None:
    """在系统文件管理器里定位并高亮指定文件/文件夹。

    macOS 用 `open -R`（Finder 中显示）；Windows 用 explorer /select；
    Linux 用 xdg-open（无高亮，仅打开所在目录）。失败抛 OSError。
    """
    import subprocess
    import shutil
    target = str(Path(path).expanduser())
    if os.name == "nt":
        subprocess.run(["explorer", "/select,", target], check=True)
    elif sys.platform == "darwin":
        subprocess.run(["open", "-R", target], check=True)
    else:
        d = shutil.which("xdg-open")
        if not d:
            raise OSError("当前系统无可用文件管理器")
        subprocess.run([d, str(Path(target).parent)], check=True)


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


def _locate_chrome() -> str | None:
    """返回本机 Chrome/Chromium/Edge 可执行文件路径，用于 --app 窗口回退。

    按平台探测常见安装位置；找不到返回 None。
    """
    import shutil
    if os.name == "nt":
        candidates = [
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
            / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
            / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Google" / "Chrome" / "Application" / "chrome.exe",
            # Edge（Windows 必备，几乎总能命中）
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
            / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        ]
    else:
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    # 兜底：在 PATH 里找 chrome/chromium/msedge
    for name in ("google-chrome", "chromium", "chromium-browser", "msedge", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _dock_badge_loop():
    """桌面模式：扫描/导出进度同步到 Dock 图标徽标；空闲时清除。

    macOS 专用能力（platform.dock_badge 内部静默降级），每 2s 轮询一次，
    任何异常只影响徽标、绝不影响业务。
    """
    from .platform import dock_badge
    while True:
        time.sleep(2)
        try:
            label = None
            # 1) 最近一个未结束的扫描任务：显示 done/total
            for job in reversed(list(SCAN_JOBS.values())):
                if not job.get("finished") and job.get("total"):
                    label = f"{job.get('done', 0)}/{job['total']}"
                    break
            # 2) 无扫描时看导出队列剩余张数
            if label is None:
                remaining = 0
                for pub in _get_export_queue().list(summaries=True):
                    if pub["state"] in {"queued", "running"}:
                        counts = pub.get("counts", {})
                        remaining += counts.get("pending", 0) + counts.get("running", 0)
                if remaining:
                    label = str(remaining)
            dock_badge(label)
        except Exception:
            pass


def _window_geometry_path() -> Path:
    return cache_dir() / "window_geometry.json"


def _load_window_geometry() -> tuple[int, int]:
    """读取上次保存的窗口尺寸（w, h）；无/损坏时返回默认 1440×920。"""
    try:
        data = json.loads(_window_geometry_path().read_text(encoding="utf-8"))
        w = int(data.get("width", 0))
        h = int(data.get("height", 0))
        if 600 <= w <= 10000 and 500 <= h <= 10000:
            return w, h
    except Exception:
        pass
    return 1440, 920


def _save_window_geometry(width: int, height: int) -> None:
    try:
        w, h = int(width), int(height)
        if not (600 <= w <= 10000 and 500 <= h <= 10000):
            return
        _window_geometry_path().parent.mkdir(parents=True, exist_ok=True)
        _window_geometry_path().write_text(
            json.dumps({"width": w, "height": h}), encoding="utf-8")
    except Exception:
        pass


def run_app(folder: str | None = None, port: int = 0):
    """桌面软件模式：pywebview 原生窗口（macOS WKWebView / Windows WebView2），不开浏览器标签页。

    pywebview 不可用时回退 Chrome/Edge --app 窗口，再回退默认浏览器。
    """
    import traceback

    try:
        instance_lock = _acquire_app_instance_lock()
    except OSError as e:
        print(f"无法创建 PhotoPilot 单实例锁：{e}")
        return
    if instance_lock is None:
        print("PhotoPilot 已在运行，本次启动已忽略；请切换到已打开的窗口。")
        # .app 双击启动没有控制台，print 用户看不到；弹系统通知并激活已有窗口。
        from .platform import activate_running_instance
        activate_running_instance()
        return
    # Keep the file handle alive so the OS lock lasts for the whole app session.

    # LaunchServices does not expose a console, so preserve the real startup
    # exception for diagnosis instead of silently losing it to /dev/null.
    launch_log = _log_dir() / "PhotoPilot.log"

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
    threading.Thread(target=_dock_badge_loop, daemon=True,
                     name="PhotoPilotDockBadge").start()
    log_launch(f"PhotoPilot startup: engine={url}")
    print(f"PhotoPilot 引擎运行中：{url}")

    try:
        import webview

        class NativeDialogs:
            """暴露给页面的原生选择器：window.pywebview.api.* 不存在时页面自动回退。"""

            def pick_folder(self):
                result = webview.windows[0].create_file_dialog(
                    webview.FileDialog.FOLDER)
                return str(result[0]) if result else None

            def pick_photos(self):
                result = webview.windows[0].create_file_dialog(
                    webview.FileDialog.OPEN, allow_multiple=True,
                    file_types=("图片文件 (*.jpg;*.jpeg;*.png;*.webp;*.tif;*.tiff;*.bmp;"
                                "*.arw;*.cr2;*.cr3;*.nef;*.nrw;*.dng;*.raf;*.orf;*.rw2;*.pef;*.srw;*.x3f)",))
                return list(result) if result else None

            def save_geometry(self, width, height):
                # 前端窗口 resize 时回调，持久化尺寸供下次启动恢复。
                _save_window_geometry(width, height)

        win_w, win_h = _load_window_geometry()
        webview.create_window("PhotoPilot", url, width=win_w, height=win_h,
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
    chrome = _locate_chrome()
    if chrome is not None:
        try:
            profile = cache_dir() / "chrome"
            profile.mkdir(parents=True, exist_ok=True)
            child = subprocess.Popen(
                [chrome, f"--app={url}", "--window-size=1440,920",
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
