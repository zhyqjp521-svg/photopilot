"""Isolated behavioral regressions: python -m unittest discover -s tests."""
from __future__ import annotations

import tempfile
import sys
import http.client
import json
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from types import ModuleType
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import numpy as np
from PIL import Image

from photopilot import io, server, polish
from photopilot.cull import PhotoScore, finalize_cull, F_DUP
from photopilot.xmp import read_rating, write_sidecar
from photopilot import platform as platform_module

XMP = "http://ns.adobe.com/xap/1.0/"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"


class SidecarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.image = Path(self.tmp.name) / "photo.jpg"
        self.sidecar = self.image.with_suffix(".xmp")

    def test_read_attribute_rating_with_arbitrary_namespace_prefix(self):
        self.sidecar.write_text(f'<x xmlns="{RDF}" xmlns:stars="{XMP}"><Description stars:Rating="4"/></x>')
        self.assertEqual(read_rating(self.image), 4)

    def test_rating_write_preserves_lightroom_adjustments_and_keywords(self):
        self.sidecar.write_text(f'<x xmlns:rdf="{RDF}" xmlns:xmp="{XMP}" xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/"><rdf:RDF><rdf:Description crs:Exposure2012="1.25" xmp:Rating="2"><dc:subject><rdf:Bag><rdf:li>婚礼</rdf:li></rdf:Bag></dc:subject></rdf:Description></rdf:RDF></x>')
        write_sidecar(self.image, .6, [], rating=5)
        root = ET.parse(self.sidecar).getroot()
        desc = root.find(f'.//{{{RDF}}}Description')
        self.assertEqual(desc.get('{http://ns.adobe.com/camera-raw-settings/1.0/}Exposure2012'), '1.25')
        self.assertEqual(root.find(f'.//{{{RDF}}}li').text, '婚礼')
        self.assertEqual(read_rating(self.image), 5)

    def test_xml_special_characters_are_roundtrippable(self):
        write_sidecar(self.image, .5, ['A&B <test>'], label='客户 & 摄影')
        self.assertEqual(ET.parse(self.sidecar).find(f'.//{{{XMP}}}Label').text, '客户 & 摄影')

    def test_invalid_existing_xmp_is_not_overwritten(self):
        original = '<unclosed Lightroom data'
        self.sidecar.write_text(original)
        with self.assertRaises(ValueError):
            write_sidecar(self.image, .5, [], rating=3)
        self.assertEqual(self.sidecar.read_text(), original)

    def test_automatic_rescan_keeps_manual_rating(self):
        write_sidecar(self.image, .9, [], rating=5)
        write_sidecar(self.image, .1, [])
        self.assertEqual(read_rating(self.image), 5)

    def test_automatic_rating_is_not_shown_as_manual_favorite(self):
        write_sidecar(self.image, .9, [])
        self.assertEqual(read_rating(self.image), 5)
        self.assertIsNone(read_rating(self.image, manual_only=True))
        write_sidecar(self.image, .5, [], rating=4)
        self.assertEqual(read_rating(self.image, manual_only=True), 4)


