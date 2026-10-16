"""Project checkpoint behavior, including the real local HTTP boundary."""
from __future__ import annotations

import copy
import contextlib
import http.client
import importlib
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from photopilot import server
from photopilot.xmp import write_sidecar


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('photopilot.project'),
                             'ProjectStore checkpoint backend is missing')
        self.project = importlib.import_module('photopilot.project')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.original = self.root / 'a & b.jpg'
        self.original.write_bytes(b'original photo')
        self.path = self.root / 'user-data' / 'project.json'
        self.store = self.project.ProjectStore(self.path)
        self.state = {'version': 1, 'folder': str(self.root), 'photos': [
            {'path': str(self.original), 'width': 120, 'height': 80,
             'score': .75, 'sharpness': .8, 'exposure': .7, 'contrast': .6,
             'colorfulness': .5, 'face_quality': .5, 'faces': 0, 'blinks': 0,
             'flags': [], 'rating': 5, 'thumb': 'https://evil.invalid/image',
             'out': 'https://evil.invalid/result'}],
            'selected': [str(self.original)], 'target': str(self.original),
            'selection_touched': True, 'settings': {'recipe': {'exposure': .5},
                'view': {'sort': 'score'}}, 'drafts': {str(self.original): [
                    {'exposure': 0}, {'exposure': .5}]}, 'scan_incomplete': True}

    def test_absent_checkpoint_returns_null(self):
        self.assertEqual(self.store.load(), {'state': None})

    def test_roundtrip_across_instances_rebuilds_urls_and_safe_defaults(self):
        before = self.original.read_bytes()
        result = self.store.save(self.state)
        self.assertTrue(result['ok'])
        self.assertIsInstance(result['saved_at'], (float, int))
        loaded = self.project.ProjectStore(self.path).load()
        self.assertEqual(loaded['missing'], [])
        self.assertEqual(loaded['changed'], [])
        for key in ('folder', 'selected', 'target', 'selection_touched',
                    'settings', 'drafts', 'scan_incomplete'):
            self.assertEqual(loaded['state'][key], self.state[key])
        photo = loaded['state']['photos'][0]
        self.assertTrue(photo['thumb'].startswith('/api/thumb?p='))
        self.assertIn('%26', photo['thumb'])
        self.assertNotIn('out', photo)
        self.assertEqual(photo['group'], -1)
        self.assertNotIn('_fingerprint', photo)
        self.assertEqual(self.original.read_bytes(), before)
        self.assertEqual(json.loads(self.path.read_text())['photos'][0]['_fingerprint'],
                         {'size': self.original.stat().st_size,
                          'mtime_ns': self.original.stat().st_mtime_ns})

    def test_empty_state_gets_schema_defaults(self):
        self.store.save({})
        self.assertEqual(self.store.load()['state'], {
            'version': 1, 'folder': '', 'photos': [], 'selected': [], 'target': None,
            'selection_touched': False, 'settings': {}, 'drafts': {},
            'scan_incomplete': False})

    def test_default_data_location_and_environment_override(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(Path, 'home', return_value=self.root):
            self.assertEqual(self.project.ProjectStore().path,
                             self.root / 'Library/Application Support/PhotoPilot/project.json')
        with patch.dict(os.environ, {'PHOTOPILOT_PROJECT_PATH': str(self.path)}):
            self.assertEqual(self.project.ProjectStore().path, self.path)

    def test_corrupt_and_unsupported_checkpoints_are_preserved(self):
        self.path.parent.mkdir()
        for raw in (b'{broken', b'{"version": 2}', b'{"version":1,"photos":"bad"}'):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaisesRegex(ValueError, 'checkpoint|project|version|photos'):
                    self.store.load()
                self.assertEqual(self.path.read_bytes(), raw)

    def test_missing_originals_are_reported_without_dropping_records(self):
        self.store.save(self.state)
        self.original.unlink()
        loaded = self.store.load()
        self.assertEqual(loaded['missing'], [str(self.original)])
        self.assertEqual(len(loaded['state']['photos']), 1)
        self.assertEqual(loaded['state']['selected'], [str(self.original)])

    def test_modified_originals_are_reported_without_dropping_records(self):
        self.store.save(self.state)
        self.original.write_bytes(b'new original contents')
        loaded = self.store.load()
        self.assertEqual(loaded['changed'], [str(self.original)])
        self.assertEqual(len(loaded['state']['photos']), 1)

    def test_rating_is_refreshed_from_manual_xmp(self):
        self.store.save(self.state)
        self.assertEqual(self.store.load()['state']['photos'][0]['rating'], 0)
        write_sidecar(self.original, .75, [], rating=3)
        self.assertEqual(self.store.load()['state']['photos'][0]['rating'], 3)
        self.original.with_suffix('.xmp').unlink()
        write_sidecar(self.original, .9, [])
        self.assertEqual(self.store.load()['state']['photos'][0]['rating'], 0)

    def test_replace_failure_keeps_previous_checkpoint_and_cleans_temp(self):
        self.store.save(self.state)
        before = self.path.read_bytes()
        with patch.object(self.project.os, 'replace', side_effect=OSError('disk failure')):
            with self.assertRaisesRegex(OSError, 'disk failure'):
                self.store.save({})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_invalid_photo_and_selection_data_are_rejected(self):
        variants = [
            {'selected': ['/unknown.jpg']}, {'target': '/unknown.jpg'},
            {'selection_touched': 'yes'}, {'scan_incomplete': 1},
            {'photos': [{}]}, {'photos': [dict(self.state['photos'][0], path='https://evil.invalid/a')]},
            {'photos': [dict(self.state['photos'][0], width=0)]},
            {'photos': [dict(self.state['photos'][0], score=1.2)]},
            {'photos': [dict(self.state['photos'][0], rating=6)]},
            {'photos': [dict(self.state['photos'][0], faces=True)]},
            {'photos': [dict(self.state['photos'][0], flags='bad')]},
            {'photos': self.state['photos'] * 20001},
        ]
        for changes in variants:
            with self.subTest(changes=list(changes)):
                invalid = dict(self.state, **changes)
                with self.assertRaises(ValueError):
                    self.store.save(invalid)
        self.assertFalse(self.path.exists())

    def test_nonfinite_deep_prototype_and_oversized_json_are_rejected(self):
        nested = {}
        for _ in range(17):
            nested = {'nested': nested}
        for settings in ({'value': float('nan')}, {'value': float('inf')}, nested,
                         {'__proto__': {}}, {'nested': {'constructor': {}}},
                         {'huge': 'x' * (16 * 1024 * 1024)}, {'bad': object()}):
            with self.subTest(settings=str(settings)[:50]):
                with self.assertRaises(ValueError):
                    self.store.save(dict(self.state, settings=settings))
        with self.assertRaises(ValueError):
            self.store.save(dict(self.state, photos=[dict(self.state['photos'][0], score=float('nan'))]))

    def test_oversized_disk_checkpoint_is_preserved(self):
        self.path.parent.mkdir()
        self.path.write_bytes(b' ' * (self.project.MAX_PROJECT_BYTES + 1))
        with self.assertRaises(ValueError):
            self.store.load()
        self.assertEqual(self.path.stat().st_size, self.project.MAX_PROJECT_BYTES + 1)

    def test_large_library_and_windows_drive_paths_roundtrip(self):
        # 回归：2 万张以上的项目曾因 MAX_PHOTOS=20000 保存失败；
        # Windows 盘符路径（E:/…）在 POSIX 上曾被判为非绝对路径。
        count = 21460
        photos = [{'path': 'E:/Photos/wedding/dir%03d/IMG_%05d.CR3' % (i // 500, i),
                   'width': 6000, 'height': 4000, 'score': .83,
                   'sharpness': .7, 'exposure': .5, 'contrast': .5,
                   'colorfulness': .5, 'faces': 2, 'blinks': 0,
                   'group': i // 4, 'group_size': 4, 'group_rank': 1,
                   'group_best': i % 4 == 0, 'rating': 3, 'ai': 4.2,
                   'flags': ['pick']} for i in range(count)]
        state = {'version': 1, 'folder': 'E:/Photos/wedding', 'photos': photos,
                 'selected': [photos[0]['path']], 'target': photos[0]['path'],
                 'settings': {}, 'drafts': {}}
        result = self.store.save(state)
        self.assertTrue(result['ok'])
        loaded = self.store.load()
        self.assertEqual(len(loaded['state']['photos']), count)
        self.assertEqual(loaded['state']['folder'], 'E:/Photos/wedding')
        # 超限仍拒绝
        with self.assertRaises(ValueError):
            self.store.save(dict(state, photos=[
                dict(photos[0], path='/tmp/x.jpg') for _ in range(self.project.MAX_PHOTOS + 1)]))

    def test_concurrent_writes_always_leave_complete_valid_json(self):
        def save(index):
            state = copy.deepcopy(self.state)
            state['settings']['index'] = index
            self.store.save(state)
            self.assertEqual(self.store.load()['missing'], [])
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(save, range(24)))
        self.assertIn(json.loads(self.path.read_text())['settings']['index'], range(24))
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])


