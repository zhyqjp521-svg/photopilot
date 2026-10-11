"""Bounded JSON codec for immutable prepared export settings.

References contain pixels/statistics, never source-file handles or derived caches.
Restoring a recipe does not read source or reference images or re-estimate a batch.
"""
from __future__ import annotations

import base64
import binascii
import json
import math
import os
from dataclasses import asdict, fields
from pathlib import Path

import numpy as np

from .color import ColorReference, PRESETS
from .io import collision_safe_output_names
from .polish import PolishParams

MAX_RECIPE_BYTES = 16 * 1024 * 1024
MAX_PATHS = 20_000
MAX_REFERENCE_SIDE = 1024
_ALGORITHMS = {'none', 'oklab', 'reinhard', 'mkl', 'histogram', 'luma_hist'}
_FIELDS = {'folder', 'paths', 'output_names', 'operation', 'do_color', 'do_polish',
           'algo', 'strength', 'preserve_luma', 'skin_protect', 'params',
           'export_size', 'export_quality', 'scene_ref', 'wb_mode', 'wb_strength',
           'wb_target', 'wb_gains', 'reference', 'scene_refs', 'ref_desc'}
_OPTIONAL = {'wb_target', 'wb_gains', 'scene_refs'}
_PARAM_FIELDS = {field.name for field in fields(PolishParams)}
_BOOL_PARAMS = {'eyes', 'global_skin', 'auto_tone'}
_TONE_PARAMS = {'exposure', 'contrast', 'highlights', 'shadows', 'temperature', 'saturation'}


def _mapping(value, required, optional=()):
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - set(optional):
        raise ValueError('导出配方结构无效')
    return value


def _check_json_size(recipe):
    # Budget the JSON before decoding any image. Streaming avoids constructing an
    # oversized complete serialization; the shallow walk also bounds encoder
    # recursion and rejects individual oversized strings before escaping them.
    nodes = 0
    def walk(value, depth=0):
        nonlocal nodes
        nodes += 1
        if depth > 12 or nodes > 1_000_000:
            raise ValueError('导出配方结构超过限制')
        if isinstance(value, str) and len(value) > MAX_RECIPE_BYTES:
            raise ValueError('导出配方超过大小限制')
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise ValueError('导出配方不是 JSON 数据')
                walk(key, depth + 1)
                walk(child, depth + 1)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child, depth + 1)
    walk(recipe)
    try:
        total = 0
        encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        for chunk in encoder.iterencode(recipe):
            total += len(chunk.encode('utf-8'))
            if total > MAX_RECIPE_BYTES:
                raise ValueError('导出配方超过大小限制')
    except (TypeError, RecursionError, OverflowError, UnicodeError) as exc:
        raise ValueError('导出配方不是 JSON 数据') from exc


def _number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float, np.integer, np.floating)):
        raise ValueError('导出配方需要数值')
    try:
        value = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError('导出配方数值无效') from exc
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError('导出配方数值超出范围')
    return value


def _boolean(value):
    if type(value) is not bool:
        raise ValueError('导出配方需要布尔值')
    return value


def _path(value, *, canonicalize=False):
    if canonicalize and isinstance(value, Path):
        value = str(value)
    if not isinstance(value, str) or not value or len(value) > 4096 or '\x00' in value or '://' in value:
        raise ValueError('导出配方路径无效')
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('导出配方需要绝对路径')
    if canonicalize:
        try:
            return str(path.resolve())
        except (OSError, RuntimeError) as exc:
            raise ValueError('导出配方路径无效') from exc
    if os.path.normpath(value) != value:
        raise ValueError('导出配方路径尚未规范化')
    return value


def _vector(value, low, high):
    if isinstance(value, np.ndarray):
        if value.shape != (3,):
            raise ValueError('导出配方向量需要三个通道')
        value = value.tolist()
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError('导出配方向量需要三个通道')
    result = np.array([_number(item, low, high) for item in value], dtype=np.float32)
    if not np.isfinite(result).all():
        raise ValueError('导出配方向量超出范围')
    return result


def _freeze_reference(reference):
    if reference is None:
        return None
    if not isinstance(reference, ColorReference):
        raise ValueError('导出配方参考类型无效')
    if reference.kind == 'image':
        image = reference.image
        if (not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3
                or image.shape[2] != 3 or any(not 1 <= side <= MAX_REFERENCE_SIDE for side in image.shape[:2])
                or reference.preset is not None or reference.stats is not None):
            raise ValueError('导出配方参考照片必须是有界 RGB uint8')
        return {'kind': 'image', 'shape': list(image.shape),
                'data': base64.b64encode(image.tobytes(order='C')).decode('ascii')}
    if reference.kind == 'preset':
        if reference.image is not None or reference.stats is not None:
            raise ValueError('导出配方参考类型不一致')
        return {'kind': 'preset', 'preset': reference.preset}
    if reference.kind == 'stats':
        if (reference.image is not None or reference.preset is not None
                or not isinstance(reference.stats, (tuple, list)) or len(reference.stats) != 2):
            raise ValueError('导出配方参考统计无效')
        return {'kind': 'stats', 'mean': _vector(reference.stats[0], -2, 2).tolist(),
                'std': _vector(reference.stats[1], 0, 2).tolist()}
    raise ValueError('导出配方参考类型无效')


