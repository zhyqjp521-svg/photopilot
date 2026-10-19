import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import numpy as np

from photopilot import server


class PreviewPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name) / 'photo.dng'
        self.source.write_bytes(b'fixture')
        self.cache = Path(self.tmp.name) / 'cache'
        server._thumb_cache.clear()

    def test_browsing_uses_embedded_raw_preview_not_full_development(self):
        with patch.object(server, 'THUMB_DIR', self.cache), patch.object(
                server, 'load_image', return_value=np.zeros((60, 90, 3), np.uint8)) as decode:
            server._thumb(str(self.source), 360)
        self.assertIs(decode.call_args.kwargs.get('raw_preview'), True)

    def test_simultaneous_identical_requests_decode_only_once(self):
        calls = []
        def decode(*args, **kwargs):
            calls.append(1)
            time.sleep(.08)
            return np.zeros((60, 90, 3), np.uint8)
        with patch.object(server, 'THUMB_DIR', self.cache), patch.object(server, 'load_image', side_effect=decode):
            with ThreadPoolExecutor(max_workers=6) as pool:
                outputs = list(pool.map(lambda _: server._thumb(str(self.source), 2200), range(6)))
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(data == outputs[0] for data in outputs))

    def test_failed_preview_can_retry_instead_of_retaining_a_failed_flight(self):
        with patch.object(server, 'THUMB_DIR', self.cache), patch.object(server, 'load_image',
                side_effect=[ValueError('decode failed'), np.zeros((60, 90, 3), np.uint8)]):
            with self.assertRaises(ValueError):
                server._thumb(str(self.source), 360)
            self.assertGreater(len(server._thumb(str(self.source), 360)), 0)
