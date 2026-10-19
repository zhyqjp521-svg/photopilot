import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from io import BytesIO

from PIL import Image

from photopilot import cull, io, project


class SizeMetadataTests(unittest.TestCase):
    def test_legacy_raw_stays_unknown_without_decoding_on_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'legacy.arw'
            path.write_bytes(b'raw fixture')
            store = project.ProjectStore(Path(tmp) / 'project.json')
            store.save({'photos': [{'path': str(path), 'width': 1024, 'height': 680, 'score': .5}]})
            with patch.object(io, 'load_image') as decode, patch.object(project.Image, 'open') as header:
                photo = store.load()['state']['photos'][0]
            decode.assert_not_called()
            header.assert_not_called()
            self.assertEqual((photo['original_width'], photo['original_height']), (0, 0))
            self.assertEqual(photo['file_size'], path.stat().st_size)

    def test_raw_preview_keeps_sensor_dimensions_without_a_second_decode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'a.arw'
            path.write_bytes(b'raw fixture')
            buffer = BytesIO()
            Image.new('RGB', (96, 64), 'gray').save(buffer, format='JPEG')
            raw = MagicMock()
            raw.__enter__.return_value = raw
            raw.sizes = SimpleNamespace(width=6000, height=4000)
            raw.extract_thumb.return_value = SimpleNamespace(format='jpeg', data=buffer.getvalue())
            runtime = SimpleNamespace(imread=MagicMock(return_value=raw), ThumbFormat=SimpleNamespace(JPEG='jpeg'))
            metadata = {}
            with patch.object(io, 'rawpy', runtime), patch.object(io, 'HAS_RAWPY', True):
                image = io.load_image(path, max_size=320, raw_preview=True, metadata=metadata)
            self.assertEqual(image.shape[:2], (64, 96))
            self.assertEqual((metadata['original_width'], metadata['original_height']), (6000, 4000))
            runtime.imread.assert_called_once()
            raw.postprocess.assert_not_called()

    def test_scan_records_original_dimensions_not_working_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'large.jpg'
            Image.new('RGB', (2400, 1600), 'gray').save(path)
            score, _ = cull._score_file(str(path), faces=False)
            self.assertLessEqual(score.width, cull.WORK_SIZE)
            self.assertEqual((score.original_width, score.original_height), (2400, 1600))
            self.assertEqual(score.file_size, path.stat().st_size)

    def test_metadata_is_captured_before_jpeg_draft_resize(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'large.jpg'
            Image.new('RGB', (2400, 1600), 'gray').save(path)
            metadata = {}
            image = io.load_image(path, max_size=320, metadata=metadata)
            self.assertLessEqual(image.shape[1], 320)
            self.assertEqual(metadata['original_width'], 2400)
            self.assertEqual(metadata['original_height'], 1600)

    def test_legacy_checkpoint_recovers_raster_header_and_file_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'icon.png'
            Image.new('RGB', (48, 64), 'gray').save(path)
            store = project.ProjectStore(Path(tmp) / 'project.json')
            state = {'photos': [{'path': str(path), 'width': 48, 'height': 64, 'score': .5}]}
            store.save(state)
            photo = store.load()['state']['photos'][0]
            self.assertEqual((photo['original_width'], photo['original_height']), (48, 64))
            self.assertEqual(photo['file_size'], path.stat().st_size)

