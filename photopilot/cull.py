"""筛选引擎：多维打分 + 连拍分组。

参考 Facet 的「多维度评分」思路，用轻量传统视觉实现，全部本地计算：

- 清晰度：全图 Laplacian 方差 + 6x6 分块取 P75（避免"背景糊但主体实"被低估）
- 曝光：均值偏离理想中灰 + 高光/阴影裁剪占比
- 对比度：RMS 对比度
- 色彩丰富度：Hasler & Süsstrunk (2003) 指标
- 人脸质量：Haar 检测 + 人脸区域清晰度（无人脸照片取中性值，不惩罚风光）
- 连拍去重：dHash 感知哈希，序列上相邻且 Hamming 距离小的归为一组，
  组内仅最高分保留，其余打 "burst-dup" 标记
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path

import cv2
import numpy as np

from .io import iter_images, load_image, resize_max
from .face_analysis import analyze_faces, FaceInfo

WORK_SIZE = 1024   # 打分用的最长边
IDEAL_LUMA = 0.47  # 理想平均亮度（略低于中灰，保留高光层次）

# 并行扫描：少于 PARALLEL_MIN 张时进程池启动开销不划算，走串行
PARALLEL_MIN = 8
DEFAULT_JOBS = 4

# 综合分权重（和为 1）
W_SHARP, W_EXPOSURE, W_CONTRAST, W_COLOR, W_FACE = 0.35, 0.25, 0.12, 0.08, 0.20

# flags（中文，直接呈现给用户）
F_BLURRY, F_EXPOSURE, F_LOW, F_DUP = "可能模糊", "曝光异常", "低分", "连拍重复"
F_BLINK, F_BLINK_ONE = "闭眼", "疑似眨眼"


@dataclass
class PhotoScore:
    path: str
    width: int
    height: int
    sharpness: float      # 0-1
    exposure: float       # 0-1
    contrast: float       # 0-1
    colorfulness: float   # 0-1
    face_quality: float   # 0-1（无人脸 = 0.5 中性）
    score: float          # 0-1 综合分
    faces: int            # 检出人脸数
    blinks: int           # 双眼闭合的人脸数
    group: int = -1       # 连拍分组 id
    ai: float | None = None   # NIMA 美学分 1-10（未启用时 None）
    flags: list[str] = field(default_factory=list)
    group_size: int = 1       # 近似/连拍组成员数（单张为 1）
    group_rank: int = 1       # 组内按综合分排序的名次（1 为最佳）
    group_best: bool = False  # 是否为该组当前最佳帧

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CullReport:
    photos: list[PhotoScore]

    def ranked(self) -> list[PhotoScore]:
        return smart_rank(self.photos)

    def keepers(self, top: int | None = None, min_score: float | None = None,
                drop_flags: tuple[str, ...] = (F_DUP, F_LOW)) -> list[PhotoScore]:
        pool = self.ranked()
        pool = [p for p in pool if not (set(p.flags) & set(drop_flags))]
        if min_score is not None:
            pool = [p for p in pool if p.score >= min_score]
        if top is not None:
            pool = pool[:top]
        return pool

    def to_json(self) -> str:
        return json.dumps({"photos": [p.to_dict() for p in self.ranked()]},
                          ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 指标实现

def _lap_var(gray: np.ndarray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _sharpness(gray: np.ndarray) -> float:
    """全局方差与分块 P75 加权；输出 0-1。"""
    g = gray.astype(np.float64)
    v_global = _lap_var(g)
    h, w = g.shape
    th, tw = h // 6, w // 6
    tiles = [g[r * th:(r + 1) * th, c * tw:(c + 1) * tw]
             for r in range(6) for c in range(6)
             if th > 4 and tw > 4]
    v_tile = float(np.percentile([_lap_var(t) for t in tiles], 75)) if tiles else v_global
    v = 0.6 * v_global + 0.4 * v_tile
    score = 1.0 - float(np.exp(-v / 1200.0))
    if v_tile < 25:      # 最实的区域都糊 → 直接判软
        score = min(score, 0.25)
    return score


def _exposure(gray01: np.ndarray) -> float:
    mean = float(gray01.mean())
    clip_hi = float((gray01 > 0.985).mean())
    clip_lo = float((gray01 < 0.02).mean())
    penalty = abs(mean - IDEAL_LUMA) * 2.5 + clip_hi * 2.0 + clip_lo * 2.0
    return float(np.clip(1.0 - penalty, 0.0, 1.0))


def _contrast(gray01: np.ndarray) -> float:
    return float(np.clip(gray01.std() / 0.22, 0.0, 1.0))


def _colorfulness(rgb01: np.ndarray) -> float:
    """Hasler & Süsstrunk 色彩丰富度，归一到 0-1。"""
    rg = rgb01[..., 0] - rgb01[..., 1]
    yb = 0.5 * (rgb01[..., 0] + rgb01[..., 1]) - rgb01[..., 2]
    m = np.sqrt(rg.std() ** 2 + yb.std() ** 2) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2)
    return float(np.clip(m / 0.42, 0.0, 1.0))


_cascades_tls = None


def _cascades():
    """Haar 级联每线程独立实例——CascadeClassifier.detectMultiScale 非线程安全，
    多线程共用同一实例会在内部 scaleData 上断言崩溃（OpenCV 4.10 实测）。"""
    global _cascades_tls
    if _cascades_tls is None:
        import threading
        _cascades_tls = threading.local()
    if not hasattr(_cascades_tls, "list"):
        try:
            base = cv2.data.haarcascades
            cs = []
            for name in ("haarcascade_frontalface_default.xml", "haarcascade_profileface.xml"):
                c = cv2.CascadeClassifier(base + name)
                if not c.empty():
                    cs.append(c)
            _cascades_tls.list = cs
        except Exception:
            _cascades_tls.list = []
    return _cascades_tls.list


def detect_faces(gray: np.ndarray) -> list[tuple[int, int, int, int]]:
    faces: list[tuple[int, int, int, int]] = []
    for c in _cascades():
        if len(faces) >= 12:
            break
        found = c.detectMultiScale(gray, scaleFactor=1.12, minNeighbors=5,
                                   minSize=(int(gray.shape[1] * 0.04),) * 2)
        for (x, y, w, h) in found:
            if all(abs(x - fx) > w // 2 or abs(y - fy) > h // 2
                   for (fx, fy, fw, fh) in faces):
                faces.append((int(x), int(y), int(w), int(h)))
    return faces[:12]


def _face_quality(gray: np.ndarray, faces) -> float:
    if not faces:
        return 0.5  # 中性：不惩罚无人脸照片
    quals = []
    for (x, y, w, h) in faces:
        crop = gray[max(0, y):y + h, max(0, x):x + w]
        if crop.size == 0:
            continue
        quals.append(np.clip(_lap_var(crop) / 600.0, 0.0, 1.0))
    return float(np.clip(np.mean(quals), 0.0, 1.0)) if quals else 0.5


def dhash(rgb: np.ndarray, size: int = 8) -> np.ndarray:
    """dHash 感知哈希。先轻度高斯模糊再降采样，避免 2-3px 手抖位移翻转比特。"""
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    gray = cv2.GaussianBlur(gray, (0, 0), 1.5)
    small = cv2.resize(gray, (size + 1, size), interpolation=cv2.INTER_AREA)
    return (small[:, 1:] > small[:, :-1]).flatten()


def score_photo(img_work: np.ndarray, path: str | Path,
                faces: bool = True, ai: bool = False) -> PhotoScore:
    """对一张工作分辨率（≤WORK_SIZE）的 RGB 图打分。

    faces=False 跳过人脸检测（大图库快速扫描用，速度约 5 倍，
    人脸质量维取中性值、无闭眼标记）。
    """
    gray = cv2.cvtColor(img_work, cv2.COLOR_RGB2GRAY)
    gray01 = gray.astype(np.float32) / 255.0
    rgb01 = img_work.astype(np.float32) / 255.0

    sharp = _sharpness(gray)
    expo = _exposure(gray01)
    con = _contrast(gray01)
    col = _colorfulness(rgb01)

    if faces:
        faces_info: list[FaceInfo] = analyze_faces(img_work)
        boxes = [f.box for f in faces_info]
        blinks = sum(1 for f in faces_info if f.blink)
        blink_one = sum(1 for f in faces_info if f.blink_one)
    else:
        faces_info, boxes, blinks, blink_one = [], [], 0, 0
    fq = _face_quality(gray, boxes)
    score = (W_SHARP * sharp + W_EXPOSURE * expo + W_CONTRAST * con
             + W_COLOR * col + W_FACE * fq)

    flags = []
    if sharp < 0.30:
        flags.append(F_BLURRY)
    if expo < 0.35:
        flags.append(F_EXPOSURE)
    if score < 0.35:
        flags.append(F_LOW)

    # 眨眼是硬伤：闭眼直接重罚（商业筛选软件的头号规则）
    if blinks:
        score *= 0.75
        flags.append(F_BLINK)
    elif blink_one:
        score *= 0.90
        flags.append(F_BLINK_ONE)

    nima = None
    if ai:
        from .aesthetic import nima_score, blend_with_traditional
        nima = nima_score(img_work)
        if nima is not None:
            score = blend_with_traditional(score, nima)

    return PhotoScore(path=str(path), width=img_work.shape[1], height=img_work.shape[0],
                      sharpness=round(sharp, 4), exposure=round(expo, 4),
                      contrast=round(con, 4), colorfulness=round(col, 4),
                      face_quality=round(fq, 4), score=round(float(score), 4),
                      faces=len(boxes), blinks=blinks, ai=(round(nima, 3) if nima else None),
                      flags=flags)


def _group_bursts(photos: list[PhotoScore], hashes: dict[str, np.ndarray],
                  max_hamming: int = 8) -> None:
    """按文件名顺序做一维聚类：相邻两张 dHash 距离小 → 同一连拍组。"""
    ordered = sorted(photos, key=lambda p: p.path)
    gid = 0
    prev_photo: PhotoScore | None = None
    for p in ordered:
        h = hashes.get(p.path)
        if h is None:
            p.group = -1
            prev_photo = None
            continue
        if (prev_photo is not None
                and np.count_nonzero(h != hashes[prev_photo.path]) <= max_hamming):
            p.group = prev_photo.group  # 加入上一张所在组
        else:
            p.group = gid               # 开启新组
            gid += 1
        prev_photo = p

    # 组内（≥2 张）只留最高分，其余标连拍重复
    by_group = defaultdict(list)
    for p in ordered:
        if p.group >= 0:
            by_group[p.group].append(p)
    for members in by_group.values():
        members.sort(key=lambda x: (-x.score, x.path))
        size = len(members)
        for rank, p in enumerate(members, 1):
            p.group_size = size
            p.group_rank = rank
            p.group_best = rank == 1
        if size >= 2:
            for p in members[1:]:
                if F_DUP not in p.flags:
                    p.flags.append(F_DUP)


def smart_rank(photos: list[PhotoScore]) -> list[PhotoScore]:
    """按组聚拢并在组内把最佳帧放在最前面。

    组与组之间按组内最佳综合分排序；未形成重复组的照片视为单成员组。
    这样既保留“先看高分”的习惯，也不会把连拍成员拆散到网格各处。
    """
    groups: dict[int, list[PhotoScore]] = defaultdict(list)
    singles: list[PhotoScore] = []
    for p in photos:
        if p.group >= 0 and p.group_size >= 2:
            groups[p.group].append(p)
        else:
            singles.append(p)
    blocks = [sorted(members, key=lambda x: (-x.score, x.path))
              for members in groups.values()]
    blocks.extend([[p] for p in singles])
    blocks.sort(key=lambda block: (-block[0].score, block[0].path))
    return [p for block in blocks for p in block]


def _score_file(path: str, faces: bool = True, ai: bool = False):
    """加载单张并打分（模块级函数：worker 可直接调用）。"""
    img = load_image(path, max_size=WORK_SIZE)
    return score_photo(img, path, faces=faces, ai=ai), dhash(img)


def _scan_sequential(paths: list[str], faces: bool = True, quiet: bool = True,
                     ai: bool = False):
    results = []
    for i, p in enumerate(paths, 1):
        try:
            results.append(_score_file(p, faces, ai=ai))
        except Exception as e:
            print(f"[cull] 跳过无法读取的文件 {p}: {e}")
        if not quiet and i % 12 == 0:
            print(f"[cull] {i}/{len(paths)}")
    return results


_POOL = None          # 常驻「重」池：线程本地 FaceMesh/ONNX 会话跨任务复用
_POOL_JOBS = 0
_FAST_POOL = None     # 常驻「轻」池：极速段专用（纯解码/dHash/缩略图，无模型），永不排队等模型


def _get_pool(jobs: int):
    """常驻重池只创建一次、永不销毁——销毁携带 mediapipe 线程本地模型的
    线程会触发 TFLite 死锁/崩溃（实测 recursive_mutex abort）。
    jobs 只是初始建议值，后续调用复用现有池。"""
    global _POOL, _POOL_JOBS
    if _POOL is None:
        from concurrent.futures import ThreadPoolExecutor
        _POOL = ThreadPoolExecutor(max_workers=max(2, jobs))
        _POOL_JOBS = max(2, jobs)
    return _POOL


def _get_fast_pool(jobs: int):
    global _FAST_POOL
    if _FAST_POOL is None:
        from concurrent.futures import ThreadPoolExecutor
        _FAST_POOL = ThreadPoolExecutor(max_workers=jobs)
    return _FAST_POOL


def _scan_parallel(paths: list[str], jobs: int, faces: bool = True, ai: bool = False,
                   cancel=None):
    """常驻线程池并行（cv2/mediapipe/numpy/onnxruntime 推理时释放 GIL）。

    FaceMesh / NIMA ONNX 会话均为线程本地实例且随池常驻——首次扫描完成
    引擎初始化（约 10-20s），之后的扫描任务直接复用、秒出结果。
    """
    ex = _get_pool(jobs)
    results = []
    futs = [ex.submit(_score_file, p, faces, ai) for p in paths]   # 与 paths 同序
    for i, (path, fut) in enumerate(zip(paths, futs), 1):
        if cancel is not None and cancel():
            break
        try:
            results.append(fut.result())
        except Exception as e:
            print(f"[cull] 跳过无法读取的文件 {path}: {e}")
        if i % 24 == 0:
            print(f"[cull] {i}/{len(paths)}")
    return results


def finalize_cull(photos: list[PhotoScore], hashes: dict[str, np.ndarray]) -> None:
    """全量到齐后的收尾：批内相对模糊标记 + 连拍分组（原地修改）。"""
    # 模糊标记改为批内相对判定：同一批里显著低于中位数的才算"糊"，
    # 避免低纹理题材（雾景/极简/纯色背景）被绝对阈值全军覆没
    if photos:
        med = float(np.median([p.sharpness for p in photos]))
        thresh = max(0.12, 0.45 * med)
        for p in photos:
            if p.sharpness < thresh and F_BLURRY not in p.flags:
                p.flags.append(F_BLURRY)
            elif p.sharpness >= thresh and F_BLURRY in p.flags:
                p.flags.remove(F_BLURRY)
    _group_bursts(photos, hashes)


def cull_folder(folder: str | Path, recursive: bool = False,
                jobs: int | None = None, faces: bool = True,
                ai: bool = False) -> CullReport:
    """对目录内全部图片打分并做连拍分组。

    jobs>1 且图片足够多时用线程池并行（每线程独立 FaceMesh/Haar 实例）；
    出错自动回退串行。faces=False 跳过人脸分析（大图库快速档）。
    """
    paths = [str(p) for p in iter_images(folder, recursive=recursive)]
    if jobs is None:
        jobs = min(DEFAULT_JOBS, os.cpu_count() or 1)
    results = None
    if jobs > 1 and len(paths) >= PARALLEL_MIN:
        try:
            results = _scan_parallel(paths, jobs, faces, ai,
                                     cancel=lambda: getattr(cull_folder, "_cancel", False))
        except Exception as e:
            print(f"[cull] 并行扫描不可用（{e}），回退串行")
            results = None
    if results is None:
        results = _scan_sequential(paths, faces, ai=ai)

    photos = [ps for ps, _ in results]
    hashes = {ps.path: h for ps, h in results}
    finalize_cull(photos, hashes)
    return CullReport(photos=photos)
