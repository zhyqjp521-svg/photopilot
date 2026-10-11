"""Live retouch contract using synthetic pixels and temporary outputs only."""
import copy
import tempfile
import unittest
import urllib.parse
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from photopilot import polish, server
from photopilot.face_analysis import FaceInfo


class LiveRetouchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'synthetic.png'
        rng = np.random.default_rng(73)
        self.image = np.clip(np.array([170, 120, 95]) +
                             rng.integers(-25, 26, (100, 120, 3)), 0, 255).astype(np.uint8)
        self.image[40, 55] = 20
        Image.fromarray(self.image).save(self.source)
        self.faces = [FaceInfo((20, 15, 70, 70))]
        for mocked in (patch.object(server, '_SESSIONS', {}),
                       patch.object(server, 'THUMB_DIR', self.root / 'cache'),
                       patch.object(server, 'analyze_faces', side_effect=lambda image: self.faces)):
            mocked.start()
            self.addCleanup(mocked.stop)
        prepared = server._prepare({'folder': str(self.root), 'paths': [str(self.source)],
                                    'operation': 'polish', 'export_size': 'original',
                                    'polish': {'wb': 0, 'skin': 0, 'clarity': 0, 'eyes': False}})
        self.sid = prepared['sid']
        self.session = server._SESSIONS[self.sid]

    def render(self, **values):
        return server._process_one({'sid': self.sid, 'path': str(self.source), **values})

    def pixels(self, result):
        with Image.open(result['out']) as image:
            return np.asarray(image).copy()

    def mask(self, result):
        self.assertIn('region_mask', result)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(result['region_mask']).query)
        path = Path(query['p'][0])
        self.assertEqual(path.suffix, '.png')
        self.assertEqual(path.parent, self.root / 'cache' / 'edits' / self.sid)
        with Image.open(path) as image:
            self.assertEqual(image.mode, 'RGBA')
            self.assertEqual(image.size, (result['width'], result['height']))
            return np.asarray(image).copy()

    def test_retouch_overrides_render_real_pixels_without_changing_session(self):
        original = copy.deepcopy(self.session)
        original_params = self.session['params']
        for control in ({'skin': 1, 'retain': 0}, {'clarity': 1},
                        {'face_repair': 1}, {'blemish': 1},
                        {'skin': 1, 'retain': 1}):
            with self.subTest(control=control):
                expected = polish.polish_image(self.image, replace(self.session['params'], **control),
                                               faces=self.faces)
                result = self.render(retouch_adjustments=control)
                np.testing.assert_array_equal(self.pixels(result), expected)
                if control.get('retain') != 1:
                    self.assertTrue(np.any(expected != self.image))
                self.assertEqual(self.session, original)
                self.assertIs(self.session['params'], original_params)
        retained = self.pixels(self.render(retouch_adjustments={'skin': 1, 'retain': 1}))
        smoothed = self.pixels(self.render(retouch_adjustments={'skin': 1, 'retain': 0}))
        self.assertTrue(np.any(retained != smoothed))

    def test_local_region_override_limits_real_render(self):
        self.faces = []
        result = self.render(retouch_adjustments={'clarity': 1, 'local_region': 'face'})
        np.testing.assert_array_equal(self.pixels(result), self.image)
        self.assertTrue(result['warnings'])

    def test_tone_and_retouch_overrides_can_be_combined(self):
        controls = {'skin': .8, 'retain': .1, 'local_region': 'skin'}
        tone = dict(exposure=.2, contrast=.1, highlights=-.1,
                    shadows=.2, temperature=.1, saturation=.2)
        expected = polish.polish_image(self.image, replace(self.session['params'], **controls, **tone),
                                       faces=self.faces)
        result = self.render(retouch_adjustments=controls, adjustments=tone)
        np.testing.assert_array_equal(self.pixels(result), expected)

    def test_invalid_retouch_inputs_are_rejected_before_decode(self):
        invalid = [None, [], 'face', 0, {'unknown': .5}, {'wb': .5}, {'eyes': True}]
        invalid += [{'local_region': value} for value in
                    (None, [], {}, 1, True, 'global', '全图', 'unknown')]
        for adjustment in invalid:
            with self.subTest(adjustment=adjustment), patch.object(server, 'load_image') as decode:
                with self.assertRaises(ValueError):
                    self.render(retouch_adjustments=adjustment)
                decode.assert_not_called()

    def test_invalid_retouch_scalar_values_are_rejected_before_decode(self):
        invalid = [None, True, False, '0.5', -.01, 1.01, float('nan'), float('inf'),
                   float('-inf'), [], {}]
        for key in ('skin', 'retain', 'clarity', 'face_repair', 'blemish'):
            for value in invalid:
                with self.subTest(key=key, value=value), patch.object(server, 'load_image') as decode:
                    with self.assertRaises(ValueError):
                        self.render(retouch_adjustments={key: value})
                    decode.assert_not_called()

    def test_invalid_mask_flags_are_rejected_before_decode(self):
        for flag in (None, 0, 1, 'true', [], {}):
            with self.subTest(flag=flag), patch.object(server, 'load_image') as decode:
                with self.assertRaises(ValueError):
                    self.render(show_mask=flag)
                decode.assert_not_called()

    def test_invalid_tone_structure_and_values_are_rejected_before_decode(self):
        for adjustments in (None, [], {'unknown': 1}, {'exposure': 4}, {'contrast': float('nan')}):
            with self.subTest(adjustments=adjustments), patch.object(server, 'load_image') as decode:
                with self.assertRaises(ValueError):
                    self.render(adjustments=adjustments)
                decode.assert_not_called()

    def test_region_masks_reuses_computed_local_mask_and_eye_detection(self):
        masks = []
        with patch.object(polish, '_detect_eye_regions', return_value=[(35, 35, 15, 10)]) as detect:
            expected = polish._local_mask(self.image, self.faces, 'eyes', eye_boxes=[(35, 35, 15, 10)])
            rendered = polish.polish_image(self.image, replace(self.session['params'], local_region='eyes'),
                                            faces=self.faces, region_masks=masks)
        self.assertEqual(detect.call_count, 1)
        self.assertEqual(len(masks), 1)
        np.testing.assert_array_equal(masks[0], expected)
        self.assertEqual(rendered.shape, self.image.shape)

    def test_preview_mask_has_correct_alpha_color_and_immutable_url(self):
        result = self.render(preview_only=True, show_mask=True,
                             retouch_adjustments={'local_region': 'face'})
        mask = self.mask(result)
        expected = polish._local_mask(self.image, self.faces, 'face')
        np.testing.assert_array_equal(mask[..., 3], (expected * 100).astype(np.uint8))
        np.testing.assert_array_equal(mask[..., :3], np.broadcast_to([0, 122, 255], mask[..., :3].shape))
        second = self.render(preview_only=True, show_mask=True)
        self.assertNotEqual(result['region_mask'], second['region_mask'])

    def test_preview_mask_matches_resized_render_dimensions(self):
        Image.new('RGB', (1400, 900), (120, 100, 80)).save(self.source)
        self.faces = []
        result = self.render(preview_only=True, preview_size=640, show_mask=True)
        mask = self.mask(result)
        self.assertEqual(mask.shape[:2], self.pixels(result).shape[:2])
        self.assertEqual(result['width'], 640)
        self.assertTrue(np.all(mask[..., 3] == 100))

    def test_missing_local_region_produces_fully_transparent_mask(self):
        self.faces = []
        for region in ('face', 'skin', 'eyes'):
            with self.subTest(region=region):
                result = self.render(preview_only=True, show_mask=True,
                                     retouch_adjustments={'local_region': region})
                self.assertFalse(self.mask(result)[..., 3].any())

    def test_mask_toggle_does_not_change_preview_pixels(self):
        controls = {'clarity': .7, 'skin': .6, 'local_region': 'face'}
        before = self.render(preview_only=True, retouch_adjustments=controls)
        after = self.render(preview_only=True, show_mask=True, retouch_adjustments=controls)
        self.assertNotIn('region_mask', before)
        self.mask(after)
        np.testing.assert_array_equal(self.pixels(before), self.pixels(after))

    def test_export_ignores_mask_flag_and_keeps_rendered_pixels(self):
        before = self.render(retouch_adjustments={'clarity': .7})
        pixels = self.pixels(before)
        after = self.render(show_mask=True, retouch_adjustments={'clarity': .7})
        self.assertNotIn('region_mask', after)
        self.assertFalse((self.root / 'cache' / 'edits').exists())
        np.testing.assert_array_equal(pixels, self.pixels(after))

    def test_color_only_does_not_run_retouch_or_generate_mask(self):
        self.session.update(do_polish=False, do_color=True,
                            reference=server.reference_from_preset('film_warm'))
        before = self.render(preview_only=True)
        with patch.object(server, 'polish_image', side_effect=AssertionError('color ran retouch')):
            after = self.render(preview_only=True, show_mask=True,
                                retouch_adjustments={'clarity': 1, 'face_repair': 1})
        self.assertNotIn('region_mask', after)
        self.assertFalse(list((self.root / 'cache').rglob('*.png')))
        np.testing.assert_array_equal(self.pixels(before), self.pixels(after))
