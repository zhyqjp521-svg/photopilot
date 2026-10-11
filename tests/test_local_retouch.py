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


if __name__ == '__main__':
    unittest.main()