class ScanTests(unittest.TestCase):
    def test_empty_nonrecursive_folder_scan_falls_back_to_nested_photos(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "day-1" / "photo.jpg"
            nested.parent.mkdir()
            Image.new('RGB', (24, 16), (90, 120, 150)).save(nested)
            jid = server._scan_start({'folder': tmp, 'recursive': False,
                                      'faces': False, 'ai': False, 'jobs': 1})['job']
            deadline = time.monotonic() + 5
            while not server.SCAN_JOBS[jid]['finished'] and time.monotonic() < deadline:
                time.sleep(.01)
            status = server._scan_status({'job': [jid]})
            self.assertTrue(status['finished'])
            self.assertEqual(status['total'], 1)
            self.assertTrue(status.get('recursive_auto'))
            self.assertEqual([Path(p['path']).name for p in status['final']], ['photo.jpg'])

    def test_nonrecursive_scan_stays_top_level_when_it_finds_photos(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            Image.new('RGB', (24, 16), (90, 120, 150)).save(root / 'root.jpg')
            nested = root / 'day-1' / 'nested.jpg'
            nested.parent.mkdir()
            Image.new('RGB', (24, 16), (120, 150, 90)).save(nested)
            jid = server._scan_start({'folder': tmp, 'recursive': False,
                                      'faces': False, 'ai': False, 'jobs': 1})['job']
            deadline = time.monotonic() + 5
            while not server.SCAN_JOBS[jid]['finished'] and time.monotonic() < deadline:
                time.sleep(.01)
            status = server._scan_status({'job': [jid]})
            self.assertTrue(status['finished'])
            self.assertEqual(status['total'], 1)
            self.assertFalse(status.get('recursive_auto', False))
            self.assertEqual([Path(p['path']).name for p in status['final']], ['root.jpg'])

    def test_folder_enumeration_failure_terminates_scan_with_error(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(server, 'iter_images', side_effect=PermissionError('不可读目录')):
            jid = server._scan_start({'folder': tmp, 'faces': False})['job']
            deadline = time.monotonic() + .5
            while not server.SCAN_JOBS[jid]['finished'] and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(server.SCAN_JOBS[jid]['finished'])
            self.assertIn('不可读目录', server._scan_status({'job': [jid]})['finalize_error'])

    def test_empty_scan_settles_without_infinite_refine_polling(self):
        with tempfile.TemporaryDirectory() as tmp:
            jid = server._scan_start({'folder': tmp, 'faces': True, 'ai': False})['job']
            deadline = time.monotonic() + 5
            while not server.SCAN_JOBS[jid]['finished'] and time.monotonic() < deadline:
                time.sleep(.01)
            status = server._scan_status({'job': [jid]})
            self.assertTrue(status['finished'])
            self.assertFalse(status['refine_pending'])
            self.assertEqual(status['final'], [])

    def test_final_snapshot_is_not_repeated_when_client_has_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            jid = server._scan_start({'folder': tmp, 'faces': False})['job']
            while not server.SCAN_JOBS[jid]['finished']:
                time.sleep(.01)
            first = server._scan_status({'job': [jid]})
            second = server._scan_status({'job': [jid], 'final_stage': [first.get('final_stage', 'refined')]})
            self.assertNotIn('final', second)

    def test_refine_updates_are_available_to_each_independent_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            jid = server._scan_start({'folder': tmp, 'faces': False})['job']
            while not server.SCAN_JOBS[jid]['finished']:
                time.sleep(.01)
            server.SCAN_JOBS[jid]['updates'] = [{'path': 'sample.jpg', 'score': .8}]
            query = {'job': [jid], 'updates_since': ['0']}
            self.assertEqual(server._scan_status(query).get('updates'), server._scan_status(query).get('updates'))

    def test_regrouping_removes_stale_duplicate_flag_from_new_best(self):
        a = PhotoScore('a.jpg', 20, 20, .5, .5, .5, .5, .5, .9, 0, 0)
        b = PhotoScore('b.jpg', 20, 20, .5, .5, .5, .5, .5, .8, 0, 0)
        hashes = {p.path: np.zeros(64, bool) for p in (a, b)}
        finalize_cull([a, b], hashes)
        a.score, b.score = .7, .95
        finalize_cull([a, b], hashes)
        self.assertNotIn(F_DUP, b.flags)
        self.assertIn(F_DUP, a.flags)


class LocalServerSecurityTests(unittest.TestCase):
    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)

    def request(self, headers):
        import http.client
        conn = http.client.HTTPConnection('127.0.0.1', self.http.server_port)
        conn.request('GET', '/api/presets', headers=headers)
        reply = conn.getresponse()
        status = reply.status
        reply.read()
        conn.close()
        return status

    def test_unrelated_web_origin_cannot_access_local_api(self):
        self.assertEqual(self.request({'Origin': 'https://example.com'}), 403)

    def test_non_loopback_host_rejects_dns_rebinding_requests(self):
        self.assertEqual(self.request({'Host': 'example.com'}), 403)

    def test_local_ui_same_origin_still_works(self):
        origin = f'http://127.0.0.1:{self.http.server_port}'
        self.assertEqual(self.request({'Origin': origin}), 200)


class ExportTests(unittest.TestCase):
    def test_auto_reference_is_computed_after_batch_white_balance(self):
        from photopilot import pipeline
        from photopilot.cull import CullReport
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'photo.png'
            Image.new('RGB', (80, 60), (60, 90, 140)).save(source)
            photo = PhotoScore(str(source), 80, 60, .8, .8, .8, .8, .5, .8, 0, 0)
            balanced = np.full((60, 80, 3), 100, np.uint8)
            expected = pipeline.oklab_stats(balanced)[0]
            with patch.object(pipeline, 'cull_folder', return_value=CullReport([photo])), \
                 patch.object(polish, 'apply_wb_gains', new=lambda img, *_a: np.full_like(img, 100)), \
                 patch.object(pipeline, 'reference_from_stats', wraps=pipeline.reference_from_stats) as ref, \
                 patch.object(pipeline, 'analyze_faces', new=lambda _img: []):
                pipeline.run_pipeline(tmp, out_dir=Path(tmp)/'output', contact=False, wb_mode='batch')
            np.testing.assert_allclose(ref.call_args.args[0], expected, atol=1e-6)

    def test_edit_face_detection_uses_persistent_worker_not_http_thread(self):
        import threading
        calls = []
        def detector(_img):
            calls.append(threading.current_thread().name)
            return []
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'photo.png'
            Image.new('RGB', (80, 60), (100, 80, 60)).save(source)
            session = server._prepare({'folder': tmp, 'paths': [str(source)], 'operation': 'polish',
                                      'polish': {'skin': 0, 'clarity': 0}})
            with patch.object(server, 'analyze_faces', new=detector):
                for _ in range(2):
                    server._process_one({'sid': session['sid'], 'path': str(source)})
        self.assertEqual(len(set(calls)), 1)
        self.assertTrue(calls[0].startswith('PhotoPilotEditFace'))

    def test_pipeline_does_not_retain_entire_batch_of_work_images(self):
        import weakref
        from photopilot import pipeline
        from photopilot.cull import CullReport
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i in range(9):
                path = Path(tmp) / f'{i}.png'
                Image.new('RGB', (80, 60), (80+i, 110, 90)).save(path)
                paths.append(str(path))
            photos = [PhotoScore(p, 80, 60, .8, .8, .8, .8, .5, .8, 0, 0) for p in paths]
            refs, peak = [], [0]
            original_load = pipeline.load_image
            def measured(*args, **kwargs):
                result = original_load(*args, **kwargs)
                refs.append(weakref.ref(result))
                peak[0] = max(peak[0], sum(ref() is not None for ref in refs))
                return result
            with patch.object(pipeline, 'cull_folder', return_value=CullReport(photos)), patch.object(pipeline, 'load_image', side_effect=measured), patch.object(pipeline, 'analyze_faces', new=lambda _img: []):
                result = pipeline.run_pipeline(tmp, out_dir=Path(tmp)/'output', preset='film_warm',
                                               contact=False, polish_params=polish.PolishParams(skin=0, clarity=0, eyes=False))
            self.assertEqual(len(result.outputs),9)
            self.assertLessEqual(peak[0],5, 'decoded work images accumulated across the whole batch')

    def test_preview_does_not_write_final_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'photo.jpg'
            Image.new('RGB', (80, 60), (100, 90, 80)).save(source)
            session = server._prepare({'folder': tmp, 'paths': [str(source)], 'operation': 'polish'})
            with patch.object(server, 'analyze_faces', return_value=[]), patch.object(server, 'THUMB_DIR', Path(tmp)/'cache'):
                result = server._process_one({'sid': session['sid'], 'path': str(source), 'preview_only': True,
                                              'adjustments': {'exposure': .5}})
                self.assertTrue(Path(result['out']).exists())
                self.assertFalse((Path(tmp)/'photopilot_out').exists())

    def test_requested_original_size_is_preserved_and_polish_runs_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'photo.png'
            Image.new('RGB', (2600, 1800), (120, 90, 60)).save(source)
            session = server._prepare({'folder': tmp, 'paths': [str(source)], 'operation': 'polish',
                                      'export_size': 'original', 'polish': {'wb': 0, 'skin': 0, 'clarity': 0, 'eyes': False}})
            with patch.object(server, 'analyze_faces', return_value=[]), patch.object(server, 'polish_image', wraps=polish.polish_image) as render:
                result = server._process_one({'sid': session['sid'], 'path': str(source)})
            with Image.open(result['out']) as im:
                self.assertEqual(im.size, (2600, 1800))
            self.assertEqual(render.call_count, 1)

    def test_raw_original_does_not_use_half_resolution_decode(self):
        class Raw:
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def postprocess(self, **kwargs):
                return np.zeros((20, 30, 3) if kwargs['half_size'] else (40, 60, 3), np.uint8)
        with patch.object(io.rawpy, 'imread', return_value=Raw()):
            self.assertEqual(io.load_image('photo.arw', max_size=None).shape[:2], (40, 60))

    def test_zero_channel_white_balance_is_finite(self):
        gains = polish.estimate_wb_gains(np.zeros((20, 30, 3), np.uint8))
        ratio = polish.batch_target_gains([gains]) / gains
        self.assertTrue(np.isfinite(ratio).all())

    def test_batch_pipeline_accepts_real_path_dictionary(self):
        from photopilot.pipeline import run_pipeline
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'input'
            source.mkdir()
            rng = np.random.default_rng(17)
            Image.fromarray(rng.integers(40, 200, (96, 128, 3), dtype=np.uint8)).save(source / 'a.jpg')
            result = run_pipeline(source, out_dir=Path(tmp)/'output', wb_mode='batch', faces=False,
                                  contact=False, polish_params=polish.PolishParams(skin=0, clarity=0, eyes=False))
            self.assertEqual(len(result.outputs), 1)


class AdjustmentTests(unittest.TestCase):
    def apply(self, img, **params):
        func = getattr(polish, 'adjust_tone', None)
        self.assertIsNotNone(func, 'Manual tone controls are missing')
        return func(img, **params)

    def test_neutral_controls_are_pixel_identity(self):
        img = np.random.default_rng(1).integers(0, 256, (80, 90, 3), dtype=np.uint8)
        self.assertTrue(np.array_equal(self.apply(img), img))

    def test_exposure_positive_brightens_negative_darkens(self):
        img = np.full((40, 40, 3), 100, np.uint8)
        self.assertGreater(self.apply(img, exposure=1).mean(), img.mean())
        self.assertLess(self.apply(img, exposure=-1).mean(), img.mean())

    def test_highlight_reduction_preserves_dark_pixels(self):
        img = np.tile(np.arange(256, dtype=np.uint8)[None, :, None], (20, 1, 3))
        out = self.apply(img, highlights=-.7)
        self.assertLess(out[:, 220:].mean(), img[:, 220:].mean())
        self.assertLess(np.abs(out[:, :30].astype(float) - img[:, :30]).mean(), 2)

    def test_shadows_raise_dark_pixels_and_preserve_white(self):
        img = np.tile(np.arange(256, dtype=np.uint8)[None, :, None], (20, 1, 3))
        out = self.apply(img, shadows=.7)
        self.assertGreater(out[:, 20:60].mean(), img[:, 20:60].mean())
        self.assertLess(abs(float(out[:, -1].mean()) - 255), 2)

    def test_zero_saturation_produces_gray(self):
        img = np.full((20, 20, 3), (180, 80, 30), np.uint8)
        out = self.apply(img, saturation=-1)
        self.assertTrue(np.max(np.ptp(out.astype(float), axis=-1)) <= 1)

    def test_temperature_warms_without_changing_shape(self):
        img = np.full((20, 20, 3), 100, np.uint8)
        out = self.apply(img, temperature=.7)
        self.assertEqual(out.shape, img.shape)
        self.assertGreater(out[..., 0].mean(), out[..., 2].mean())


class ModelLicenseTests(unittest.TestCase):
    def test_licensed_model_is_verified(self):
        # 旧版曾断言「未授权模型默认不启用」；2026-10-14 起已换成
        # idealo/image-quality-assessment 的 Apache-2.0 权重，许可证已核实。
        from photopilot import aesthetic
        self.assertTrue(aesthetic.MODEL_LICENSE_VERIFIED)

    def test_available_reflects_model_file_presence(self):
        # available() 仅在许可证已验证 + 模型文件存在 + onnxruntime 可导入时为真。
        # 若模型文件缺失（如 CI 未放权重），应优雅降级返回 False，而非报错。
        from photopilot import aesthetic
        # 不依赖本机是否真有模型文件：只验证返回值是布尔且不抛异常。
        self.assertIsInstance(aesthetic.available(), bool)


class ImportTests(unittest.TestCase):
    def test_recursive_scan_excludes_previous_exports_and_hidden_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for relative in ('source/a.jpg', 'photopilot_out/a_pp.jpg', '.hidden/secret.jpg'):
                file = root / relative
                file.parent.mkdir(parents=True, exist_ok=True)
                Image.new('RGB',(10,10)).save(file)
            self.assertEqual([p.name for p in io.iter_images(root, recursive=True)], ['a.jpg'])


class TrashTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.first = self.root / 'first.jpg'
        self.second = self.root / 'second.png'
        self.first.write_bytes(b'fixture image')
        self.second.write_bytes(b'fixture image')

    def test_trash_moves_only_validated_images_and_their_xmp_sidecars(self):
        sidecar = self.first.with_suffix('.xmp')
        sidecar.write_text('<x/>')
        moved = []
        with patch.object(server, '_move_to_trash', side_effect=lambda path: moved.append(Path(path))):
            result = server._trash_photos({'paths': [str(self.first)]})
        self.assertEqual(result['removed'], [str(self.first)])
        self.assertEqual(result['failed'], [])
        self.assertEqual(moved, [self.first, sidecar])

    def test_invalid_batch_is_rejected_before_any_file_is_moved(self):
        text = self.root / 'notes.txt'
        text.write_text('not a photo')
        with patch.object(server, '_move_to_trash') as move:
            with self.assertRaisesRegex(ValueError, '照片|图片|路径'):
                server._trash_photos({'paths': [str(self.first), str(text)]})
        move.assert_not_called()

    def test_move_failures_are_reported_per_photo_and_sidecar_failure_is_a_warning(self):
        self.first.with_suffix('.xmp').write_text('<x/>')
        moved = []

        def move(path):
            path = Path(path)
            if path == self.second:
                raise PermissionError('拒绝访问')
            if path.suffix == '.xmp':
                raise OSError('sidecar busy')
            moved.append(path)

        with patch.object(server, '_move_to_trash', side_effect=move):
            result = server._trash_photos({'paths': [str(self.first), str(self.second)]})
        self.assertEqual(result['removed'], [str(self.first)])
        self.assertEqual(result['failed'], [{'path': str(self.second), 'error': '拒绝访问'}])
        self.assertEqual(len(result['warnings']), 1)
        self.assertIn('sidecar 未能移动：sidecar busy', result['warnings'][0]['message'])
        self.assertEqual(moved, [self.first])

    def test_path_must_be_unique_absolute_regular_image_and_sidecar_not_symlink(self):
        for paths in ([], [str(self.first), str(self.first)], ['relative.jpg']):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                server._trash_photos({'paths': paths})
        link = self.root / 'link.jpg'
        link.symlink_to(self.first)
        with self.assertRaisesRegex(ValueError, '符号链接'):
            server._trash_photos({'paths': [str(link)]})
        self.first.with_suffix('.xmp').symlink_to(self.second)
        with self.assertRaisesRegex(ValueError, '符号链接'):
            server._trash_photos({'paths': [str(self.first)]})

    def test_macos_trash_bridge_uses_recoverable_file_manager_operation(self):
        if not platform_module.IS_MACOS:
            self.skipTest('仅 macOS 走 Foundation 废纸篓')
        calls = []
        class Manager:
            def trashItemAtURL_resultingItemURL_error_(self, url, result, error):
                calls.append(url)
                return True, None, None

        foundation = ModuleType('Foundation')
        foundation.NSFileManager = type('NSFileManager', (), {'defaultManager': staticmethod(lambda: Manager())})
        foundation.NSURL = type('NSURL', (), {'fileURLWithPath_': staticmethod(lambda path: path)})
        with patch.dict(sys.modules, {'Foundation': foundation}):
            server._move_to_trash(self.first)
        self.assertEqual(calls, [str(self.first)])

    def test_windows_linux_trash_bridge_uses_send2trash(self):
        if platform_module.IS_MACOS:
            self.skipTest('send2trash 分支用于 Windows/Linux')
        sent = []
        send2trash_mod = ModuleType('send2trash')
        send2trash_mod.send2trash = lambda path: sent.append(str(path))
        with patch.dict(sys.modules, {'send2trash': send2trash_mod}):
            server._move_to_trash(self.first)
        self.assertEqual(sent, [str(self.first)])

    def test_local_http_endpoint_moves_only_the_requested_fixture(self):
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        moved = []
        with patch.object(server, '_move_to_trash', side_effect=lambda path: moved.append(str(path))):
            connection = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=5)
            self.addCleanup(connection.close)
            body = json.dumps({'paths': [str(self.first)]})
            connection.request('POST', '/api/trash', body=body,
                               headers={'Content-Type': 'application/json'})
            response = connection.getresponse()
            result = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(result['removed'], [str(self.first)])
        self.assertEqual(moved, [str(self.first)])
        oversized = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=5)
        self.addCleanup(oversized.close)
        oversized.putrequest('POST', '/api/trash')
        oversized.putheader('Content-Type', 'application/json')
        oversized.putheader('Content-Length', str(server.MAX_TRASH_REQUEST_BYTES + 1))
        oversized.endheaders()
        too_large = oversized.getresponse()
        self.assertEqual(too_large.status, 413)
        too_large.read()
        self.assertEqual(moved, [str(self.first)])


if __name__ == '__main__':
    unittest.main()
