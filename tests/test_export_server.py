"""Durable export adapter: real files and real loopback HTTP boundary."""
from __future__ import annotations

import copy
import http.client
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from photopilot import server


class ExportServerTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(callable(getattr(server, '_submit_export', None)),
                        'durable export submission adapter is missing')
        from photopilot.export_queue import ExportQueue
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / '测试 & a.png'
        Image.new('RGB', (48, 32), (120, 90, 60)).save(self.source)
        self.original = self.source.read_bytes()
        self.queue = ExportQueue(self.root / 'exports.json', renderer=server._render_export,
                                 autostart=False)
        self.addCleanup(self.queue.shutdown)
        self.getter = patch.object(server, '_get_export_queue', return_value=self.queue)
        self.getter.start()
        self.addCleanup(self.getter.stop)
        self.faces = patch.object(server, 'analyze_faces', return_value=[])
        self.faces.start()
        self.addCleanup(self.faces.stop)
        self.payload = {'request_id': 'test-request-1', 'settings': {
            'folder': str(self.root), 'paths': [str(self.source)], 'operation': 'polish',
            'polish': {'wb': 0, 'skin': 0, 'clarity': 0, 'eyes': False,
                       'exposure': 0.1}, 'export_size': 'original'}}

    def submit(self):
        return server._submit_export(copy.deepcopy(self.payload))['job']

    def wait(self, jid):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.queue.get(jid)
            if job['state'] in {'completed', 'failed'}:
                return job
            time.sleep(.01)
        self.fail('export did not finish')

    def http_server(self):
        http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        return http

    def request(self, httpd, method, path, payload=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=3)
        try:
            conn.request(method, path, body=None if payload is None else json.dumps(payload),
                         headers=headers or {})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_queue_exports_real_png_after_runtime_sessions_are_evicted(self):
        job = self.submit()
        with patch.object(server, '_SESSIONS', {}):
            self.queue.action(job['id'], 'resume')
            done = self.wait(job['id'])
        self.assertEqual(done['state'], 'completed', done)
        result = done['items'][0]['result']
        with Image.open(result['out']) as image:
            self.assertEqual(image.size, (48, 32))
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(Path(result['out']).parent,
                         self.root / 'photopilot_out' / job['id'])

    def test_duplicate_request_survives_source_deletion_without_preparing_again(self):
        job = self.submit()
        self.source.unlink()
        with patch.object(server, '_prepare', side_effect=AssertionError('must not reprepare')):
            self.assertEqual(self.submit()['id'], job['id'])

    def test_duplicate_request_rejects_changed_settings(self):
        self.submit()
        self.payload['settings']['polish']['exposure'] = .2
        with self.assertRaises(ValueError):
            self.submit()

    def test_frontend_numeric_size_string_is_canonicalized(self):
        self.payload['settings']['export_size'] = '2048'
        job = self.submit()
        self.payload['settings']['export_size'] = 2048
        self.assertEqual(self.submit()['id'], job['id'])

    def test_preparing_queue_does_not_evict_live_editor_sessions(self):
        live = {str(index): {'editor': index} for index in range(16)}
        with patch.object(server, '_SESSIONS', live):
            self.submit()
            self.assertEqual(len(live), 16)
            self.assertEqual(live['0'], {'editor': 0})

    def test_explicit_global_skin_setting_is_frozen(self):
        self.payload['settings']['polish']['global_skin'] = False
        self.submit()
        stored = json.loads((self.root / 'exports.json').read_text(encoding='utf-8'))
        self.assertFalse(stored['jobs'][0]['recipe']['session']['params']['global_skin'])

    def test_restored_queue_uses_frozen_batch_white_balance_without_reprepare(self):
        from photopilot.export_queue import ExportQueue
        self.payload['settings']['wb_mode'] = 'batch'
        job = self.submit()
        self.queue.shutdown()
        self.queue = ExportQueue(self.root / 'exports.json', renderer=server._render_export)
        self.addCleanup(self.queue.shutdown)
        self.assertEqual(self.queue.get(job['id'])['state'], 'paused')
        with (patch.object(server, '_prepare', side_effect=AssertionError('no reprepare')),
              patch.object(server, 'estimate_wb_gains', side_effect=AssertionError('no estimation'))):
            self.queue.action(job['id'], 'resume')
            self.assertEqual(self.wait(job['id'])['state'], 'completed')

    def test_same_named_sources_have_distinct_output_files(self):
        second_dir = self.root / 'second'
        second_dir.mkdir()
        second = second_dir / self.source.name
        Image.new('RGB', (48, 32), (30, 40, 50)).save(second)
        self.payload['settings']['paths'].append(str(second))
        job = self.submit()
        self.queue.action(job['id'], 'resume')
        done = self.wait(job['id'])
        self.assertEqual(done['state'], 'completed', done)
        self.assertEqual(len({item['result']['out'] for item in done['items']}), 2)

    def test_retry_uses_frozen_settings_instead_of_new_ui_values(self):
        job = self.submit()
        with patch.object(server, 'save_image', side_effect=OSError('temporary write failure')):
            self.queue.action(job['id'], 'resume')
            self.assertEqual(self.wait(job['id'])['state'], 'failed')
        self.payload['settings']['polish']['exposure'] = .9
        with patch.object(server, '_prepare', side_effect=AssertionError('retry must not reprepare')):
            self.queue.action(job['id'], 'retry')
            self.assertEqual(self.wait(job['id'])['state'], 'completed')
        stored = json.loads((self.root / 'exports.json').read_text(encoding='utf-8'))
        self.assertEqual(stored['jobs'][0]['recipe']['session']['params']['exposure'], .1)

    def test_bad_settings_are_rejected_before_image_preparation(self):
        for bad in ({'strength': float('nan')}, {'algo': 'unknown'}, {'operation': 'delete'},
                    {'polish': {'exposure': True}}, {'polish': {'unknown': 1}},
                    {'scene_ref': 'false'}, {'extra': 1}):
            with self.subTest(bad=bad):
                payload = copy.deepcopy(self.payload)
                payload['settings'].update(bad)
                with patch.object(server, '_prepare', side_effect=AssertionError('validation too late')):
                    with self.assertRaises(ValueError):
                        server._submit_export(payload)

    def test_color_without_reference_is_rejected_before_batch_decode(self):
        self.payload['settings'].update(operation='color', wb_mode='batch')
        with patch.object(server, 'load_image', side_effect=AssertionError('decoded before validation')):
            with self.assertRaisesRegex(ValueError, '目标照片'):
                self.submit()

    def test_source_mutation_during_preparation_rejects_submission(self):
        original_prepare = server._prepare
        def changed(settings, **kwargs):
            result = original_prepare(settings, **kwargs)
            self.source.write_bytes(b'changed source')
            return result
        with patch.object(server, '_prepare', side_effect=changed):
            with self.assertRaises(ValueError):
                self.submit()
        self.assertEqual(self.queue.list(), [])

    def test_source_mutation_during_render_never_publishes_result(self):
        job = self.submit()
        original_save = server.save_image
        def mutate(image, destination, **kwargs):
            self.source.write_bytes(b'changed after decode')
            return original_save(image, destination, **kwargs)
        with patch.object(server, 'save_image', side_effect=mutate):
            self.queue.action(job['id'], 'resume')
            done = self.wait(job['id'])
        self.assertEqual(done['state'], 'failed')
        out = self.root / 'photopilot_out' / job['id']
        self.assertEqual(list(out.iterdir()) if out.exists() else [], [])

    def test_output_collision_never_overwrites_existing_file(self):
        job = self.submit()
        folder = self.root / 'photopilot_out' / job['id']
        folder.mkdir(parents=True)
        from photopilot.io import collision_safe_output_names
        destination = folder / collision_safe_output_names([str(self.source)])[str(self.source)]
        destination.write_bytes(b'precious existing result')
        self.queue.action(job['id'], 'resume')
        self.assertEqual(self.wait(job['id'])['state'], 'failed')
        self.assertEqual(destination.read_bytes(), b'precious existing result')
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_windows_export_does_not_require_posix_directory_fd_apis(self):
        job = self.submit()
        with (patch.object(server, '_posix_directory_fd_available', return_value=False),
              patch.object(server, '_posix_publish_export', side_effect=AssertionError(
                  'POSIX publish path is unavailable on Windows'))):
            self.queue.action(job['id'], 'resume')
            done = self.wait(job['id'])
        self.assertEqual(done['state'], 'completed', done)
        with Image.open(done['items'][0]['result']['out']) as image:
            self.assertEqual(image.size, (48, 32))

    def test_windows_output_collision_never_overwrites_existing_file(self):
        job = self.submit()
        folder = self.root / 'photopilot_out' / job['id']
        folder.mkdir(parents=True)
        from photopilot.io import collision_safe_output_names
        destination = folder / collision_safe_output_names([str(self.source)])[str(self.source)]
        destination.write_bytes(b'precious existing result')
        with patch.object(server, '_posix_directory_fd_available', return_value=False):
            self.queue.action(job['id'], 'resume')
            done = self.wait(job['id'])
        self.assertEqual(done['state'], 'failed')
        self.assertEqual(destination.read_bytes(), b'precious existing result')
        self.assertEqual(list(folder.glob('.export-*')), [])

    def test_symlinked_output_parent_is_rejected_without_writing_outside_folder(self):
        job = self.submit()
        outside = self.root / 'outside'
        outside.mkdir()
        (self.root / 'photopilot_out').symlink_to(outside, target_is_directory=True)
        self.queue.action(job['id'], 'resume')
        self.assertEqual(self.wait(job['id'])['state'], 'failed')
        self.assertEqual(list(outside.iterdir()), [])

    def test_http_submit_list_pagination_and_actions(self):
        http = self.http_server()
        status, created = self.request(http, 'POST', '/api/exports', self.payload)
        self.assertEqual(status, 200, created)
        jid = created['job']['id']
        status, listing = self.request(http, 'GET', '/api/exports')
        self.assertEqual(status, 200)
        self.assertNotIn('items', listing['jobs'][0])
        self.assertNotIn('recipe', listing['jobs'][0])
        status, detail = self.request(http, 'GET', f'/api/export?id={jid}&offset=1&limit=1')
        self.assertEqual(status, 200)
        self.assertEqual(detail['job']['items'], [])
        self.assertEqual(detail['job']['items_total'], 1)
        status, paused = self.request(http, 'POST', '/api/export_action', {'id': jid, 'action': 'pause'})
        self.assertEqual(status, 200)
        self.assertEqual(paused['job']['state'], 'paused')

    def test_http_pagination_rejects_invalid_bounds(self):
        jid = self.submit()['id']
        http = self.http_server()
        for suffix in ('offset=-1', 'limit=0', 'limit=101', 'limit=abc'):
            self.assertEqual(self.request(http, 'GET', f'/api/export?id={jid}&{suffix}')[0], 400)

    def test_http_reads_queue_summaries_without_copying_items(self):
        self.submit()
        with patch.object(self.queue, 'list', wraps=self.queue.list) as listing:
            status, response = self.request(self.http_server(), 'GET', '/api/exports')
        self.assertEqual(status, 200, response)
        listing.assert_called_once_with(summaries=True)

    def test_http_reads_one_bounded_page_without_slicing_it_twice(self):
        second = self.root / 'second.png'
        Image.new('RGB', (48, 32), (30, 40, 50)).save(second)
        self.payload['settings']['paths'].append(str(second))
        jid = self.submit()['id']
        with patch.object(self.queue, 'get', wraps=self.queue.get) as getter:
            status, response = self.request(self.http_server(), 'GET',
                                           f'/api/export?id={jid}&offset=1&limit=1')
        self.assertEqual(status, 200, response)
        getter.assert_called_once_with(jid, offset=1, limit=1)
        self.assertEqual(response['job']['items'][0]['path'], str(second))
        self.assertEqual(response['job']['items_total'], 2)
        self.assertEqual(response['job']['offset'], 1)

    def test_public_result_derives_bounded_thumbnails_and_download_url(self):
        jid = self.submit()['id']
        out = self.root / 'photopilot_out' / jid / '测试 & a_pp.png'
        public = server._public_export_job({'id': jid, 'items': [{
            'path': str(self.source), 'index': 0, 'state': 'succeeded', 'result': {
                'src': str(self.source), 'out': str(out), 'width': 4096, 'height': 3000,
                'before': 'https://evil.invalid/before', 'after': 'javascript:bad'}}]})
        result = public['items'][0]['result']
        self.assertTrue(result['before'].startswith('/api/thumb?p='))
        self.assertTrue(result['after'].startswith('/api/thumb?p='))
        self.assertTrue(result['after'].endswith('&s=640'))
        self.assertTrue(result['download'].startswith('/api/img?p='))
        self.assertNotIn('evil', str(result))

    def test_public_result_rejects_matching_suffix_outside_frozen_folder(self):
        jid = self.submit()['id']
        out = self.root / 'foreign' / 'photopilot_out' / jid / '测试 & a_pp.png'
        job = self.queue.get(jid)
        job['items'][0].update(state='succeeded', result={'out': str(out)})
        public = server._public_export_job(job)
        self.assertNotIn('result', public['items'][0])
        self.assertIn('结果', public['items'][0]['error'])
        self.assertNotIn('recipe', public)

    def test_public_result_rejects_symlinked_output_paths(self):
        jid = self.submit()['id']
        outside = self.root / 'outside'
        outside.mkdir()
        (self.root / 'photopilot_out').symlink_to(outside, target_is_directory=True)
        job = self.queue.get(jid)
        job['items'][0].update(state='succeeded', result={
            'out': str(self.root / 'photopilot_out' / jid / '测试 & a_pp.png')})
        public = server._public_export_job(job)
        self.assertNotIn('result', public['items'][0])
        self.assertIn('结果', public['items'][0]['error'])

    def test_public_result_rejects_symlinked_output_file(self):
        jid = self.submit()['id']
        out = self.root / 'photopilot_out' / jid / '测试 & a_pp.png'
        out.parent.mkdir(parents=True)
        out.symlink_to(self.source)
        job = self.queue.get(jid)
        job['items'][0].update(state='succeeded', result={'out': str(out)})
        public = server._public_export_job(job)
        self.assertNotIn('result', public['items'][0])
        self.assertIn('结果', public['items'][0]['error'])

    def test_public_result_rejects_symlinked_job_directory(self):
        jid = self.submit()['id']
        outside = self.root / 'outside'
        outside.mkdir()
        output_root = self.root / 'photopilot_out'
        output_root.mkdir()
        (output_root / jid).symlink_to(outside, target_is_directory=True)
        job = self.queue.get(jid)
        job['items'][0].update(state='succeeded', result={
            'out': str(output_root / jid / '测试 & a_pp.png')})
        public = server._public_export_job(job)
        self.assertNotIn('result', public['items'][0])
        self.assertIn('结果', public['items'][0]['error'])

    def test_public_result_rejects_replaced_frozen_folder_symlink(self):
        folder = self.root / 'frozen'
        folder.mkdir()
        self.payload['settings']['folder'] = str(folder)
        jid = self.submit()['id']
        outside = self.root / 'outside'
        outside.mkdir()
        folder.rmdir()
        folder.symlink_to(outside, target_is_directory=True)
        job = self.queue.get(jid)
        job['items'][0].update(state='succeeded', result={
            'out': str(folder / 'photopilot_out' / jid / '测试 & a_pp.png')})
        public = server._public_export_job(job)
        self.assertNotIn('result', public['items'][0])
        self.assertIn('结果', public['items'][0]['error'])

    def test_public_page_uses_frozen_names_for_same_named_sources(self):
        second_dir = self.root / 'second'
        second_dir.mkdir()
        second = second_dir / self.source.name
        Image.new('RGB', (48, 32), (30, 40, 50)).save(second)
        self.payload['settings']['paths'].append(str(second))
        jid = self.submit()['id']
        job = self.queue.get(jid, offset=1, limit=1)
        name = self.queue.get_recipe_field(jid, 'session', 'output_names', str(second))
        out = self.root / 'photopilot_out' / jid / name
        job['items'][0].update(state='succeeded', result={'out': str(out)})
        public = server._public_export_job(job)
        self.assertEqual(len(public['items']), 1)
        self.assertEqual(public['items'][0]['result']['out'], str(out))
        self.assertEqual(public['items_total'], 2)
        self.assertEqual(public['offset'], 1)

    def test_http_forget_completed_job_keeps_exported_files(self):
        job = self.submit()
        self.queue.action(job['id'], 'resume')
        done = self.wait(job['id'])
        destination = Path(done['items'][0]['result']['out'])
        before = destination.read_bytes()
        status, removed = self.request(self.http_server(), 'POST', '/api/export_action',
                                       {'id': job['id'], 'action': 'forget'})
        self.assertEqual(status, 200)
        self.assertEqual(removed['job']['state'], 'removed')
        self.assertEqual(destination.read_bytes(), before)

    def test_http_rejects_remote_origin(self):
        http = self.http_server()
        self.assertEqual(self.request(http, 'GET', '/api/exports',
                                     headers={'Origin': 'https://evil.invalid'})[0], 403)

    def test_oversized_export_body_rejected_before_body_read(self):
        http = self.http_server()
        status, _ = self.request(http, 'POST', '/api/exports',
                                headers={'Content-Length': str(2 * 1024 * 1024 + 1)})
        self.assertEqual(status, 413)


if __name__ == '__main__':
    unittest.main()
