"""Deterministic similarity tests; no original photos or sidecars are touched."""
import unittest

import numpy as np

from photopilot.cull import F_DUP, PhotoScore, finalize_cull


def photo(path, score=.7, width=1500, height=1000):
    return PhotoScore(path, width, height, .8, .8, .8, .8, .5, score, 0, 0)


def bits(count):
    return np.arange(64) < count


class SimilarityTests(unittest.TestCase):
    def test_anchor_stops_gradual_chain_drift(self):
        photos = [photo(f'/shoot/{i}.jpg') for i in range(3)]
        hashes = {p.path: bits(i * 8) for i, p in enumerate(photos)}
        finalize_cull(photos, hashes)
        self.assertEqual(photos[0].group, photos[1].group)
        self.assertNotEqual(photos[1].group, photos[2].group)
        self.assertEqual([p.group_size for p in photos], [2, 2, 1])

    def test_same_hash_different_directories_does_not_group(self):
        photos = [photo('/shoot/day1/a.jpg'), photo('/shoot/day2/b.jpg')]
        finalize_cull(photos, {p.path: bits(0) for p in photos})
        self.assertNotEqual(photos[0].group, photos[1].group)

    def test_same_hash_incompatible_aspect_does_not_group(self):
        photos = [photo('/shoot/a.jpg'), photo('/shoot/b.jpg', width=1000, height=1500)]
        finalize_cull(photos, {p.path: bits(0) for p in photos})
        self.assertNotEqual(photos[0].group, photos[1].group)

    def test_missing_hash_breaks_group(self):
        photos = [photo(f'/shoot/{i}.jpg') for i in range(3)]
        finalize_cull(photos, {photos[0].path: bits(0), photos[2].path: bits(0)})
        self.assertEqual(photos[1].group, -1)
        self.assertNotEqual(photos[0].group, photos[2].group)

    def test_aspect_compatibility_does_not_depend_on_filename_order(self):
        for widths in [(1500, 1800), (1800, 1500)]:
            with self.subTest(widths=widths):
                photos = [photo('/shoot/a.jpg', width=widths[0]),
                          photo('/shoot/b.jpg', width=widths[1])]
                finalize_cull(photos, {p.path: bits(0) for p in photos})
                self.assertNotEqual(photos[0].group, photos[1].group)

    def test_candidate_must_match_previous_even_if_anchor_matches(self):
        photos = [photo(f'/shoot/{i}.jpg') for i in range(3)]
        last = np.logical_and(np.arange(64) >= 8, np.arange(64) < 16)
        finalize_cull(photos, {photos[0].path: bits(0), photos[1].path: bits(8), photos[2].path: last})
        self.assertEqual(photos[0].group, photos[1].group)
        self.assertNotEqual(photos[1].group, photos[2].group)

    def test_profiles_use_predictable_distance_thresholds(self):
        for profile, limit in [('strict', 4), ('balanced', 8), ('loose', 12)]:
            for distance in [limit, limit + 1]:
                with self.subTest(profile=profile, distance=distance):
                    photos = [photo('/shoot/a.jpg'), photo('/shoot/b.jpg')]
                    finalize_cull(photos, {photos[0].path: bits(0), photos[1].path: bits(distance)},
                                  similarity_profile=profile)
                    self.assertEqual(photos[0].group == photos[1].group, distance <= limit)

    def test_invalid_profile_rejected_before_mutation(self):
        p = photo('/shoot/a.jpg')
        p.group, p.flags = 123, [F_DUP]
        for invalid in ['fast', '', None, 8, []]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                finalize_cull([p], {p.path: bits(0)}, similarity_profile=invalid)
            self.assertEqual((p.group, p.flags), (123, [F_DUP]))

    def test_regrouping_clears_duplicates_and_recomputes_rank(self):
        photos = [photo('/shoot/a.jpg', .5), photo('/shoot/b.jpg', .9)]
        hashes = {p.path: bits(i * 6) for i, p in enumerate(photos)}
        finalize_cull(photos, hashes, similarity_profile='loose')
        self.assertEqual([p.group_rank for p in photos], [2, 1])
        self.assertEqual([p.group_best for p in photos], [False, True])
        self.assertEqual(photos[0].flags.count(F_DUP), 1)
        finalize_cull(photos, hashes, similarity_profile='loose')
        self.assertEqual(photos[0].flags.count(F_DUP), 1)
        finalize_cull(photos, hashes, similarity_profile='strict')
        self.assertEqual([p.group_size for p in photos], [1, 1])
        self.assertEqual([p.group_rank for p in photos], [1, 1])
        self.assertTrue(all(F_DUP not in p.flags for p in photos))


if __name__ == '__main__':
    unittest.main()
