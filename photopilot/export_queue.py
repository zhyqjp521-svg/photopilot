"""Small, serial, durable export queue independent of UI and image rendering.

The renderer owns safe output publication. It must tolerate recovery of an item
whose output exists but whose completion checkpoint was interrupted. Restoration
never starts such work automatically: explicit resume is required.
"""
from __future__ import annotations

import copy
import json
import math
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

MAX_STATE_BYTES = 64 * 1024 * 1024
MAX_RECIPE_BYTES = 16 * 1024 * 1024
MAX_FILES = 20000
MAX_JOBS = 20
MAX_DEPTH = 16
MAX_NODES = 1000000
ITEM_STATES = {'pending', 'running', 'succeeded', 'failed'}
JOB_STATES = {'queued', 'running', 'paused', 'completed', 'failed'}
RESULT_KEYS = {'out', 'src', 'width', 'height', 'before', 'after'}


def _json_copy(value, limit):
    """Validate before encoding; bounded depth prevents recursive JSON attacks."""
    nodes = 0
    def visit(obj, depth):
        nonlocal nodes
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise ValueError('export data is too complex')
        if obj is None or isinstance(obj, (str, bool)):
            return
        if isinstance(obj, (int, float)):
            try:
                finite = math.isfinite(obj)
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError('export data must contain finite numbers')
            return
        if isinstance(obj, dict):
            if not all(isinstance(k, str) for k in obj):
                raise ValueError('export data keys must be strings')
            for child in obj.values():
                visit(child, depth + 1)
            return
        if isinstance(obj, list):
            for child in obj:
                visit(child, depth + 1)
            return
        raise ValueError('export data must be JSON')
    visit(value, 0)
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    except (ValueError, OverflowError, UnicodeError) as exc:
        raise ValueError('invalid export JSON') from exc
    if len(raw) > limit:
        raise ValueError('export data exceeds size limit')
    return json.loads(raw), raw


def _fingerprint(path):
    stat = Path(path).stat()
    if not Path(path).is_file():
        raise ValueError('source is not a file')
    return {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}


def _integer(value):
    return type(value) is int and value >= 0


