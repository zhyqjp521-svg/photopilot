"""Validated, atomic project checkpoints stored separately from photo files."""
from __future__ import annotations

import json
import base64
import binascii
import io
import math
import os
import tempfile
import threading
import time
import urllib.parse
from functools import lru_cache
from pathlib import Path

from PIL import Image

from .xmp import read_rating

MAX_PROJECT_BYTES = 64 * 1024 * 1024
MAX_PHOTOS = 100000
MAX_DEPTH = 16
_PROTOTYPE_KEYS = {'__proto__', 'prototype', 'constructor'}
_MAX_BRUSH_MASK_CHARS = 8 * 1024 * 1024


def _large_brush_mask(value: str) -> None:
    """Validate a canvas PNG without decoding/allocating its pixel raster."""
    prefix = 'data:image/png;base64,'
    if len(value) >= _MAX_BRUSH_MASK_CHARS or not value.startswith(prefix):
        raise ValueError('project brush mask exceeds limit or is not a PNG data URL')
    try:
        data = base64.b64decode(value[len(prefix):], validate=True)
        if not data.startswith(b'\x89PNG\r\n\x1a\n'):
            raise ValueError('not a PNG')
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width > 8192 or height > 8192 or width * height > 16777216:
                raise ValueError('PNG dimensions exceed limit')
            image.verify()
    except (ValueError, OSError, SyntaxError, binascii.Error, Image.DecompressionBombError) as exc:
        raise ValueError('project brush mask is invalid') from exc


def default_project_path() -> Path:
    override = os.environ.get('PHOTOPILOT_PROJECT_PATH')
    if override:
        return Path(override).expanduser()
    from .platform import app_data_dir
    return app_data_dir() / 'project.json'


def _bounded_json(value) -> None:
    """Reject unsafe or unbounded JSON before allocating serialized data."""
    budget = MAX_PROJECT_BYTES
    nodes = 0

    # Only actual v2 photo drafts may carry a large encoded canvas. Settings,
    # arbitrary nested brush_mask keys, legacy drafts and object keys keep the
    # ordinary string limit. Do not loosen the entire checkpoint schema.
    photos = value.get('photos', []) if isinstance(value, dict) else []
    photo_paths = {photo['path'] for photo in photos
                   if isinstance(photo, dict) and isinstance(photo.get('path'), str)} if isinstance(photos, list) else set()
    drafts = value.get('drafts', {}) if isinstance(value, dict) else {}

    def brush_slot(path):
        if (len(path) not in (4, 5) or path[0] != 'drafts' or path[1] not in photo_paths
                or not isinstance(drafts, dict)):
            return False
        draft = drafts.get(path[1])
        return (isinstance(draft, dict) and type(draft.get('version')) is int
                and draft['version'] == 2 and path[-1] == 'brush_mask'
                and ((len(path) == 4 and path[2] == 'retouch')
                     or (len(path) == 5 and path[2] == 'retouchHistory' and type(path[3]) is int)))

    def visit(item, depth, path=()):
        nonlocal budget, nodes
        nodes += 1
        if depth > MAX_DEPTH or nodes > 1000000:
            raise ValueError('project JSON nesting or item count exceeds limit')
        if isinstance(item, dict):
            budget -= 2 + len(item)
            for key, child in item.items():
                if not isinstance(key, str) or key in _PROTOTYPE_KEYS:
                    raise ValueError('project JSON contains an unsafe object key')
                visit(key, depth + 1)
                visit(child, depth + 1, path + (key,))
        elif isinstance(item, list):
            budget -= 2 + len(item)
            for index, child in enumerate(item):
                visit(child, depth + 1, path + (index,))
        elif isinstance(item, str):
            if len(item) > 16384:
                if not brush_slot(path):
                    raise ValueError('project JSON string exceeds limit')
                _large_brush_mask(item)
            budget -= len(json.dumps(item, ensure_ascii=False).encode('utf-8'))
        elif item is None or isinstance(item, bool):
            budget -= 5
        elif type(item) in (int, float):
            if abs(item) > 1e100 or not math.isfinite(item):
                raise ValueError('project JSON numbers must be finite and bounded')
            budget -= len(str(item))
        else:
            raise ValueError('project contains non-JSON data')
        if budget < 0:
            raise ValueError('project checkpoint exceeds size limit')

    visit(value, 0)


