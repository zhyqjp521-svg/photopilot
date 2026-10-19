"""Durable export queue contract; all source files are temporary fixtures."""
import copy
import importlib
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


class DeepcopyTrap:
    def __deepcopy__(self, memo):
        raise AssertionError('unrequested payload was copied')


class ExportQueueTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('photopilot.export_queue'),
                             'Durable export queue is missing')
        self.module = importlib.import_module('photopilot.export_queue')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sources = [self.root / f'{i}.jpg' for i in range(3)]
        for src in self.sources:
            src.write_bytes(b'photo')
        self.path = self.root / 'data' / 'exports.json'
        self.queues = []
        self.addCleanup(lambda: [q.shutdown(2) for q in self.queues])

    def queue(self, renderer=None, autostart=True):
        q = self.module.ExportQueue(self.path, renderer, autostart)
        self.queues.append(q)
        return q

    def submit(self, q, recipe=None, request='request-1', paths=None):
        return q.submit(recipe or {'tone': {'exposure': .4}},
                        paths or [str(p) for p in self.sources], request)

    def wait(self, q, jid, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = q.get(jid)
            if predicate(job):
                return job
            time.sleep(.005)
        self.fail(f'timeout: {q.get(jid)}')

    def test_immutable_idempotent_canonical_bounded_private_state(self):
        q = self.queue(autostart=False)
        recipe = {'tone': {'exposure': .4}, 'enabled': True}
        job = self.submit(q, recipe, paths=[str(self.sources[0]), str(self.sources[0])])
        recipe['tone']['exposure'] = 99
        self.assertEqual(len(job['items']), 1)
        self.assertEqual(self.submit(q, {'tone': {'exposure': .4}, 'enabled': True},
                                     paths=[str(self.sources[0])])['id'], job['id'])
        with self.assertRaises(ValueError):
            self.submit(q, {'other': 2}, paths=[str(self.sources[0])])
        self.assertNotIn('recipe', job)
        self.assertNotIn('request_id', job)
        self.assertNotIn('fingerprint', job['items'][0])
        raw = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(raw['jobs'][0]['recipe']['tone']['exposure'], .4)
        if os.name != 'nt':
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        job['items'].clear()
        self.assertEqual(len(q.get(job['id'])['items']), 1)

    def test_summary_list_omits_private_payload_without_copying_items(self):
        q = self.queue(autostart=False)
        first = self.submit(q)['id']
        second = self.submit(q, request='second')['id']
        for stored in q._state['jobs']:
            stored['recipe'] = DeepcopyTrap()
            for item in stored['items']:
                item['result'] = DeepcopyTrap()
        q.error = 'checkpoint unavailable'
        summaries = q.list(summaries=True)
        self.assertEqual([job['id'] for job in summaries], [second, first])
        self.assertEqual(set(summaries[0]),
                         {'id', 'state', 'created_at', 'updated_at', 'counts', 'queue_error'})
        self.assertEqual(summaries[0]['counts']['pending'], 3)
        self.assertEqual(summaries[0]['counts']['total'], 3)
        self.assertEqual(summaries[0]['queue_error'], q.error)
        summaries[0]['counts']['pending'] = 99
        summaries[0]['state'] = 'failed'
        q._state['jobs'][1]['items'][0]['state'] = 'succeeded'
        q.error = None
        updated = q.list(summaries=True)[0]
        self.assertEqual(updated['state'], 'queued')
        self.assertEqual(updated['counts']['pending'], 2)
        self.assertEqual(updated['counts']['succeeded'], 1)
        self.assertIsNone(updated['queue_error'])

    def test_bounded_get_copies_only_slice_and_counts_all_source_items(self):
        q = self.queue(autostart=False)
        jid = self.submit(q)['id']
        stored = q._state['jobs'][0]
        stored['recipe'] = DeepcopyTrap()
        stored['items'][0].update(state='failed', result=DeepcopyTrap())
        stored['items'][1].update(state='succeeded', result={'out': '/tmp/result'})
        stored['items'][2].update(state='running', result=DeepcopyTrap())
        q.error = 'checkpoint unavailable'
        page = q.get(jid, offset=1, limit=1)
        self.assertEqual(page['offset'], 1)
        self.assertEqual(page['items_total'], 3)
        self.assertEqual(page['counts'],
                         {'pending': 0, 'running': 1, 'succeeded': 1, 'failed': 1, 'total': 3})
        self.assertEqual([item['index'] for item in page['items']], [1])
        self.assertNotIn('fingerprint', page['items'][0])
        self.assertNotIn('recipe', page)
        self.assertEqual(page['queue_error'], q.error)
        page['items'][0]['result']['out'] = 'changed'
        page['counts']['succeeded'] = 99
        self.assertEqual(q.get(jid, offset=1, limit=1)['items'][0]['result']['out'], '/tmp/result')
        stored['items'][0]['state'] = 'pending'
        q.error = None
        updated = q.get(jid, offset=1, limit=1)
        self.assertEqual(updated['counts']['failed'], 0)
        self.assertEqual(updated['counts']['pending'], 1)
        self.assertIsNone(updated['queue_error'])

    def test_bounded_get_handles_default_offset_and_end_of_items(self):
        q = self.queue(autostart=False)
        jid = self.submit(q)['id']
        self.assertEqual([item['index'] for item in q.get(jid, limit=2)['items']], [0, 1])
        self.assertEqual([item['index'] for item in q.get(jid, offset=2, limit=100)['items']], [2])
        for offset in (3, 100000):
            page = q.get(jid, offset=offset, limit=100)
            self.assertEqual(page['items'], [])
            self.assertEqual(page['offset'], offset)
            self.assertEqual(page['items_total'], 3)
            self.assertEqual(page['counts']['pending'], 3)

    def test_default_reads_remain_full_detached_and_without_pagination_fields(self):
        q = self.queue(autostart=False)
        original = self.submit(q)
        self.assertEqual(q.get(original['id'], limit=None), original)
        self.assertEqual(q.get(original['id']), original)
        self.assertEqual(q.list(), [original])
        self.assertEqual(q.list(summaries=False), [original])
        q._state['jobs'][0]['items'][0]['result'] = {'out': '/tmp/result'}
        full = q.list()[0]
        full['items'][0]['result']['out'] = 'changed'
        self.assertEqual(q.get(original['id'])['items'][0]['result']['out'], '/tmp/result')

    def test_bounded_get_rejects_malformed_limits_and_offsets(self):
        q = self.queue(autostart=False)
        jid = self.submit(q)['id']
        for limit in (True, False, 0, -1, 101, 1.0, '1', [], {}):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, 'limit'):
                q.get(jid, limit=limit)
        for offset in (True, False, -1, 1.0, '1', None, [], {}):
            with self.subTest(offset=offset), self.assertRaisesRegex(ValueError, 'offset'):
                q.get(jid, offset=offset, limit=1)

    def test_recipe_field_copies_only_requested_value_and_preserves_isolation(self):
        q = self.queue(autostart=False)
        source = str(self.sources[0])
        jid = self.submit(q, {'session': {'folder': str(self.root),
                                         'output_names': {source: 'photo.jpg'}},
                              'tone': {'exposure': .4}})['id']
        recipe = q._state['jobs'][0]['recipe']
        recipe['reference_image'] = DeepcopyTrap()
        recipe['session']['unused'] = DeepcopyTrap()
        self.assertEqual(q.get_recipe_field(jid, 'session', 'folder'), str(self.root))
        self.assertEqual(q.get_recipe_field(jid, 'session', 'output_names', source), 'photo.jpg')
        tone = q.get_recipe_field(jid, 'tone')
        tone['exposure'] = 99
        self.assertEqual(q.get_recipe_field(jid, 'tone', 'exposure'), .4)

    def test_recipe_field_rejects_missing_fields_and_malformed_paths(self):
        q = self.queue(autostart=False)
        jid = self.submit(q)['id']
        for keys in ((), ('',), (None,), (True,), (1,), ([],),
                     ('tone', ''), tuple('layer' for _ in range(9))):
            with self.subTest(keys=keys), self.assertRaises(ValueError):
                q.get_recipe_field(jid, *keys)
        for keys in (('absent',), ('tone', 'absent'), ('tone', 'exposure', 'child')):
            with self.subTest(keys=keys), self.assertRaises(ValueError):
                q.get_recipe_field(jid, *keys)
        with self.assertRaisesRegex(ValueError, 'job not found'):
            q.get_recipe_field('absent-job', 'tone')

    def test_recipe_field_accepts_eight_key_path(self):
        q = self.queue(autostart=False)
        recipe = value = {}
        keys = tuple(f'layer-{i}' for i in range(8))
        for key in keys[:-1]:
            value[key] = {}
            value = value[key]
        value[keys[-1]] = 'leaf'
        jid = self.submit(q, recipe)['id']
        self.assertEqual(q.get_recipe_field(jid, *keys), 'leaf')

    def test_serial_renderer_success_persisted_and_never_retried(self):
        calls = []
        def renderer(job, item):
            raw = json.loads(self.path.read_text(encoding='utf-8'))
            stored = next(j for j in raw['jobs'] if j['id'] == job['id'])
            self.assertEqual(stored['items'][item['index']]['state'], 'running')
            calls.append(item['path'])
            job['recipe']['tone']['exposure'] = 9
            return {'src': item['path'], 'out': '/tmp/own-result', 'width': 10, 'height': 20}
        q = self.queue(renderer)
        jid = self.submit(q)['id']
        job = self.wait(q, jid, lambda j: j['state'] == 'completed')
        self.assertEqual(job['counts']['succeeded'], 3)
        q.action(jid, 'retry')
        self.assertEqual(len(calls), 3)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['jobs'][0]['recipe']['tone']['exposure'], .4)

    def test_pause_retry_failed_only_and_pending(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def render(job, item):
            calls.append(item['index'])
            if len(calls) == 1:
                entered.set()
                release.wait(2)
                raise ValueError('decode failed')
            return {'out': '/tmp/result'}
        q = self.queue(render)
        jid = self.submit(q)['id']
        self.assertTrue(entered.wait(2))
        q.action(jid, 'pause')
        release.set()
        self.wait(q, jid, lambda j: j['counts']['failed'] == 1)
        self.assertEqual(calls, [0])
        q.action(jid, 'retry')
        self.wait(q, jid, lambda j: j['state'] == 'completed')
        self.assertEqual(calls, [0, 0, 1, 2])

    def test_resume_leaves_failed_items_failed(self):
        q = self.queue(lambda j, i: (_ for _ in ()).throw(ValueError('bad')))
        jid = self.submit(q)['id']
        self.wait(q, jid, lambda j: j['state'] == 'failed')
        q.action(jid, 'resume')
        self.assertEqual(q.get(jid)['counts']['failed'], 3)

    def test_restore_is_paused_and_requires_explicit_resume(self):
        q = self.queue(autostart=False)
        jid = self.submit(q)['id']
        raw = json.loads(self.path.read_text(encoding='utf-8'))
        raw['jobs'][0]['state'] = 'running'
        raw['jobs'][0]['items'][0]['state'] = 'running'
        self.path.write_text(json.dumps(raw), encoding='utf-8')
        calls = []
        restored = self.queue(lambda j, i: calls.append(i['path']) or {'out': '/tmp/result'})
        self.assertEqual(restored.get(jid)['state'], 'paused')
        self.assertEqual(restored.get(jid)['counts']['pending'], 3)
        self.assertEqual(calls, [])
        restored.action(jid, 'resume')
        self.wait(restored, jid, lambda j: j['state'] == 'completed')
        self.assertEqual(len(calls), 3)

    def test_restore_save_failure_recovers_on_explicit_resume_with_frozen_recipe(self):
        for saved_state in ('queued', 'running'):
            with self.subTest(saved_state=saved_state):
                self.path = self.root / saved_state / 'exports.json'
                q = self.queue(autostart=False)
                recipe = {'tone': {'exposure': .4}}
                jid = self.submit(q, recipe)['id']
                if saved_state == 'running':
                    raw = json.loads(self.path.read_text(encoding='utf-8'))
                    raw['jobs'][0]['state'] = 'running'
                    raw['jobs'][0]['items'][0]['state'] = 'running'
                    self.path.write_text(json.dumps(raw), encoding='utf-8')
                before = self.path.read_bytes()
                recipe['tone']['exposure'] = 99
                calls = []
                def render(job, item):
                    calls.append((item['index'], copy.deepcopy(job['recipe'])))
                    job['recipe']['tone']['exposure'] = 9
                    return {'out': f'/tmp/result-{item["index"]}'}
                with patch.object(self.module.os, 'replace', side_effect=OSError('disk full')):
                    restored = self.queue(render)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(restored.get(jid)['state'], saved_state)
                self.assertEqual(calls, [])
                self.assertTrue(restored.error.startswith('Export checkpoint write failed:'))
                with patch.object(self.module.os, 'replace', side_effect=OSError('disk still full')):
                    with self.assertRaises(OSError):
                        restored.action(jid, 'resume')
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(restored.get(jid)['state'], saved_state)
                self.assertEqual(calls, [])
                restored.action(jid, 'resume')
                job = self.wait(restored, jid, lambda job: job['state'] == 'completed')
                self.assertEqual(job['counts']['succeeded'], 3)
                self.assertEqual(calls, [(index, {'tone': {'exposure': .4}}) for index in range(3)])
                self.assertIsNone(restored.error)
                self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['jobs'][0]['recipe'],
                                 {'tone': {'exposure': .4}})
                restored.shutdown(2)

    def test_source_change_and_missing_prevent_render(self):
        calls = []
        q = self.queue(lambda j, i: calls.append(i['path']) or {}, autostart=False)
        jid = self.submit(q)['id']
        self.sources[0].write_bytes(b'changed')
        self.sources[1].unlink()
        q.action(jid, 'pause')
        q.action(jid, 'resume')
        job = self.wait(q, jid, lambda j: j['state'] == 'failed')
        self.assertEqual(job['counts']['failed'], 2)
        self.assertEqual(calls, [str(self.sources[2].resolve())])

    def test_source_change_during_render_fails_item(self):
        def render(job, item):
            Path(item['path']).write_bytes(b'changed during render')
            return {'out': '/tmp/result'}
        q = self.queue(render)
        jid = self.submit(q, paths=[str(self.sources[0])])['id']
        job = self.wait(q, jid, lambda j: j['state'] == 'failed')
        self.assertNotIn('result', job['items'][0])

    def test_save_failure_preserves_old_state_and_prevents_render(self):
        calls = []
        q = self.queue(lambda j, i: calls.append(i['path']) or {}, autostart=False)
        jid = self.submit(q)['id']
        before = self.path.read_bytes()
        with patch.object(self.module.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                q.action(jid, 'resume')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(calls, [])
        self.assertTrue(q.get(jid)['queue_error'])

    def test_completion_save_failure_stops_advancement(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def render(j, i):
            calls.append(i['index'])
            entered.set()
            release.wait(2)
            return {'out': '/tmp/result'}
        q = self.queue(render)
        jid = self.submit(q)['id']
        self.assertTrue(entered.wait(2))
        with patch.object(self.module.os, 'replace', side_effect=OSError('disk full')):
            release.set()
            self.wait(q, jid, lambda j: bool(j.get('queue_error')))
        self.assertEqual(calls, [0])
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['jobs'][0]['items'][0]['state'], 'running')

    def test_action_save_failure_stays_latched_after_active_render_finishes(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls = []
        def render(job, item):
            calls.append(item['index'])
            if item['index'] == 0:
                entered.set()
                self.assertTrue(release.wait(3))
            return {'out': f'/tmp/result-{item["index"]}'}
        q = self.queue(render)
        jid = self.submit(q)['id']
        self.assertTrue(entered.wait(2))
        before = self.path.read_bytes()
        worker = q._thread
        with patch.object(self.module.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                q.action(jid, 'pause')
        failure = q.error
        release.set()
        worker.join(2)
        self.assertEqual(q.get(jid)['queue_error'], failure)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(calls, [0])
        self.assertFalse(worker.is_alive())
        with patch.object(self.module.os, 'replace', side_effect=OSError('disk still full')):
            with self.assertRaises(OSError):
                q.action(jid, 'resume')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(q.get(jid)['items'][0]['state'], 'running')
        q.action(jid, 'resume')
        self.wait(q, jid, lambda job: job['state'] == 'completed')
        self.assertEqual(calls, [0, 1, 2])

    def test_completion_save_failure_recovers_result_without_rendering_twice(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls = []
        def render(job, item):
            calls.append(item['index'])
            if item['index'] == 0:
                entered.set()
                self.assertTrue(release.wait(3))
            return {'out': f'/tmp/result-{item["index"]}'}
        q = self.queue(render)
        jid = self.submit(q)['id']
        self.assertTrue(entered.wait(2))
        worker = q._thread
        before = self.path.read_bytes()
        with patch.object(self.module.os, 'replace', side_effect=OSError('disk full')):
            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(q.get(jid)['items'][0]['state'], 'running')
        recovered = q.action(jid, 'pause')
        self.assertIsNone(recovered['queue_error'])
        self.assertEqual(recovered['state'], 'paused')
        self.assertEqual(recovered['items'][0]['state'], 'succeeded')
        self.assertEqual(recovered['items'][0]['result']['out'], '/tmp/result-0')
        self.assertEqual(calls, [0])
        q.action(jid, 'resume')
        self.wait(q, jid, lambda job: job['state'] == 'completed')
        self.assertEqual(calls, [0, 1, 2])

    def test_idle_worker_exiting_on_save_failure_can_be_resumed(self):
        calls, idle = [], threading.Event()
        q = self.queue(lambda j, i: calls.append(i['index']) or {}, autostart=False)
        jid = self.submit(q)['id']
        q.action(jid, 'pause')
        original_wait = q._condition.wait
        def wait_until_notified(*args, **kwargs):
            idle.set()
            return original_wait(*args, **kwargs)
        with patch.object(q._condition, 'wait', side_effect=wait_until_notified):
            with q._condition:
                q._start()
                worker = q._thread
            self.assertTrue(idle.wait(2))
            with patch.object(self.module.os, 'replace', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    q.action(jid, 'resume')
            with q._condition:
                q._condition.notify_all()
            worker.join(2)
            self.assertFalse(worker.is_alive())
        q.action(jid, 'resume')
        self.wait(q, jid, lambda job: job['state'] == 'completed')
        self.assertEqual(calls, [0, 1, 2])

    def test_recovery_actions_preserve_active_render_until_completion(self):
        for action in ('pause', 'resume', 'retry'):
            with self.subTest(action=action):
                self.path = self.root / action / 'exports.json'
                entered, release = threading.Event(), threading.Event()
                self.addCleanup(release.set)
                calls = []
                def render(job, item):
                    calls.append(item['index'])
                    if len(calls) == 1:
                        entered.set()
                        self.assertTrue(release.wait(3))
                    return {'out': '/tmp/result'}
                q = self.queue(render)
                jid = self.submit(q)['id']
                self.assertTrue(entered.wait(2))
                worker = q._thread
                with patch.object(self.module.os, 'replace', side_effect=OSError('disk full')):
                    with self.assertRaises(OSError):
                        q.action(jid, 'pause')
                recovered = q.action(jid, action)
                self.assertIsNone(recovered['queue_error'])
                self.assertEqual(recovered['items'][0]['state'], 'running')
                self.assertIs(q._thread, worker)
                release.set()
                if action == 'pause':
                    self.wait(q, jid, lambda job: job['counts']['succeeded'] == 1)
                    self.assertEqual(q.get(jid)['state'], 'paused')
                    self.assertEqual(calls, [0])
                    q.action(jid, 'resume')
                self.wait(q, jid, lambda job: job['state'] == 'completed')
                self.assertEqual(calls, [0, 1, 2])
                q.shutdown(2)

    def test_corrupt_unsupported_oversize_state_preserved(self):
        self.path.parent.mkdir()
        for raw in (b'{broken', b'{"version":2,"jobs":[]}', b'{"version":true,"jobs":[]}'):
            self.path.write_bytes(raw)
            q = self.queue(autostart=False)
            self.assertTrue(q.error)
            with self.assertRaises(ValueError):
                self.submit(q)
            with self.assertRaisesRegex(ValueError, 'Export checkpoint unavailable:'):
                q.action('0' * 32, 'resume')
            self.assertEqual(self.path.read_bytes(), raw)
        with patch.object(self.module, 'MAX_STATE_BYTES', 10):
            q = self.queue(autostart=False)
            self.assertTrue(q.error)

    def test_validation_and_limits(self):
        q = self.queue(autostart=False)
        for recipe in (True, {'x': float('nan')}, {'x': object()}):
            with self.assertRaises(ValueError):
                self.submit(q, recipe)
        nested = {}
        for _ in range(20):
            nested = {'x': nested}
        with self.assertRaises(ValueError):
            self.submit(q, nested)
        for paths in ([True], ['relative.jpg'], [str(self.root)], []):
            with self.assertRaises(ValueError):
                q.submit({}, paths, 'x')
        for req in (True, '', 'x' * 201):
            with self.assertRaises(ValueError):
                q.submit({}, [str(self.sources[0])], req)
        with patch.object(self.module, 'MAX_JOBS', 1):
            self.submit(q)
            with self.assertRaises(ValueError):
                self.submit(q, request='another')

    def test_concurrent_submit_is_idempotent_and_newest_first(self):
        q = self.queue(autostart=False)
        ids = []
        threads = [threading.Thread(target=lambda: ids.append(self.submit(q)['id'])) for _ in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len(set(ids)), 1)
        second = self.submit(q, request='second')
        self.assertEqual(q.list()[0]['id'], second['id'])

    def test_candidate_depth_rejection_leaves_queue_healthy(self):
        recipe = {}
        for _ in range(14):
            recipe = {'nested': recipe}
        for existing in (False, True):
            with self.subTest(existing=existing):
                self.path = self.root / str(existing) / 'exports.json'
                q = self.queue(autostart=False)
                if existing:
                    self.submit(q, request='existing')
                before = self.path.read_bytes() if self.path.exists() else None
                jobs = q.list()
                with self.assertRaisesRegex(ValueError, 'too complex'):
                    self.submit(q, recipe, request='rejected')
                self.assertIsNone(q.error)
                self.assertEqual(q.list(), jobs)
                self.assertEqual(self.path.read_bytes() if self.path.exists() else None, before)
                accepted = self.submit(q, request='accepted')
                self.assertEqual(accepted['state'], 'queued')
                self.assertIsNone(q.error)

    def test_candidate_size_rejection_preserves_existing_jobs(self):
        q = self.queue(autostart=False)
        self.submit(q, request='existing')
        before = self.path.read_bytes()
        jobs = q.list()
        with patch.object(self.module, 'MAX_STATE_BYTES', len(before)):
            with self.assertRaisesRegex(ValueError, 'size limit'):
                self.submit(q, request='rejected')
        self.assertIsNone(q.error)
        self.assertEqual(q.list(), jobs)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.submit(q, request='accepted')['state'], 'queued')

    def test_completion_size_failure_remains_latched(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls = []
        def render(job, item):
            calls.append(item['index'])
            entered.set()
            self.assertTrue(release.wait(3))
            return {'out': '/tmp/result'}
        q = self.queue(render)
        jid = self.submit(q)['id']
        self.assertTrue(entered.wait(2))
        before = self.path.read_bytes()
        worker = q._thread
        with patch.object(self.module, 'MAX_STATE_BYTES', len(before)):
            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
        self.assertTrue(q.error.startswith('Export checkpoint write failed:'))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(q.get(jid)['items'][0]['state'], 'running')
        self.assertEqual(calls, [0])
        recovered = q.action(jid, 'pause')
        self.assertIsNone(recovered['queue_error'])
        self.assertEqual(recovered['items'][0]['state'], 'succeeded')
        self.assertEqual(calls, [0])

    def test_default_location_and_environment(self):
        if os.name == 'nt':
            expected = self.root / 'AppData/Roaming/PhotoPilot/exports.json'
            environment = {'APPDATA': str(self.root / 'AppData/Roaming')}
        else:
            expected = self.root / 'Library/Application Support/PhotoPilot/exports.json'
            environment = {}
        with patch.dict(os.environ, environment, clear=True), patch.object(Path, 'home', return_value=self.root):
            q = self.module.ExportQueue(autostart=False)
            self.addCleanup(q.shutdown)
            self.assertEqual(q.path, expected)
        with patch.dict(os.environ, {'PHOTOPILOT_EXPORT_PATH': str(self.path)}):
            q = self.module.ExportQueue(autostart=False)
            self.addCleanup(q.shutdown)
            self.assertEqual(q.path, self.path)

    def test_renderer_cannot_mutate_source_verification(self):
        def render(job, item):
            original = Path(item['path'])
            original.write_bytes(b'changed')
            item['path'] = str(self.sources[1].resolve())
            item['fingerprint'] = {'size': 5, 'mtime_ns': self.sources[1].stat().st_mtime_ns}
            return {'out': '/tmp/result'}
        q = self.queue(render)
        jid = self.submit(q, paths=[str(self.sources[0])])['id']
        job = self.wait(q, jid, lambda j: j['state'] in {'completed', 'failed'})
        self.assertEqual(job['state'], 'failed')

    def test_inconsistent_completed_checkpoint_rejected(self):
        q = self.queue(autostart=False)
        self.submit(q)
        raw = json.loads(self.path.read_text(encoding='utf-8'))
        raw['jobs'][0]['state'] = 'completed'
        before = json.dumps(raw).encode()
        self.path.write_bytes(before)
        restored = self.queue(autostart=False)
        self.assertTrue(restored.error)
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_renderer_result_fails_without_stopping_remaining_items(self):
        q = self.queue(lambda j, i: {'unexpected': 'bad'} if i['index'] == 0 else {'width': 1})
        jid = self.submit(q)['id']
        job = self.wait(q, jid, lambda j: j['state'] == 'failed')
        self.assertEqual(job['counts']['failed'], 1)
        self.assertEqual(job['counts']['succeeded'], 2)

    def test_lookup_original_request_before_missing_sources(self):
        q = self.queue(autostart=False)
        submission = {'strength': .4}
        jid = self.submit(q, {'submission': submission, 'session': {}})['id']
        self.sources[0].unlink()
        self.assertEqual(q.lookup_request('request-1', submission)['id'], jid)
        self.assertIsNone(q.lookup_request('absent'))
        with self.assertRaises(ValueError):
            q.lookup_request('request-1', {'strength': .5})

    def test_resume_retries_failed_persistence_before_starting_worker(self):
        calls = []
        q = self.queue(lambda j, i: calls.append(i['index']) or {'out': '/tmp/result'}, autostart=False)
        jid = self.submit(q)['id']
        with patch.object(self.module.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                q.action(jid, 'resume')
        q.action(jid, 'resume')
        self.wait(q, jid, lambda j: j['state'] == 'completed')
        self.assertEqual(calls, [0, 1, 2])
        self.assertIsNone(q.error)

    def test_forget_terminal_job_frees_capacity_without_touching_output(self):
        output = self.root / 'output.jpg'
        output.write_bytes(b'exported')
        q = self.queue(lambda j, i: {'out': str(output)})
        with patch.object(self.module, 'MAX_JOBS', 1):
            jid = self.submit(q)['id']
            self.wait(q, jid, lambda j: j['state'] == 'completed')
            self.assertEqual(q.action(jid, 'forget'), {'id': jid, 'state': 'removed'})
            self.submit(q, request='next')
        self.assertEqual(output.read_bytes(), b'exported')
        paused = self.queue(autostart=False)
        jid = self.submit(paused, request='pending')['id']
        with self.assertRaises(ValueError):
            paused.action(jid, 'forget')

    def test_huge_numeric_recipe_rejected_and_typed_identity_preserved(self):
        q = self.queue(autostart=False)
        with self.assertRaises(ValueError):
            self.submit(q, {'large': 10 ** 1000})
        self.submit(q, {'flag': True})
        with self.assertRaises(ValueError):
            self.submit(q, {'flag': 1})

    def test_idempotency_does_not_depend_on_dictionary_key_order(self):
        q = self.queue(autostart=False)
        jid = self.submit(q, {'first': 1, 'second': 2})['id']
        self.assertEqual(self.submit(q, {'second': 2, 'first': 1})['id'], jid)