class ExportQueue:
    def __init__(self, path=None, renderer=None, autostart=True, notifier=None):
        if path or os.environ.get('PHOTOPILOT_EXPORT_PATH'):
            default = path or os.environ.get('PHOTOPILOT_EXPORT_PATH')
        else:
            from .platform import app_data_dir
            default = app_data_dir() / 'exports.json'
        self.path = Path(default).expanduser()
        self.renderer = renderer
        # 可选终态回调：job 到达 completed/failed 时在锁外调用（桌面通知等）。
        self.notifier = notifier
        self.error = None
        self._state = {'version': 1, 'jobs': []}
        self._condition = threading.Condition(threading.RLock())
        self._thread = None
        self._active = None
        self._pending_completion = None
        self._closed = False
        self._load()
        if autostart and renderer and not self.error:
            with self._condition:
                self._start()

    def _load(self):
        if not self.path.exists():
            return
        try:
            if self.path.stat().st_size > MAX_STATE_BYTES:
                raise ValueError('export state exceeds size limit')
            with self.path.open('rb') as stream:
                raw = stream.read(MAX_STATE_BYTES + 1)
            if len(raw) > MAX_STATE_BYTES:
                raise ValueError('export state exceeds size limit')
            data = json.loads(raw)
            _json_copy(data, MAX_STATE_BYTES)
            self._validate_state(data)
            self._state = data
            restored = copy.deepcopy(data)
            changed = False
            for job in restored['jobs']:
                if job['state'] in {'queued', 'running'}:
                    job['state'] = 'paused'
                    changed = True
                for item in job['items']:
                    if item['state'] == 'running':
                        item['state'] = 'pending'
                        changed = True
            if changed:
                try:
                    self._commit(restored)
                except (OSError, ValueError):
                    # Valid data remains loaded; preserve the recoverable write
                    # failure so an explicit action can retry persistence.
                    pass
        except (OSError, ValueError, TypeError, RecursionError, OverflowError) as exc:
            self.error = f'Export checkpoint unavailable: {str(exc)[:500]}'

    def _validate_state(self, data):
        if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1:
            raise ValueError('unsupported export checkpoint version')
        jobs = data.get('jobs')
        if not isinstance(jobs, list) or len(jobs) > MAX_JOBS:
            raise ValueError('invalid export jobs')
        ids, requests = set(), set()
        for job in jobs:
            if not isinstance(job, dict):
                raise ValueError('invalid export job')
            jid, req = job.get('id'), job.get('request_id')
            if (not isinstance(jid, str) or len(jid) != 32 or any(c not in '0123456789abcdef' for c in jid)
                    or jid in ids or not isinstance(req, str) or not 1 <= len(req) <= 200 or req in requests):
                raise ValueError('invalid export identity')
            ids.add(jid)
            requests.add(req)
            if job.get('state') not in JOB_STATES or not isinstance(job.get('recipe'), dict):
                raise ValueError('invalid export job state or recipe')
            _json_copy(job['recipe'], MAX_RECIPE_BYTES)
            for field in ('created_at', 'updated_at'):
                val = job.get(field)
                if type(val) not in (int, float) or not math.isfinite(val) or val < 0:
                    raise ValueError('invalid export timestamp')
            items = job.get('items')
            if not isinstance(items, list) or not 1 <= len(items) <= MAX_FILES:
                raise ValueError('invalid export items')
            paths = set()
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    raise ValueError('invalid export item')
                path, fp = item.get('path'), item.get('fingerprint')
                if (not isinstance(path, str) or not path or '\x00' in path or not Path(path).is_absolute()
                        or path in paths or type(item.get('index')) is not int or item['index'] != index
                        or item.get('state') not in ITEM_STATES or not isinstance(fp, dict)
                        or set(fp) != {'size', 'mtime_ns'} or not all(_integer(v) for v in fp.values())):
                    raise ValueError('invalid export source or fingerprint')
                paths.add(path)
                if 'error' in item and (not isinstance(item['error'], str) or len(item['error']) > 1000):
                    raise ValueError('invalid export item error')
                if 'result' in item:
                    self._result(item['result'])
                if item['state'] == 'succeeded' and 'result' not in item:
                    raise ValueError('successful export requires result')
            if job['state'] == 'completed' and any(i['state'] != 'succeeded' for i in items):
                raise ValueError('completed export has unfinished items')
            if job['state'] == 'failed' and (any(i['state'] in {'pending', 'running'} for i in items)
                                            or not any(i['state'] == 'failed' for i in items)):
                raise ValueError('invalid failed export state')

    def _result(self, result):
        if not isinstance(result, dict) or set(result) - RESULT_KEYS:
            raise ValueError('invalid export result keys')
        for key, value in result.items():
            if key in {'width', 'height'}:
                if not _integer(value) or not 1 <= value <= 1000000:
                    raise ValueError('invalid export dimensions')
            elif not isinstance(value, str) or len(value) > 8192:
                raise ValueError('invalid export result path')
        return _json_copy(result, 65536)[0]

    def _commit(self, state, raw=None):
        """Publish memory only after the private checkpoint is atomically replaced."""
        temporary = None
        try:
            if raw is None:
                _, raw = _json_copy(state, MAX_STATE_BYTES)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(prefix='.exports-', suffix='.tmp', dir=self.path.parent,
                                             delete=False) as stream:
                temporary = Path(stream.name)
                os.chmod(temporary, 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self._state = state
            self.error = None
        except (OSError, ValueError) as exc:
            self.error = f'Export checkpoint write failed: {str(exc)[:500]}'
            raise
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def _healthy(self):
        if self.error:
            raise ValueError(self.error)
        if self._closed:
            raise ValueError('export queue is shut down')

    def _find(self, jid, state=None):
        for job in (state or self._state)['jobs']:
            if job['id'] == jid:
                return job
        raise ValueError('export job not found')

    def _public(self, job, summaries=False, offset=0, limit=None):
        source_items = job['items']
        counts = dict.fromkeys(ITEM_STATES, 0)
        for item in source_items:
            counts[item['state']] += 1
        counts['total'] = len(source_items)
        public = {'id': job['id'], 'state': job['state'], 'created_at': job['created_at'],
                  'updated_at': job['updated_at'], 'counts': counts, 'queue_error': self.error}
        if not summaries:
            selected = source_items if limit is None else source_items[offset:offset + limit]
            public['items'] = [{k: copy.deepcopy(v) for k, v in item.items() if k != 'fingerprint'}
                               for item in selected]
            if limit is not None:
                public.update(items_total=len(source_items), offset=offset)
        return public

    def list(self, summaries=False):
        """Return newest jobs first; summaries omit item payloads."""
        with self._condition:
            return [self._public(j, summaries=summaries) for j in reversed(self._state['jobs'])]

    def get(self, job_id, offset=0, limit=None):
        """Return the full job, or a bounded item page when limit is supplied."""
        if not _integer(offset):
            raise ValueError('invalid export item offset')
        if limit is not None and (not _integer(limit) or not 1 <= limit <= 100):
            raise ValueError('invalid export item limit')
        with self._condition:
            return self._public(self._find(job_id), offset=offset, limit=limit)

    def get_recipe_field(self, job_id, *keys):
        """Read one private recipe value without copying unrelated payloads."""
        if not 1 <= len(keys) <= 8 or any(not isinstance(key, str) or not key for key in keys):
            raise ValueError('invalid export recipe field path')
        with self._condition:
            value = self._find(job_id)['recipe']
            for key in keys:
                if not isinstance(value, dict) or key not in value:
                    raise ValueError('export recipe field not found')
                value = value[key]
            return copy.deepcopy(value)

    def lookup_request(self, request_id, submission=None):
        """Find an acknowledged submission without re-reading any source files."""
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
            raise ValueError('invalid export request id')
        if submission is not None:
            submission = _json_copy(submission, MAX_RECIPE_BYTES)[0]
        with self._condition:
            for job in self._state['jobs']:
                if job['request_id'] == request_id:
                    if submission is not None and _json_copy(job['recipe'].get('submission'), MAX_RECIPE_BYTES)[1] != _json_copy(submission, MAX_RECIPE_BYTES)[1]:
                        raise ValueError('export request id conflicts with original request')
                    return self._public(job)
            return None

    def submit(self, recipe, paths, request_id):
        if not isinstance(recipe, dict):
            raise ValueError('export recipe must be an object')
        recipe, recipe_raw = _json_copy(recipe, MAX_RECIPE_BYTES)
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
            raise ValueError('invalid export request id')
        if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_FILES:
            raise ValueError('invalid export paths')
        canonical, seen = [], set()
        for path in paths:
            if not isinstance(path, str) or not path or '\x00' in path or not Path(path).is_absolute():
                raise ValueError('export paths must be absolute file paths')
            resolved = str(Path(path).resolve())
            if resolved not in seen:
                canonical.append(resolved)
                seen.add(resolved)
        with self._condition:
            self._healthy()
            for job in self._state['jobs']:
                if job['request_id'] == request_id:
                    if _json_copy(job['recipe'], MAX_RECIPE_BYTES)[1] != recipe_raw or [i['path'] for i in job['items']] != canonical:
                        raise ValueError('export request id conflicts with original request')
                    return self._public(job)
            if len(self._state['jobs']) >= MAX_JOBS:
                raise ValueError('export queue job limit reached')
            items = []
            for index, path in enumerate(canonical):
                try:
                    fp = _fingerprint(path)
                except (OSError, ValueError) as exc:
                    raise ValueError('export source must be an existing file') from exc
                items.append({'path': path, 'index': index, 'fingerprint': fp, 'state': 'pending'})
            now = time.time()
            job = {'id': uuid.uuid4().hex, 'recipe': recipe, 'request_id': request_id,
                   'state': 'queued', 'created_at': now, 'updated_at': now, 'items': items}
            candidate = copy.deepcopy(self._state)
            candidate['jobs'].append(job)
            # Reject unpersistable submissions before entering the checkpoint
            # failure latch; worker outcomes still validate inside _commit.
            _, raw = _json_copy(candidate, MAX_STATE_BYTES)
            self._commit(candidate, raw)
            self._condition.notify_all()
            return self._public(job)

    def action(self, job_id, action):
        if action not in {'pause', 'resume', 'retry', 'forget'}:
            raise ValueError('invalid export queue action')
        with self._condition:
            recover = bool(self.error and self.error.startswith('Export checkpoint write failed:'))
            if not recover:
                self._healthy()
            elif self._closed:
                raise ValueError('export queue is shut down')
            checkpoint = self._state
            if recover and self._pending_completion is not None:
                checkpoint = self._pending_completion
            candidate = copy.deepcopy(checkpoint)
            if recover:
                for saved in candidate['jobs']:
                    if saved['state'] in {'queued', 'running'}:
                        saved['state'] = 'paused'
                    for item in saved['items']:
                        if item['state'] == 'running' and self._active != (saved['id'], item['index']):
                            item['state'] = 'pending'
            job = self._find(job_id, candidate)
            if action == 'forget':
                if job['state'] not in {'completed', 'failed'} or any(i['state'] in {'pending', 'running'} for i in job['items']):
                    raise ValueError('only terminal export jobs may be forgotten')
                candidate['jobs'].remove(job)
                self._commit(candidate)
                self._pending_completion = None
                return {'id': job_id, 'state': 'removed'}
            if action == 'pause':
                if job['state'] not in {'completed', 'failed'}:
                    job['state'] = 'paused'
            else:
                if action == 'retry':
                    for item in job['items']:
                        if item['state'] == 'failed':
                            item['state'] = 'pending'
                            item.pop('error', None)
                            item.pop('result', None)
                if any(i['state'] in {'pending', 'running'} for i in job['items']):
                    job['state'] = 'running' if any(i['state'] == 'running' for i in job['items']) else 'queued'
            job['updated_at'] = time.time()
            self._commit(candidate)
            self._pending_completion = None
            if action != 'pause' and self.renderer:
                self._start()
            self._condition.notify_all()
            return self._public(job)

    def _start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._worker, name='PhotoPilotExport', daemon=True)
            self._thread.start()

    def _worker(self):
        while True:
            with self._condition:
                if self._closed or self.error:
                    self._thread = None
                    return
                found = next(((j, i) for j in self._state['jobs'] if j['state'] in {'queued', 'running'}
                              for i in j['items'] if i['state'] == 'pending'), None)
                if found is None:
                    self._condition.wait()
                    continue
                job, item = found
                jid, index = job['id'], item['index']
                candidate = copy.deepcopy(self._state)
                running = self._find(jid, candidate)
                running['state'] = 'running'
                running['items'][index]['state'] = 'running'
                running['updated_at'] = time.time()
                try:
                    self._commit(candidate)
                except (OSError, ValueError):
                    self._thread = None
                    return
                render_job = {'id': jid, 'recipe': copy.deepcopy(running['recipe'])}
                original = copy.deepcopy(running['items'][index])
                render_item = {'path': original['path'], 'index': index}
                self._active = (jid, index)
            result, error = None, None
            try:
                if _fingerprint(original['path']) != original['fingerprint']:
                    raise ValueError('source changed since export was queued')
                result = self._result(self.renderer(render_job, render_item))
                if _fingerprint(original['path']) != original['fingerprint']:
                    raise ValueError('source changed during export')
            except Exception as exc:
                error = str(exc)[:1000] or type(exc).__name__
            notification = None
            with self._condition:
                self._active = None
                candidate = copy.deepcopy(self._state)
                finished = self._find(jid, candidate)
                current = finished['items'][index]
                current['state'] = 'failed' if error else 'succeeded'
                if error:
                    current['error'] = error
                else:
                    current['result'] = result
                if not any(i['state'] in {'pending', 'running'} for i in finished['items']):
                    finished['state'] = 'failed' if any(i['state'] == 'failed' for i in finished['items']) else 'completed'
                elif finished['state'] != 'paused':
                    finished['state'] = 'queued'
                if finished['state'] in {'completed', 'failed'}:
                    notification = {
                        'id': jid, 'state': finished['state'],
                        'total': len(finished['items']),
                        'failed': sum(1 for i in finished['items'] if i['state'] == 'failed'),
                    }
                finished['updated_at'] = time.time()
                if self.error:
                    # A failed user checkpoint must remain latched. Keep the
                    # renderer outcome private until an explicit action saves it.
                    self._pending_completion = candidate
                    self._thread = None
                    self._condition.notify_all()
                    return
                try:
                    self._commit(candidate)
                except (OSError, ValueError):
                    self._pending_completion = candidate
                    self._thread = None
                    return
                self._condition.notify_all()
            if notification and callable(self.notifier):
                # 锁外调用，回调异常绝不影响导出循环。
                try:
                    self.notifier(notification)
                except Exception:
                    pass

    def shutdown(self, timeout=5):
        with self._condition:
            self._closed = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
