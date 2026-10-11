import tempfile
import unittest
from unittest.mock import patch

from photopilot import server


class ScanSimilarityTests(unittest.TestCase):
    def test_profile_is_saved_in_scan_job(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(server.threading.Thread, 'start'):
            job_id = server._scan_start({'folder': folder, 'similarity_profile': 'strict'})['job']
            try:
                self.assertEqual(server.SCAN_JOBS[job_id].get('similarity_profile'), 'strict')
            finally:
                server.SCAN_JOBS.pop(job_id, None)

    def test_invalid_profile_never_starts_worker(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(server.threading.Thread, 'start') as start:
            with self.assertRaisesRegex(ValueError, 'similarity|相似'):
                server._scan_start({'folder': folder, 'similarity_profile': 'unknown'})
            start.assert_not_called()
