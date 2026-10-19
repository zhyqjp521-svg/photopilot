"""Contracts for the packaged Windows UI import smoke test."""
from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WindowsUISmokeContractTests(unittest.TestCase):
    def test_probe_exercises_real_ui_boundaries_and_cleans_up(self):
        source = (ROOT / 'tests/windows_ui_smoke.py').read_text(encoding='utf-8')
        ast.parse(source)
        for endpoint in ('/api/presets', '/api/upload', '/api/scan',
                         '/api/scan_status', '/api/import_paths', '/api/thumb'):
            self.assertIn(endpoint, source)
        self.assertIn("host='evil.example'", source)
        self.assertIn('evidence[\'ok\'] = True', source)

        powershell = (ROOT / 'tests/windows_ui_probe.ps1').read_text(encoding='utf-8')
        self.assertIn("'ui', '--port'", powershell)
        self.assertIn("'--no-browser'", powershell)
        self.assertIn('Stop-Process', powershell)
        self.assertIn('finally', powershell)

    def test_smoke_uses_isolated_windows_user_data(self):
        source = (ROOT / 'tests/windows_smoke.ps1').read_text(encoding='utf-8')
        for name in ('PHOTOPILOT_PROJECT_PATH', 'LOCALAPPDATA', 'APPDATA', 'USERPROFILE'):
            self.assertIn(name, source)
        self.assertIn('windows_ui_probe.ps1', source)
        self.assertIn('Exported JPEG is unexpectedly small', source)


if __name__ == '__main__':
    unittest.main()
