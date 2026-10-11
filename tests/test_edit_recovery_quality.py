"""Real pixel and durable-recovery regressions; never access user photos."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from photopilot.export_queue import ExportQueue
from photopilot.polish import PolishParams, auto_tone, polish_image


class AutoToneQualityTests(unittest.TestCase):
    def test_lifting_dark_scene_retains_unclipped_highlight_levels(self):
        image = np.full((100, 100, 3), 40, np.uint8)
        image[:5] = 180
        image[5:10] = 200
        original = image.copy()
        out = auto_tone(image)
        self.assertGreater(int(out[50, 50, 0]), 40)
        self.assertLess(int(out[0, 0, 0]), int(out[5, 0, 0]))
        self.assertLess(int(out[5, 0, 0]), 255)
        np.testing.assert_array_equal(image, original)

    def test_dark_and_bright_neutral_scenes_move_toward_middle(self):
        for value in (35, 220):
            with self.subTest(value=value):
                image = np.full((64, 64, 3), value, np.uint8)
                out = auto_tone(image)
                self.assertLess(abs(float(out.mean()) - 118), abs(value - 118))
                np.testing.assert_array_equal(out[..., 0], out[..., 1])
                np.testing.assert_array_equal(out[..., 1], out[..., 2])

    def test_black_white_and_flat_inputs_are_finite_and_do_not_invent_detail(self):
        for value in (0, 118, 255):
            with self.subTest(value=value):
                image = np.full((16, 32, 3), value, np.uint8)
                out = auto_tone(image)
                self.assertEqual(out.dtype, np.uint8)
                self.assertTrue(np.isfinite(out).all())
                self.assertEqual(int(out.max()), int(out.min()))
                if value == 0:
                    self.assertEqual(int(out.max()), 0)

    def test_invalid_auto_tone_strength_rejected_before_pixel_processing(self):
        image = np.full((16, 16, 3), 100, np.uint8)
        for strength in (float('nan'), float('inf'), -.1, 1.1):
            with self.subTest(strength=strength), self.assertRaises(ValueError):
                auto_tone(image, strength)

    def test_tone_recipe_replay_is_deterministic_and_does_not_change_original(self):
        image = np.repeat(np.arange(256, dtype=np.uint8)[None, :, None], 3, axis=2)
        image = np.repeat(image, 32, axis=0)
        original = image.copy()
        settings = dict(wb=0, skin=0, clarity=0, eyes=False, global_skin=False,
                        auto_tone=True, exposure=.2, highlights=-.3, shadows=.15)
        preview = polish_image(image, PolishParams(**settings), faces=[])
        exported = polish_image(image, PolishParams(**settings), faces=[])
        np.testing.assert_array_equal(preview, exported)
        np.testing.assert_array_equal(image, original)

    def test_real_preview_matches_frozen_export_pixels_at_equal_resolution(self):
        from photopilot import server
        from photopilot.export_recipe import freeze_session
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            image = np.full((128, 192, 3), 40, np.uint8)
            image[:8] = 180
            image[8:16] = 200
            source = root / 'dark-highlights.png'
            Image.fromarray(image).save(source)
            original_bytes = source.read_bytes()
            settings = {'folder': str(root), 'paths': [str(source)],
                        'operation': 'polish', 'export_size': 'original',
                        'polish': {'wb': 0, 'skin': 0, 'clarity': 0, 'eyes': False,
                                   'auto_tone': True, 'exposure': .1, 'shadows': .1}}
            session = server._prepare(settings, retain_session=False)['session']
            recipe = {'session': freeze_session(session),
                      'sources': {str(source): server._source_fingerprint(source)}}
            captured = []
            save = server.save_image

            def capture(pixels, destination, **kwargs):
                captured.append(pixels.copy())
                return save(pixels, destination, **kwargs)

            with patch.object(server, 'THUMB_DIR', root / 'cache'), \
                    patch.object(server, 'analyze_faces', return_value=[]), \
                    patch.object(server, '_thumb_url', return_value='/fixture-before'), \
                    patch.object(server, 'save_image', side_effect=capture):
                preview = server._render_session(session, {'sid': 'preview', 'path': str(source),
                                                          'preview_only': True})
                exported = server._render_export({'id': 'a' * 32, 'recipe': recipe},
                                                 {'path': str(source), 'index': 0})
            self.assertEqual(len(captured), 2)
            np.testing.assert_array_equal(captured[0], captured[1])
            self.assertTrue(Path(preview['out']).is_file())
            self.assertTrue(Path(exported['out']).is_file())
            self.assertNotEqual(exported['out'], str(source))
            self.assertEqual(source.read_bytes(), original_bytes)


class ExportRecoveryQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.checkpoint = self.root / 'exports.json'
        self.sources = [self.root / 'one.jpg', self.root / 'two.jpg']
        for path in self.sources:
            path.write_bytes(b'original pixels')
        self.queues = []
        self.addCleanup(lambda: [queue.shutdown(2) for queue in self.queues])
        self.rendered = []

    def render(self, job, item):
        source = Path(item['path'])
        self.rendered.append(source.name)
        output = self.root / ('export-' + source.name)
        # Independent output location; do not overwrite even in a retry.
        with output.open('xb') as stream:
            stream.write(source.read_bytes())
        return {'src': str(source), 'out': str(output)}

    def queue(self, autostart=False):
        queue = ExportQueue(self.checkpoint, self.render, autostart=autostart)
        self.queues.append(queue)
        return queue

    def terminal(self, queue, jid):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = queue.get(jid)
            if job['state'] in {'failed', 'completed'}:
                return job
            time.sleep(.005)
        self.fail('export did not reach a terminal state')

    def test_missing_disk_file_retries_after_restore_without_rerendering_success(self):
        queue = self.queue()
        jid = queue.submit({'tone': {'exposure': .2}}, list(map(str, self.sources)), 'disk')['id']
        disconnected = self.root / 'disconnected.jpg'
        self.sources[1].rename(disconnected)
        queue.action(jid, 'resume')
        failed = self.terminal(queue, jid)
        self.assertEqual(failed['counts']['succeeded'], 1)
        self.assertEqual(failed['counts']['failed'], 1)
        completed_bytes = (self.root / 'export-one.jpg').read_bytes()
        queue.shutdown()
        disconnected.rename(self.sources[1])
        recovered = self.queue(autostart=True)
        self.assertEqual(recovered.get(jid)['state'], 'failed')
        self.assertEqual(self.rendered, ['one.jpg'])
        recovered.action(jid, 'retry')
        self.assertEqual(self.terminal(recovered, jid)['state'], 'completed')
        self.assertEqual(self.rendered, ['one.jpg', 'two.jpg'])
        self.assertEqual((self.root / 'export-one.jpg').read_bytes(), completed_bytes)
        for source in self.sources:
            self.assertEqual(source.read_bytes(), b'original pixels')

    def test_changed_input_is_not_silently_accepted_by_retry(self):
        queue = self.queue()
        jid = queue.submit({}, [str(self.sources[0])], 'changed')['id']
        self.sources[0].write_bytes(b'new image contents')
        queue.action(jid, 'resume')
        self.assertEqual(self.terminal(queue, jid)['state'], 'failed')
        queue.action(jid, 'retry')
        failed = self.terminal(queue, jid)
        self.assertIn('source changed', failed['items'][0]['error'])
        self.assertEqual(self.rendered, [])
        self.assertFalse((self.root / 'export-one.jpg').exists())

    def test_queued_restart_waits_for_explicit_resume(self):
        queue = self.queue()
        jid = queue.submit({}, [str(self.sources[0])], 'restart')['id']
        queue.shutdown()
        restored = self.queue(autostart=True)
        self.assertEqual(restored.get(jid)['state'], 'paused')
        self.assertEqual(self.rendered, [])
        restored.action(jid, 'resume')
        self.assertEqual(self.terminal(restored, jid)['state'], 'completed')
        self.assertEqual(self.sources[0].read_bytes(), b'original pixels')


if __name__ == '__main__':
    unittest.main()