def _is_abs_path(value: str) -> bool:
    """绝对路径判定须跨平台：Windows 盘符路径（E:/…、E:\…）在 POSIX 下
    也会被接受，避免 Windows 版保存的项目在 macOS 上无法加载。"""
    if Path(value).is_absolute():
        return True
    return len(value) >= 3 and value[1] == ':' and value[2] in '/\\' and value[0].isascii() and value[0].isalpha()


def _path(value, name: str, *, empty=False) -> str:
    if (not isinstance(value, str) or len(value) > 4096
            or any(ord(c) < 32 for c in value)
            or (not (empty and value == '') and
                (not _is_abs_path(value) or value.startswith('//')))):
        raise ValueError(f'project {name} must be an absolute local path')
    return value


def _number(value, name: str, minimum, maximum, *, integer=False):
    if (type(value) not in ((int,) if integer else (int, float))
            or not math.isfinite(value) or not minimum <= value <= maximum):
        raise ValueError(f'project {name} is invalid')
    return value


def _boolean(value, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f'project {name} must be boolean')
    return value


def _photo(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError('project photos must contain objects')
    photo = {'path': _path(raw.get('path'), 'photo path')}
    for field in ('width', 'height'):
        photo[field] = _number(raw.get(field), field, 1, 200000, integer=True)
    for field in ('original_width', 'original_height'):
        photo[field] = _number(raw.get(field, 0), field, 0, 200000, integer=True)
    photo['file_size'] = (None if raw.get('file_size') is None else
                          _number(raw['file_size'], 'file_size', 0, 2**53-1, integer=True))
    photo['score'] = _number(raw.get('score'), 'score', 0, 1)
    for field in ('sharpness', 'exposure', 'contrast', 'colorfulness', 'face_quality'):
        photo[field] = _number(raw.get(field, .5 if field == 'face_quality' else 0), field, 0, 1)
    for field, default, minimum, maximum in (
            ('faces', 0, 0, 20000), ('blinks', 0, 0, 20000),
            ('group', -1, -1, MAX_PHOTOS), ('group_size', 1, 1, MAX_PHOTOS),
            ('group_rank', 1, 1, MAX_PHOTOS), ('rating', 0, 0, 5)):
        photo[field] = _number(raw.get(field, default), field, minimum, maximum, integer=True)
    photo['group_best'] = _boolean(raw.get('group_best', False), 'group_best')
    ai = raw.get('ai')
    photo['ai'] = None if ai is None else _number(ai, 'ai', 0, 10)
    flags = raw.get('flags', [])
    if (not isinstance(flags, list) or len(flags) > 128
            or any(not isinstance(flag, str) or len(flag) > 512 for flag in flags)):
        raise ValueError('project photo flags are invalid')
    photo['flags'] = list(flags)
    return photo


def _state(raw) -> dict:
    _bounded_json(raw)
    if not isinstance(raw, dict):
        raise ValueError('project state must be an object')
    if type(raw.get('version', 1)) is not int or raw.get('version', 1) != 1:
        raise ValueError('unsupported project checkpoint version; existing file preserved')
    photos = raw.get('photos', [])
    if not isinstance(photos, list) or len(photos) > MAX_PHOTOS:
        raise ValueError('project photos exceed limit or are invalid')
    photos = [_photo(photo) for photo in photos]
    paths = {photo['path'] for photo in photos}
    if len(paths) != len(photos):
        raise ValueError('project photos contain duplicate paths')
    selected = raw.get('selected', [])
    if (not isinstance(selected, list) or len(selected) > MAX_PHOTOS
            or any(not isinstance(path, str) or path not in paths for path in selected)):
        raise ValueError('project selected paths must belong to photos')
    target = raw.get('target')
    if target is not None and (not isinstance(target, str) or target not in paths):
        raise ValueError('project target must belong to photos')
    settings, drafts = raw.get('settings', {}), raw.get('drafts', {})
    if not isinstance(settings, dict) or not isinstance(drafts, dict):
        raise ValueError('project settings and drafts must be objects')
    # Copy through JSON so caller mutations cannot modify the checkpoint snapshot.
    return {'version': 1, 'folder': _path(raw.get('folder', ''), 'folder', empty=True),
            'photos': photos, 'selected': list(dict.fromkeys(selected)), 'target': target,
            'selection_touched': _boolean(raw.get('selection_touched', False), 'selection_touched'),
            'settings': json.loads(json.dumps(settings, ensure_ascii=False, allow_nan=False)),
            'drafts': json.loads(json.dumps(drafts, ensure_ascii=False, allow_nan=False)),
            'scan_incomplete': _boolean(raw.get('scan_incomplete', False), 'scan_incomplete')}


def _fingerprint(path: str) -> dict | None:
    try:
        stat = Path(path).stat()
        if not Path(path).is_file():
            return None
        return {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
    except OSError:
        return None


class ProjectStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path).expanduser() if path is not None else default_project_path()
        self._lock = threading.RLock()

    def save(self, state: dict) -> dict:
        with self._lock:
            snapshot = _state(state)
            for photo in snapshot['photos']:
                photo['_fingerprint'] = _fingerprint(photo['path'])
            data = json.dumps(snapshot, ensure_ascii=False, allow_nan=False,
                              separators=(',', ':')).encode('utf-8')
            if len(data) > MAX_PROJECT_BYTES:
                raise ValueError('project checkpoint exceeds size limit')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.path.parent,
                        prefix=f'.{self.path.name}.', suffix='.tmp', delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return {'ok': True, 'saved_at': time.time()}

    def load(self) -> dict:
        with self._lock:
            try:
                with self.path.open('rb') as handle:
                    data = handle.read(MAX_PROJECT_BYTES + 1)
            except FileNotFoundError:
                return {'state': None}
            if len(data) > MAX_PROJECT_BYTES:
                raise ValueError('project checkpoint exceeds size limit; existing file preserved')
            try:
                raw = json.loads(data)
                state = _state(raw)
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise ValueError(f'invalid project checkpoint; existing file preserved: {exc}') from exc
            missing, changed = [], []
            for photo, saved_photo in zip(state['photos'], raw.get('photos', [])):
                path = photo['path']
                current = _fingerprint(path)
                saved = saved_photo.get('_fingerprint')
                if current is None:
                    missing.append(path)
                elif current != saved:
                    changed.append(path)
                if current is not None:
                    photo['file_size'] = current['size']
                    if not photo['original_width'] or not photo['original_height']:
                        # Header-only backfill for legacy raster records. Never
                        # develop RAWs or mistake working-preview dimensions for originals.
                        try:
                            from .io import RAW_EXTS
                            if Path(path).suffix.lower() not in RAW_EXTS:
                                with Image.open(path) as image:
                                    if 0 < image.width <= 200000 and 0 < image.height <= 200000:
                                        photo.update(original_width=image.width, original_height=image.height)
                        except (OSError, ValueError, Image.DecompressionBombError):
                            pass
                photo['rating'] = read_rating(path, manual_only=True) or 0
                photo['thumb'] = f'/api/thumb?p={urllib.parse.quote(path)}&s=360'
            return {'state': state, 'missing': missing, 'changed': changed}


@lru_cache(maxsize=8)
def _store_for_path(path: Path) -> ProjectStore:
    return ProjectStore(path)


def get_project_store() -> ProjectStore:
    """Reuse a request-safe store while allowing an explicit test path override."""
    return _store_for_path(default_project_path())
