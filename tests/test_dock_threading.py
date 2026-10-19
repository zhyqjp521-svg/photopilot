import types
import unittest
from unittest.mock import patch, Mock

from photopilot import platform


class DockThreadingTests(unittest.TestCase):
    def test_background_badge_waits_for_main_queue_before_touching_appkit(self):
        pending = []
        tile = Mock()
        application = Mock()
        application.sharedApplication.return_value.dockTile.return_value = tile
        foundation = types.SimpleNamespace(
            NSThread=types.SimpleNamespace(isMainThread=lambda: False),
            NSOperationQueue=types.SimpleNamespace(mainQueue=lambda: types.SimpleNamespace(
                addOperationWithBlock_=pending.append)))
        with patch.object(platform, 'IS_MACOS', True), patch.dict('sys.modules', {
                'Foundation': foundation, 'AppKit': types.SimpleNamespace(NSApplication=application)}):
            platform.dock_badge('2/8')
            application.sharedApplication.assert_not_called()
            self.assertEqual(len(pending), 1)
            pending[0]()
            tile.setBadgeLabel_.assert_called_once_with('2/8')

    def test_main_thread_badge_updates_directly_and_none_clears(self):
        tile = Mock()
        application = Mock()
        application.sharedApplication.return_value.dockTile.return_value = tile
        queue = Mock()
        foundation = types.SimpleNamespace(
            NSThread=types.SimpleNamespace(isMainThread=lambda: True),
            NSOperationQueue=types.SimpleNamespace(mainQueue=queue))
        with patch.object(platform, 'IS_MACOS', True), patch.dict('sys.modules', {
                'Foundation': foundation, 'AppKit': types.SimpleNamespace(NSApplication=application)}):
            platform.dock_badge(None)
            tile.setBadgeLabel_.assert_called_once_with(None)
            queue.assert_not_called()
