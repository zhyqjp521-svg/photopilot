"""Exact legacy pixel parity and the shared-guide work contract."""
import unittest
import weakref
from unittest.mock import patch

import cv2
import numpy as np

from photopilot import polish


def legacy_guided_filter(guide, src, radius, eps):
    """Standalone pre-optimization algorithm; do not use production helpers."""
    r = max(1, int(radius))
    k = 2 * r + 1

    def box(x):
        return cv2.boxFilter(x, -1, (k, k), normalize=True,
                             borderType=cv2.BORDER_REFLECT)

    mean_i, mean_p = box(guide), box(src)
    corr_ii, corr_ip = box(guide * guide), box(guide * src)
    var_i = corr_ii - mean_i * mean_i
    cov_ip = corr_ip - mean_i * mean_p
    a = cov_ip / (var_i + eps)
    b = mean_p - a * mean_i
    return box(a) * guide + box(b)


def legacy_smooth_skin(img, strength=0.6, retain=0.4, mask=None):
    """Frozen full-resolution RGB smoothing, including luma and final blend."""
    if strength <= 0:
        return img
    h, w = img.shape[:2]
    radius = max(6, min(h, w) // 50)
    f = img.astype(np.float32) / 255.0
    guide = (0.299 * f[..., 0] + 0.587 * f[..., 1] +
             0.114 * f[..., 2])[..., None]
    base = np.stack([legacy_guided_filter(guide[..., 0], f[..., c], radius, 2.5e-3)
                     for c in range(3)], axis=-1)
    detail = f - base
    out = base + retain * detail
    out = np.clip(out, 0, 1) * 255.0
    if mask is not None:
        out = mask[..., None] * out + (1 - mask[..., None]) * img
    out = img.astype(np.float32) * (1 - strength) + out * strength
    return np.clip(out, 0, 255).astype(np.uint8)


class GuidedFilterPerformanceTests(unittest.TestCase):
    def test_shared_guide_statistics_are_released_before_final_blend(self):
        rng = np.random.default_rng(62)
        image = rng.integers(0, 256, (97, 131, 3), dtype=np.uint8)
        mask = rng.random(image.shape[:2], dtype=np.float32)
        stats_refs = []
        original_stats = polish._guided_stats
        original_blend = polish._blend

        def record_stats(*args):
            stats = original_stats(*args)
            stats_refs.extend(weakref.ref(array) for array in stats)
            return stats

        def check_blend(*args):
            self.assertEqual(len(stats_refs), 2)
            self.assertTrue(all(ref() is None for ref in stats_refs),
                            'shared guide statistics remain alive at final blend')
            return original_blend(*args)

        with patch.object(polish, '_guided_stats', side_effect=record_stats), \
                patch.object(polish, '_blend', side_effect=check_blend):
            actual = polish.smooth_skin(image, mask=mask)
        np.testing.assert_array_equal(actual, legacy_smooth_skin(image, mask=mask))

    def test_rgb_smoothing_uses_fourteen_box_filters(self):
        image = np.random.default_rng(73).integers(0, 256, (97, 131, 3), dtype=np.uint8)
        expected = legacy_smooth_skin(image)
        with patch.object(cv2, 'boxFilter', wraps=cv2.boxFilter) as box:
            actual = polish.smooth_skin(image)
        np.testing.assert_array_equal(actual, expected)
        self.assertEqual(box.call_count, 14)

    def test_smooth_skin_matches_legacy_pixels_for_sizes_masks_and_controls(self):
        rng = np.random.default_rng(927)
        for height, width in ((47, 71), (83, 51), (351, 507), (503, 357)):
            image = rng.integers(0, 256, (height, width, 3), dtype=np.uint8)
            original = image.copy()
            masks = (None, rng.random((height, width), dtype=np.float32),
                     np.zeros((height, width), np.float32))
            for mask_index, mask in enumerate(masks):
                original_mask = None if mask is None else mask.copy()
                for strength in (0.01, 0.6, 1.0):
                    for retain in (0.0, 0.4, 1.0):
                        with self.subTest(size=(height, width), mask=mask_index,
                                          strength=strength, retain=retain):
                            actual = polish.smooth_skin(image, strength, retain, mask)
                            expected = legacy_smooth_skin(image, strength, retain, mask)
                            self.assertEqual(actual.dtype, np.uint8)
                            np.testing.assert_array_equal(actual, expected)
                            np.testing.assert_array_equal(image, original)
                            if mask is not None:
                                np.testing.assert_array_equal(mask, original_mask)

    def test_zero_strength_preserves_identity_and_performs_no_box_filters(self):
        image = np.random.default_rng(18).integers(0, 256, (51, 83, 3), dtype=np.uint8)
        original = image.copy()
        for strength in (0.0, -0.1):
            with self.subTest(strength=strength), \
                    patch.object(cv2, 'boxFilter', wraps=cv2.boxFilter) as box:
                self.assertIs(polish.smooth_skin(image, strength=strength), image)
                box.assert_not_called()
        np.testing.assert_array_equal(image, original)

    def test_zero_mask_preserves_pixels(self):
        image = np.random.default_rng(19).integers(0, 256, (89, 63, 3), dtype=np.uint8)
        mask = np.zeros(image.shape[:2], np.float32)
        np.testing.assert_array_equal(polish.smooth_skin(image, 1.0, 0.0, mask), image)

    def test_public_guided_filter_preserves_exact_float_results(self):
        rng = np.random.default_rng(146)
        for dtype in (np.float32, np.float64):
            for height, width in ((1, 1), (9, 15), (39, 27)):
                # Slices exercise the noncontiguous channel inputs used by smooth_skin.
                planes = rng.random((height, width, 3)).astype(dtype)
                src = planes[..., 1]
                guides = (planes[..., 0], np.full((height, width), 0.5, dtype))
                for guide in guides:
                    before_guide, before_src = guide.copy(), src.copy()
                    for radius in (-1, 0, 1, 6, 9.8):
                        for eps in (1e-6, 2.5e-3, 0.1):
                            with self.subTest(dtype=dtype, size=(height, width),
                                              radius=radius, eps=eps):
                                actual = polish.guided_filter(guide, src, radius, eps)
                                expected = legacy_guided_filter(guide, src, radius, eps)
                                self.assertEqual(actual.dtype, expected.dtype)
                                np.testing.assert_array_equal(actual, expected)
                                np.testing.assert_array_equal(guide, before_guide)
                                np.testing.assert_array_equal(src, before_src)


if __name__ == '__main__':
    unittest.main()
