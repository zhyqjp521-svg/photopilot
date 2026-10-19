import importlib
import importlib.util
import tempfile
import time
import unittest
from pathlib import Path

import cv2
import numpy as np


class RegroupJobsTests(unittest.TestCase):
    def test_regroup_existing_photos_without_scores_or_xmp_writes(self):
        self.assertIsNotNone(importlib.util.find_spec('photopilot.similarity_jobs'))
        module = importlib.import_module('photopilot.similarity_jobs')
        rng = np.random.default_rng(4)
        image = rng.integers(0, 255, (80, 120, 3), dtype=np.uint8)
        other = np.flip(image, 1)
        buffers = [cv2.imencode('.jpg', value)[1].tobytes() for value in (image, other, image)]
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            photos = [{'path': str(Path(tmp) / f'{i}.jpg'), 'width': 120, 'height': 80, 'score': .9-i*.1} for i in range(3)]
            def preview(path, size):
                calls.append((path, size))
                return buffers[int(Path(path).stem)]
            jobs = module.SimilarityJobs(preview)
            jid = jobs.start(photos, 'balanced')['job']
            deadline = time.monotonic() + 5
            while not (status := jobs.status(jid))['finished'] and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(status['finished'])
            self.assertFalse(status.get('error'))
            result = {p['path']: p for p in status['photos']}
            self.assertEqual(result[photos[0]['path']]['group'], result[photos[2]['path']]['group'])
            self.assertEqual(result[photos[0]['path']]['group_size'], 2)
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(size == 360 for _, size in calls))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_invalid_requests_fail_before_any_background_work(self):
        self.assertIsNotNone(importlib.util.find_spec('photopilot.similarity_jobs'))
        module = importlib.import_module('photopilot.similarity_jobs')
        jobs = module.SimilarityJobs(lambda *_: self.fail('should not decode'))
        photo = {'path': '/a.jpg', 'width': 10, 'height': 10, 'score': .5}
        for photos, profile in [([], 'balanced'), ([photo], 'bad'), ([photo, photo], 'strict')]:
            with self.assertRaises(ValueError):
                jobs.start(photos, profile)