def _thaw_reference(reference):
    if reference is None:
        return None
    if not isinstance(reference, dict):
        raise ValueError('导出配方参考类型无效')
    kind = reference.get('kind')
    if kind == 'preset':
        _mapping(reference, {'kind', 'preset'})
        if not isinstance(reference['preset'], str) or reference['preset'] not in PRESETS:
            raise ValueError('导出配方预设未知')
        return ColorReference(kind, preset=reference['preset'])
    if kind == 'stats':
        _mapping(reference, {'kind', 'mean', 'std'})
        return ColorReference(kind, stats=(_vector(reference['mean'], -2, 2),
                                           _vector(reference['std'], 0, 2)))
    if kind == 'image':
        _mapping(reference, {'kind', 'shape', 'data'})
        shape, data = reference['shape'], reference['data']
        if (not isinstance(shape, list) or len(shape) != 3 or any(type(x) is not int for x in shape)
                or shape[2] != 3 or any(not 1 <= x <= MAX_REFERENCE_SIDE for x in shape[:2])):
            raise ValueError('导出配方参考照片尺寸无效')
        length = shape[0] * shape[1] * 3
        if not isinstance(data, str) or len(data) != 4 * ((length + 2) // 3):
            raise ValueError('导出配方参考照片数据长度无效')
        try:
            raw = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError('导出配方参考照片编码无效') from exc
        if len(raw) != length:
            raise ValueError('导出配方参考照片数据长度无效')
        return ColorReference(kind, image=np.frombuffer(raw, dtype=np.uint8).reshape(shape).copy())
    raise ValueError('导出配方参考类型无效')


def thaw_session(recipe: dict) -> dict:
    """Validate version-1 JSON and create fresh render objects without image I/O."""
    _mapping(recipe, _FIELDS | {'version'})
    _check_json_size(recipe)
    if type(recipe['version']) is not int or recipe['version'] != 1:
        raise ValueError('不支持的导出配方版本')
    paths = recipe['paths']
    if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_PATHS:
        raise ValueError('导出配方照片数量无效')
    paths = [_path(path) for path in paths]
    if len(set(paths)) != len(paths):
        raise ValueError('导出配方照片重复')
    folder = _path(recipe['folder'])
    names = recipe['output_names']
    if not isinstance(names, dict) or names != collision_safe_output_names(paths):
        raise ValueError('导出配方输出文件名无效')
    for name in names.values():
        if (not isinstance(name, str) or not name or '/' in name or '\\' in name or '\x00' in name
                or name in {'.', '..'} or Path(name).name != name):
            raise ValueError('导出配方输出文件名不安全')
    operation = recipe['operation']
    if not isinstance(operation, str) or operation not in {'color', 'polish', 'both'}:
        raise ValueError('导出配方操作未知')
    color, polish = _boolean(recipe['do_color']), _boolean(recipe['do_polish'])
    if color != (operation in {'color', 'both'}) or polish != (operation in {'polish', 'both'}):
        raise ValueError('导出配方操作不一致')
    if not isinstance(recipe['algo'], str) or recipe['algo'] not in _ALGORITHMS:
        raise ValueError('导出配方算法未知')
    size, quality = recipe['export_size'], recipe['export_quality']
    if size is not None and (type(size) is not int or size not in {1024, 2048, 2560, 4096}):
        raise ValueError('导出配方导出尺寸无效')
    if type(quality) is not int or not 75 <= quality <= 100:
        raise ValueError('导出配方导出质量无效')
    params = _mapping(recipe['params'], _PARAM_FIELDS)
    validated_params = {}
    for name, value in params.items():
        if name in _BOOL_PARAMS:
            validated_params[name] = _boolean(value)
        elif name == 'local_region':
            if not isinstance(value, str) or value not in {'all', 'global', '全图', 'skin', 'face', 'eyes', 'background'}:
                raise ValueError('导出配方局部区域未知')
            validated_params[name] = value
        else:
            limit = 3 if name == 'exposure' else 1
            validated_params[name] = _number(value, -limit if name in _TONE_PARAMS else 0, limit)
    reference = _thaw_reference(recipe['reference'])
    if operation == 'color' and (reference is None or reference.kind != 'image'):
        raise ValueError('追色配方需要目标照片')
    if operation == 'polish' and reference is not None:
        raise ValueError('美化配方不能追色')
    if recipe['algo'] == 'none' and color and reference is not None:
        raise ValueError('导出配方算法与参考不一致')
    scene_ref = _boolean(recipe['scene_ref'])
    scenes = recipe['scene_refs']
    if (not isinstance(scenes, dict) or scenes.keys() - set(paths)
            or (scenes and (not scene_ref or not color))):
        raise ValueError('导出配方场景参考无效')
    scene_refs = {}
    for path, value in scenes.items():
        ref = _thaw_reference(value)
        if ref is None or ref.kind != 'stats':
            raise ValueError('导出配方场景参考需要统计')
        scene_refs[path] = ref
    wb_mode = recipe['wb_mode']
    if not isinstance(wb_mode, str) or wb_mode not in {'off', 'single', 'batch'}:
        raise ValueError('导出配方白平衡模式未知')
    gains, target = recipe['wb_gains'], recipe['wb_target']
    if not isinstance(gains, dict) or gains.keys() - set(paths):
        raise ValueError('导出配方白平衡路径无效')
    if wb_mode == 'batch':
        if gains.keys() != set(paths):
            raise ValueError('导出配方白平衡数据不完整')
        target = _vector(target, 1e-6, 255)
        gains = {path: _vector(value, 1e-12, 255_000_001) for path, value in gains.items()}
    elif gains or target is not None:
        raise ValueError('导出配方白平衡模式不一致')
    description = recipe['ref_desc']
    if not isinstance(description, str) or len(description) > 4096:
        raise ValueError('导出配方描述无效')
    sp = recipe['skin_protect']
    if sp is not None:
        sp = _boolean(sp)
    result = dict(folder=Path(folder), paths=list(paths), output_names=dict(names),
                  operation=operation, do_color=color, do_polish=polish, algo=recipe['algo'],
                  strength=_number(recipe['strength'], 0, 1),
                  preserve_luma=_boolean(recipe['preserve_luma']), skin_protect=sp,
                  params=PolishParams(**validated_params), export_size=size, export_quality=quality,
                  scene_ref=scene_ref, reference=reference, scene_refs=scene_refs,
                  wb_mode=wb_mode, wb_strength=_number(recipe['wb_strength'], 0, 1),
                  ref_desc=description)
    if wb_mode == 'batch':
        result.update(wb_target=target, wb_gains=gains)
    return result


def freeze_session(session: dict) -> dict:
    """Freeze a server-prepared session; reject arbitrary destinations or handles."""
    _mapping(session, _FIELDS - _OPTIONAL, _OPTIONAL)
    raw_paths = session['paths']
    if not isinstance(raw_paths, list) or not 1 <= len(raw_paths) <= MAX_PATHS:
        raise ValueError('导出配方照片数量无效')
    if any(not isinstance(path, (str, Path)) for path in raw_paths):
        raise ValueError('导出配方路径无效')
    names = session['output_names']
    if (not isinstance(names, dict) or len(names) > MAX_PATHS
            or names != collision_safe_output_names(raw_paths)):
        raise ValueError('导出配方输出文件名无效')
    path_map = {path: _path(path, canonicalize=True) for path in raw_paths}
    paths = list(dict.fromkeys(path_map.values()))
    if not isinstance(session['params'], PolishParams):
        raise ValueError('导出配方美化参数无效')
    # Resolving source aliases changes both stems and collision groups. Validate
    # the prepared names first, then derive names for the canonical source list.
    names = collision_safe_output_names(paths)
    scene_refs = session.get('scene_refs', {})
    wb_gains = session.get('wb_gains', {})
    if (not isinstance(scene_refs, dict) or not isinstance(wb_gains, dict)
            or len(scene_refs) > MAX_PATHS or len(wb_gains) > MAX_PATHS):
        raise ValueError('导出配方参考映射无效')
    if any(not isinstance(ref, ColorReference) or ref.kind != 'stats' for ref in scene_refs.values()):
        raise ValueError('导出配方场景参考需要统计')
    recipe = {key: session[key] for key in _FIELDS - _OPTIONAL - {'params', 'reference', 'folder', 'paths', 'output_names'}}
    recipe.update(version=1, folder=_path(session['folder'], canonicalize=True), paths=paths,
                  output_names=names, params=asdict(session['params']),
                  reference=_freeze_reference(session['reference']),
                  scene_refs={path_map.get(path, path): _freeze_reference(ref) for path, ref in scene_refs.items()},
                  wb_gains={path_map.get(path, path): _vector(value, 1e-12, 255_000_001).tolist()
                            for path, value in wb_gains.items()},
                  wb_target=None if session.get('wb_target') is None else _vector(session['wb_target'], 1e-6, 255).tolist())
    thaw_session(recipe)
    # JSON normalization ensures callers receive no numpy scalars or live objects.
    return json.loads(json.dumps(recipe, ensure_ascii=False, allow_nan=False))
