"""Windows packaging and cross-platform UI path contracts; no EXE is built here."""
from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    'windows_builder',
    Path(__file__).resolve().parents[1] / 'scripts/build_windows.py',
)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class WindowsBuildTests(unittest.TestCase):
    def test_ci_builds_explicit_optional_model_artifact_and_imports_it(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / '.github/workflows/build-windows.yml'
        ).read_text(encoding='utf-8')
        self.assertIn(
            'python scripts/build_windows.py --allow-missing-aesthetic', workflow)
        self.assertIn('python -m unittest discover -s tests -v', workflow)
        self.assertIn('npm install --no-save playwright-core', workflow)
        self.assertIn("PLAYWRIGHT_CORE: '1'", workflow)
        self.assertIn('node tests/frontend_regressions.cjs', workflow)
        self.assertIn('./tests/windows_smoke.ps1', workflow)
        self.assertIn('PhotoPilot-*-windows-x64.zip', workflow)
        self.assertIn('smoke-diagnostics', workflow)
        smoke = (Path(__file__).resolve().parents[1] / 'tests/windows_smoke.ps1').read_text(
            encoding='utf-8-sig')
        self.assertIn("'cull', $sample", smoke)
        self.assertIn("'polish', $source", smoke)
        self.assertIn('-RedirectStandardOutput', smoke)
        self.assertIn('-RedirectStandardError', smoke)
        self.assertIn('Start-Process', smoke)
        self.assertIn('-Wait', smoke)
        self.assertIn('Exported JPEG is unexpectedly small', smoke)
        self.assertIn('windows_ui_probe.ps1', smoke)
        ui_smoke = (Path(__file__).resolve().parents[1] / 'tests/windows_ui_smoke.py').read_text(
            encoding='utf-8')
        self.assertIn('/api/import_paths', ui_smoke)
        self.assertIn('/api/scan_status', ui_smoke)
        self.assertIn("host='evil.example'", ui_smoke)

    def test_missing_aesthetic_requires_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(builder, 'ROOT', Path(tmp)):
            with self.assertRaises(SystemExit):
                builder._build_command('PhotoPilot', console=False)

    def test_explicit_missing_aesthetic_collects_optional_denoise_and_webview2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'models').mkdir()
            denoise = root / 'models/nafnet_sidd_width32.onnx'
            denoise.touch()
            self.assertIn(
                'allow_missing_aesthetic',
                inspect.signature(builder._build_command).parameters,
            )
            with patch.object(builder, 'ROOT', root):
                command = builder._build_command(
                    'PhotoPilot', console=False, allow_missing_aesthetic=True)

        self.assertIn(str(denoise) + ';models', command)
        self.assertFalse(any(
            'nima_mobilenet_aesthetic_apache.onnx' in str(arg) for arg in command))
        hidden = {
            command[index + 1]
            for index, arg in enumerate(command)
            if arg == '--hidden-import'
        }
        self.assertTrue({
            'webview.platforms.winforms',
            'webview.platforms.edgechromium',
            'webview.platforms.win32',
            'pythonnet',
            'clr_loader',
        } <= hidden)
        excluded = {
            command[index + 1]
            for index, arg in enumerate(command)
            if arg == '--exclude-module'
        }
        self.assertNotIn('matplotlib', excluded)
        self.assertNotIn('clr_loader', excluded)
        self.assertNotIn('pythonnet', excluded)
        self.assertNotIn('AppKit', excluded)

    def test_present_aesthetic_model_is_collected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'models').mkdir()
            model = root / 'models/nima_mobilenet_aesthetic_apache.onnx'
            model.touch()
            with patch.object(builder, 'ROOT', root):
                command = builder._build_command('PhotoPilot', console=False)

        self.assertIn(str(model) + ';models', command)

    def test_windows_icon_is_used_only_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'models').mkdir()
            (root / 'models/nima_mobilenet_aesthetic_apache.onnx').touch()
            with patch.object(builder, 'ROOT', root):
                without_icon = builder._build_command('PhotoPilot', console=False)
            self.assertNotIn('--icon', without_icon)

            (root / 'scripts').mkdir()
            icon = root / 'scripts/PhotoPilot.ico'
            icon.touch()
            with patch.object(builder, 'ROOT', root):
                with_icon = builder._build_command('PhotoPilot', console=False)
            self.assertIn('--icon', with_icon)
            self.assertIn(str(icon), with_icon)

if __name__ == '__main__':
    unittest.main()
