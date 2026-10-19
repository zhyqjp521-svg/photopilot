"""Local retouch regressions; synthetic images only, never user photos."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from photopilot import polish, server


class LocalRetouchTests(unittest.TestCase):
    def test_eyes_region_uses_cascade_when_landmarks_are_unavailable(self):
        class Cascade:
            def detectMultiScale(self, image, **kwargs):
                return np.array([[10, 12, 18, 10]])

        image = np.random.default_rng(7).integers(30, 200, (100, 120, 3), dtype=np.uint8)
        with patch.object(polish, '_eye_cascade', return_value=Cascade()):
            mask = polish._local_mask(image, [(20, 20, 60, 60)], 'eyes')
            self.assertGreater(float(mask[37, 39]), .5)
            self.assertEqual(float(mask[90, 110]), 0)
            params = polish.PolishParams(wb=0, skin=0, clarity=1, eyes=False, local_region='eyes')
            rendered = polish.polish_image(image, params, faces=[(20, 20, 60, 60)])
        self.assertTrue(np.any(rendered[32:42, 30:48] != image[32:42, 30:48]))
        np.testing.assert_array_equal(rendered[80:, 90:], image[80:, 90:])

    def test_preview_reports_missing_face_region_instead_of_silent_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'landscape.png'
            Image.new('RGB', (120, 80), (100, 90, 80)).save(source)
            session = server._prepare({'folder': tmp, 'paths': [str(source)], 'operation': 'polish',
                                       'polish': {'local_region': 'face', 'wb': 0, 'skin': 0}})
            with patch.object(server, 'analyze_faces', return_value=[]), patch.object(server, 'THUMB_DIR', Path(tmp)/'cache'):
                result = server._process_one({'sid': session['sid'], 'path': str(source), 'preview_only': True})
            self.assertTrue(any('未检测到人脸' in message for message in result.get('warnings', [])))
            self.assertTrue(Path(result['out']).exists())

    def test_no_eyes_detected_leaves_local_detail_unchanged_and_warns(self):
        class Cascade:
            def detectMultiScale(self, image, **kwargs):
                return []
        image = np.random.default_rng(8).integers(30, 200, (100, 120, 3), dtype=np.uint8)
        warnings = []
        params = polish.PolishParams(wb=0, skin=0, clarity=1, local_region='eyes')
        with patch.object(polish, '_eye_cascade', return_value=Cascade()):
            result = polish.polish_image(image, params, faces=[(20, 20, 60, 60)], warnings=warnings)
        np.testing.assert_array_equal(result, image)
        self.assertTrue(any('未找到可用的眼睛区域' in message for message in warnings))

    def test_local_eye_mask_and_brightening_share_one_detection(self):
        from unittest.mock import Mock
        cascade = Mock()
        cascade.detectMultiScale.return_value = np.array([[10, 12, 18, 10]])
        image = np.full((100, 120, 3), 100, np.uint8)
        with patch.object(polish, '_eye_cascade', return_value=cascade):
            polish.polish_image(image, polish.PolishParams(local_region='eyes'), faces=[(20, 20, 60, 60)])
        self.assertEqual(cascade.detectMultiScale.call_count, 1)

    def test_eye_fallback_ignores_face_boxes_outside_image(self):
        from unittest.mock import Mock
        cascade = Mock()
        cascade.detectMultiScale.return_value = []
        with patch.object(polish, '_eye_cascade', return_value=cascade):
            mask = polish._local_mask(np.full((100, 120, 3), 100, np.uint8), [(-20, 10, 15, 30)], 'eyes')
        self.assertFalse(mask.any())
        cascade.detectMultiScale.assert_not_called()

    def test_face_info_without_mesh_also_uses_eye_fallback(self):
        from photopilot.face_analysis import FaceInfo
        from unittest.mock import Mock
        cascade = Mock()
        cascade.detectMultiScale.return_value = np.array([[10, 12, 18, 10]])
        image = np.full((100, 120, 3), 100, np.uint8)
        with patch.object(polish, '_eye_cascade', return_value=cascade):
            mask = polish._local_mask(image, [FaceInfo((20, 20, 60, 60))], 'eyes')
        self.assertGreater(float(mask[37, 39]), .5)

    def test_partially_outside_face_and_eye_boxes_are_clipped(self):
        from unittest.mock import Mock
        cascade = Mock()
        cascade.detectMultiScale.return_value = np.array([[-3, -2, 30, 20], [80, 80, 10, 10]])
        image = np.full((30, 40, 3), 100, np.uint8)
        with patch.object(polish, '_eye_cascade', return_value=cascade):
            rois = polish._detect_eye_regions(image, [(-5, -3, 20, 20)])
            mask = polish._local_mask(image, [(-5, -3, 20, 20)], 'eyes')
        self.assertEqual(rois, [(0, 0, 15, 17)])
        self.assertEqual(mask.shape, (30, 40))
        self.assertGreater(float(mask.max()), .5)
        self.assertFalse(mask[17:, :].any())
        self.assertFalse(mask[:, 15:].any())


class BrushMaskTests(unittest.TestCase):
    """局部画笔掩码：_local_mask brush 分支 + polish_image 局部性 + server 解码。"""

    def setUp(self):
        rng = np.random.default_rng(11)
        self.image = rng.integers(30, 220, (120, 160, 3), dtype=np.uint8)
        self.brush = np.zeros((120, 160), np.float32)
        self.brush[:60, :80] = 1.0

    def test_brush_mask_passes_through_local_mask(self):
        mask = polish._local_mask(self.image, [], 'brush', brush_mask=self.brush)
        self.assertEqual(mask.shape, (120, 160))
        np.testing.assert_allclose(mask, self.brush)
        # Empty brush must never silently turn into a full-image adjustment.
        fallback = polish._local_mask(self.image, [], 'brush')
        np.testing.assert_array_equal(fallback, np.zeros((120, 160), np.float32))

    def test_empty_brush_leaves_local_pixels_unchanged(self):
        params = polish.PolishParams(wb=0, skin=.8, clarity=.8, eyes=False,
                                     local_region='brush')
        for mask in (None, np.zeros(self.image.shape[:2], np.float32)):
            result = polish.polish_image(self.image, params, faces=[], brush_mask=mask)
            np.testing.assert_array_equal(result, self.image)

    def test_rgba_brush_decoder_preserves_alpha_coverage(self):
        import base64
        import cv2
        rgba = np.full((1, 4, 4), 255, np.uint8)
        rgba[0, :, 3] = [0, 64, 128, 255]
        encoded = base64.b64encode(cv2.imencode('.png', rgba)[1]).decode()
        decoded = server._decode_brush_mask(encoded)
        np.testing.assert_allclose(decoded[0], np.array([0, 64, 128, 255]) / 255,
                                   atol=1e-6)

    def test_preview_and_frozen_export_keep_identical_brush_pixels(self):
        import base64
        import cv2
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'brush.png'
            Image.fromarray(self.image).save(source)
            original = source.read_bytes()
            encoded = base64.b64encode(cv2.imencode('.png', (self.brush * 255).astype(np.uint8))[1]).decode()
            retouch = {'local_region': 'brush', 'brush_mask': encoded, 'clarity': .8,
                       'skin': 0, 'retain': .4, 'face_repair': 0, 'blemish': 0, 'ai_denoise': 0}
            prepared = server._prepare({'folder': tmp, 'paths': [str(source)], 'operation': 'polish',
                                       'export_size': 'original',
                                       'polish': {'wb': 0, 'eyes': False, **retouch}})
            try:
                captured = []
                from photopilot.io import save_image
                def save(pixels, destination, **kwargs):
                    captured.append(pixels.copy())
                    return save_image(pixels, destination, **kwargs)
                with patch.object(server, 'analyze_faces', return_value=[]), \
                     patch.object(server, 'save_image', side_effect=save), \
                     patch.object(server, 'THUMB_DIR', Path(tmp) / 'cache'):
                    server._process_one({'sid': prepared['sid'], 'path': str(source), 'preview_only': True,
                                         'retouch_adjustments': retouch})
                    server._process_one({'sid': prepared['sid'], 'path': str(source),
                                         'retouch_adjustments': retouch})
                np.testing.assert_array_equal(captured[0], captured[1])
                np.testing.assert_array_equal(captured[1][70:, 90:], self.image[70:, 90:])
                self.assertTrue(np.any(captured[1][:50, :70] != self.image[:50, :70]))
                self.assertEqual(source.read_bytes(), original)
            finally:
                server._SESSIONS.pop(prepared['sid'], None)

    def test_brush_mask_resized_to_image_resolution(self):
        small = np.zeros((30, 40), np.float32)
        small[:, :20] = 1.0
        mask = polish._local_mask(self.image, [], 'brush', brush_mask=small)
        self.assertEqual(mask.shape, (120, 160))
        self.assertGreater(float(mask[:, :40].mean()), .9)
        self.assertLess(float(mask[:, 100:].mean()), .1)

    def test_polish_clarity_applies_only_inside_brush(self):
        params = polish.PolishParams(wb=0, skin=0, clarity=.8, local_region='brush')
        result = polish.polish_image(self.image, params, faces=[], brush_mask=self.brush)
        delta = np.abs(result.astype(float) - self.image.astype(float)).mean(axis=2)
        self.assertGreater(float(delta[:60, :80].mean()), .5)
        self.assertLess(float(delta[60:, 80:].mean()), .05)

    def test_polish_warns_on_empty_brush_region(self):
        warnings = []
        params = polish.PolishParams(wb=0, clarity=.8, local_region='brush')
        polish.polish_image(self.image, params, faces=[], warnings=warnings)
        self.assertTrue(any('画笔区域为空' in message for message in warnings))

    def test_params_validate_brush_region(self):
        polish.PolishParams(local_region='brush')  # 不抛错
        with self.assertRaises(ValueError):
            polish.PolishParams(local_region='nope')

    def test_server_decodes_brush_mask_base64(self):
        import base64
        import cv2
        png = cv2.imencode('.png', (self.brush * 255).astype(np.uint8))[1]
        encoded = base64.b64encode(png.tobytes()).decode()
        decoded = server._decode_brush_mask(encoded)
        self.assertEqual(decoded.shape, (120, 160))
        self.assertGreater(float(decoded[:60, :80].mean()), .99)
        decoded_url = server._decode_brush_mask('data:image/png;base64,' + encoded)
        np.testing.assert_array_equal(decoded, decoded_url)
        for bad in ('', 'not-base64!!!', 12345, None):
            with self.assertRaises(ValueError):
                server._decode_brush_mask(bad)

    def test_render_session_applies_brush_mask(self):
        import base64
        import cv2
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'p.png'
            Image.fromarray(self.image).save(source)
            session = server._prepare({'folder': tmp, 'paths': [str(source)], 'operation': 'polish',
                                       'polish': {'wb': 0, 'skin': 0, 'clarity': 0, 'local_region': 'all'}})
            png = cv2.imencode('.png', (self.brush * 255).astype(np.uint8))[1]
            encoded = base64.b64encode(png.tobytes()).decode()
            def render(retouch):
                with patch.object(server, 'analyze_faces', return_value=[]), \
                     patch.object(server, 'THUMB_DIR', Path(tmp) / 'cache'):
                    return server._process_one({'sid': session['sid'], 'path': str(source), 'preview_only': True,
                                                'retouch_adjustments': retouch})
            base = {'local_region': 'brush', 'brush_mask': None, 'clarity': 0, 'skin': 0,
                    'retain': .4, 'face_repair': 0, 'blemish': 0}
            with_brush = render({**base, 'brush_mask': encoded, 'clarity': .8})
            without = render({**base})
            a = np.asarray(Image.open(with_brush['out']).convert('RGB')).astype(float)
            b = np.asarray(Image.open(without['out']).convert('RGB')).astype(float)
            delta = np.abs(a - b).mean(axis=2)
            h, w = delta.shape
            # 画笔区内有差异、区外几乎一致（两组渲染仅清晰度作用范围不同）
            self.assertGreater(float(delta[:h // 2, :w // 2].mean()), float(delta[h // 2:, w // 2:].mean()) * 3)


if __name__ == '__main__':
    unittest.main()
