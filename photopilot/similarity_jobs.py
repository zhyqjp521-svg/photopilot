"""Bounded, read-only regrouping of an existing library using cached previews."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import uuid

import cv2
import numpy as np

from .cull import PhotoScore, SIMILARITY_PROFILES, _group_bursts, dhash
from .project import _photo

MAX_REGROUP_PHOTOS = 20000
MAX_REGROUP_BYTES = 8 * 1024 * 1024


class SimilarityJobs:
    def __init__(self, preview):
        self.preview = preview
        self.jobs = {}
        self.lock = threading.Lock()

    def start(self, photos, profile='balanced'):
        if not isinstance(profile, str) or profile not in SIMILARITY_PROFILES:
            raise ValueError('相似程度无效')
        if not isinstance(photos, list) or not 1 <= len(photos) <= MAX_REGROUP_PHOTOS:
            raise ValueError('请选择 1–20000 张照片，或用大小过滤缩小范围')
        validated = [_photo(raw) for raw in photos]
        if len({p['path'] for p in validated}) != len(validated):
            raise ValueError('分组请求包含重复路径')
        records = [PhotoScore(p['path'], p['width'], p['height'], 0, 0, 0, 0, .5,
                              p['score'], 0, 0) for p in validated]
        with self.lock:
            if any(not job['finished'] for job in self.jobs.values()):
                raise ValueError('已有相似识别正在进行，请等待完成')
            while len(self.jobs) >= 8:
                self.jobs.pop(next(iter(self.jobs)))
            jid = uuid.uuid4().hex[:12]
            self.jobs[jid] = {'job': jid, 'total': len(records), 'done': 0,
                              'finished': False, 'skipped': 0, 'photos': []}
        try:
            threading.Thread(target=self._run, args=(jid, records, profile),
                             name='PhotoPilotSimilarity', daemon=True).start()
        except Exception:
            with self.lock:
                self.jobs.pop(jid, None)
            raise
        return {'job': jid, 'total': len(records)}

    def status(self, jid):
        with self.lock:
            if jid not in self.jobs:
                raise ValueError('相似识别任务不存在，请重新识别')
            job = self.jobs[jid]
            return {key: value for key, value in job.items()
                    if key != 'photos' or job['finished']}

    def _run(self, jid, records, profile):
        hashes = {}
        job = self.jobs[jid]
        def read(photo):
            data = self.preview(photo.path, 360)
            image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError('预览不可读')
            return dhash(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
        try:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix='PhotoPilotSimilarityRead') as pool:
                pending = {pool.submit(read, photo): photo for photo in records}
                for future in as_completed(pending):
                    photo = pending[future]
                    try:
                        hashes[photo.path] = future.result()
                    except (OSError, ValueError, cv2.error):
                        with self.lock:
                            job['skipped'] += 1
                    with self.lock:
                        job['done'] += 1
            threshold, aspect = SIMILARITY_PROFILES[profile]
            _group_bursts(records, hashes, max_hamming=threshold, aspect_tolerance=aspect)
            result = [{key: getattr(photo, key) for key in
                       ('path', 'group', 'group_size', 'group_rank', 'group_best')}
                      for photo in records if photo.path in hashes]
            with self.lock:
                job['photos'] = result
        except Exception as exc:
            with self.lock:
                job['error'] = str(exc)
        finally:
            with self.lock:
                job['finished'] = True