class ScanCheckpointIntegrationTests(unittest.TestCase):
    # Keep these real scan integration tests separate from checkpoint unit cases.
    def setUp(self):
        ProjectTests.setUp(self)
        # macOS reports cwd under /private/var even when mkdtemp returns /var.
        self.root = self.root.resolve()

    def _scan_and_checkpoint(self, payload):
        started = server._scan_start(dict(payload, faces=False, ai=False, jobs=1))
        job = server.SCAN_JOBS[started['job']]
        self.addCleanup(server.SCAN_JOBS.pop, started['job'], None)
        deadline = time.monotonic() + 5
        while not job['finished'] and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertTrue(job['finished'], 'real image scan did not finish')
        self.assertFalse(job.get('finalize_error'))
        photos = server._scan_status({'job': [started['job']]})['final']
        self.assertTrue(photos)
        for photo in photos:
            self.assertTrue(Path(photo['path']).is_absolute(), photo['path'])
        self.assertIn('folder', started)
        self.assertEqual(started['folder'], job['folder'])
        self.store.save({'folder': started['folder'], 'photos': photos,
                         'selected': [photos[0]['path']], 'target': photos[0]['path']})
        self.assertEqual(self.store.load()['missing'], [])
        return started, photos

    def _images(self):
        demo = self.root / 'demo'
        demo.mkdir()
        for filename in ('a.jpg', 'b.jpg'):
            Image.new('RGB', (96, 64), (140, 110, 90)).save(demo / filename)
        return demo

    def test_relative_folder_and_tilde_folder_scans_can_be_checkpointed(self):
        demo = self._images()
        with contextlib.chdir(self.root), patch.dict(os.environ, {'HOME': str(self.root)}), \
                patch.object(server, 'THUMB_DIR', self.root / 'cache'):
            for folder in ('./demo', '~/demo'):
                with self.subTest(folder=folder):
                    started, photos = self._scan_and_checkpoint({'folder': folder})
                    self.assertEqual(started['folder'], str(demo))
                    self.assertEqual(len(photos), 2)

    def test_relative_single_file_scan_keeps_explicit_file_mode(self):
        demo = self._images()
        with contextlib.chdir(self.root), patch.object(server, 'THUMB_DIR', self.root / 'cache'):
            started, photos = self._scan_and_checkpoint({'folder': './demo/a.jpg'})
        self.assertEqual(started['folder'], str(demo))
        self.assertEqual([p['path'] for p in photos], [str(demo / 'a.jpg')])

    def test_explicit_relative_paths_preserve_absolute_symlink_alias(self):
        demo = self._images()
        alias = self.root / 'alias'
        alias.symlink_to(demo, target_is_directory=True)
        with contextlib.chdir(self.root), patch.object(server, 'THUMB_DIR', self.root / 'cache'):
            started, photos = self._scan_and_checkpoint({
                'folder': '.', 'paths': ['./alias/a.jpg', str(alias / 'a.jpg')]})
        self.assertEqual(started['folder'], str(self.root))
        self.assertEqual([p['path'] for p in photos], [str(alias / 'a.jpg')])

    def test_import_response_matches_normalized_scan_folder(self):
        self._images()
        with contextlib.chdir(self.root), patch.object(server, 'THUMB_DIR', self.root / 'cache'), \
                patch.object(server, 'UPLOAD_ROOTS', [self.root / 'imports']):
            imported = server._import_paths({'paths': ['./demo/a.jpg'], 'faces': False, 'ai': False})
            job = server.SCAN_JOBS[imported['job']]
            self.addCleanup(server.SCAN_JOBS.pop, imported['job'], None)
            deadline = time.monotonic() + 5
            while not job['finished'] and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertTrue(job['finished'])
            self.assertIn('folder', imported)
            self.assertEqual(imported['folder'], job['folder'])
            self.assertEqual(imported['folder'], str(self.root / 'demo'))
            self.assertTrue(Path(imported['files'][0]).is_absolute())
            photos = server._scan_status({'job': [imported['job']]})['final']
            self.store.save({'folder': imported['folder'], 'photos': photos,
                             'selected': [photos[0]['path']]})
            self.assertEqual(self.store.load()['missing'], [])

    def test_multiroot_import_uses_first_selected_source_directory(self):
        demo = self._images()
        second = self.root / 'z-second'
        second.mkdir()
        Image.new('RGB', (96, 64), (140, 110, 90)).save(second / 'c.jpg')
        with contextlib.chdir(self.root), patch.object(server, 'THUMB_DIR', self.root / 'cache'), \
                patch.object(server, 'UPLOAD_ROOTS', [self.root / 'imports']):
            imported = server._import_paths({'paths': ['./z-second/c.jpg', './demo/a.jpg'],
                                             'faces': False, 'ai': False})
            job = server.SCAN_JOBS[imported['job']]
            self.addCleanup(server.SCAN_JOBS.pop, imported['job'], None)
            deadline = time.monotonic() + 5
            while not job['finished'] and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertTrue(job['finished'])
            self.assertEqual(imported.get('folder'), str(second))
            photos = server._scan_status({'job': [imported['job']]})['final']
            self.assertEqual({p['path'] for p in photos}, {str(second / 'c.jpg'), str(demo / 'a.jpg')})
            self.store.save({'folder': imported['folder'], 'photos': photos,
                             'selected': [photos[0]['path']]})
            self.assertEqual(self.store.load()['missing'], [])

    def test_mixed_file_folder_import_uses_first_selected_source_directory(self):
        demo = self._images()
        second = self.root / 'z-second'
        second.mkdir()
        Image.new('RGB', (96, 64), (140, 110, 90)).save(second / 'c.jpg')
        cases = [(['./demo/a.jpg', './z-second', './demo/a.jpg'], [demo, second]),
                 (['./z-second', './demo/a.jpg', './z-second'], [second, demo])]
        with contextlib.chdir(self.root), patch.object(server, 'THUMB_DIR', self.root / 'cache'), \
                patch.object(server, 'UPLOAD_ROOTS', [self.root / 'imports']):
            for paths, directories in cases:
                with self.subTest(paths=paths):
                    imported = server._import_paths({'paths': paths, 'faces': False, 'ai': False})
                    job = server.SCAN_JOBS[imported['job']]
                    self.addCleanup(server.SCAN_JOBS.pop, imported['job'], None)
                    deadline = time.monotonic() + 5
                    while not job['finished'] and time.monotonic() < deadline:
                        time.sleep(.005)
                    self.assertTrue(job['finished'])
                    self.assertEqual(imported['folder'], str(directories[0]))
                    self.assertEqual(job['folder'], str(directories[0]))
                    self.assertEqual(imported['scan_dirs'], [str(d) for d in directories])
                    self.assertEqual(set(imported['files']),
                                     {str(demo / 'a.jpg'), str(second / 'c.jpg')})
                    self.assertEqual(len(imported['files']), 2)
                    self.assertEqual(job['override_paths'], imported['files'])


class ProjectHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'project.json'
        self.env = patch.dict(os.environ, {'PHOTOPILOT_PROJECT_PATH': str(self.path)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.addCleanup(self.http.server_close)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.http.shutdown)

    def request(self, method, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=3)
        try:
            connection.request(method, '/api/project', body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_http_project_roundtrip_and_local_origin_security(self):
        code, data = self.request('GET')
        self.assertEqual((code, data), (200, {'state': None}))
        code, data = self.request('POST', json.dumps({'version': 1, 'settings': {'recipe': 'warm'}}))
        self.assertEqual(code, 200)
        self.assertTrue(data['ok'])
        self.assertTrue(self.path.exists())
        code, data = self.request('GET')
        self.assertEqual(data['state']['settings'], {'recipe': 'warm'})
        before = self.path.read_bytes()
        for headers in ({'Origin': 'https://evil.invalid'}, {'Host': 'evil.invalid'},
                        {'Origin': 'null'}):
            self.assertEqual(self.request('POST', '{}', headers)[0], 403)
        self.assertEqual(self.path.read_bytes(), before)

    def test_http_project_limits_and_invalid_lengths_return_json(self):
        self.assertEqual(self.request('POST', '', {'Content-Length': str(server.MAX_PROJECT_BYTES + 1)})[0], 413)
        for length in ('invalid', '-1'):
            self.assertEqual(self.request('POST', '', {'Content-Length': length})[0], 400)
        self.assertEqual(self.request('POST', '{bad')[0], 400)
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
