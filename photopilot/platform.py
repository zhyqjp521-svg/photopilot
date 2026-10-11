"""跨平台运行环境适配：路径与废纸篓/回收站。

PhotoPilot 同时支持 macOS / Windows / Linux。不同系统存放配置、缓存、
日志的位置不同，本模块统一收敛这些差异，让业务代码不直接感知平台。

- 应用数据目录（项目存档、导出队列）：
    macOS   ~/Library/Application Support/PhotoPilot
    Windows %APPDATA%/PhotoPilot          （本地应用数据）
    Linux   ~/.local/share/photopilot
- 缓存目录（缩略图、单实例锁、chrome profile）：
    macOS   ~/Library/Caches/PhotoPilot
    Windows %LOCALAPPDATA%/PhotoPilot/Cache
    Linux   ~/.cache/photopilot
- 日志目录：
    macOS   ~/Library/Logs
    Windows %LOCALAPPDATA%/PhotoPilot/Logs
    Linux   ~/.local/state/photopilot/logs（回退 ~/.cache）
- 图片导入目录（用户可见）：
    macOS   ~/Pictures/PhotoPilot
    Windows  %USERPROFILE%/Pictures/PhotoPilot
    Linux   ~/Pictures/PhotoPilot
- 废纸篓 / 回收站：macOS 用系统 Foundation 废纸篓；Windows / Linux 用
    send2trash（Windows 走回收站 API，Linux 走 freedesktop Trash），
    均是可恢复删除，不永久 unlink 用户照片。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def _home() -> Path:
    return Path.home()


def app_data_dir() -> Path:
    """应用数据目录：项目存档、导出队列等需要跨会话持久化的数据。"""
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or str(_home() / "AppData" / "Roaming")
        return Path(base) / "PhotoPilot"
    if IS_MACOS:
        return _home() / "Library" / "Application Support" / "PhotoPilot"
    base = os.environ.get("XDG_DATA_HOME") or str(_home() / ".local" / "share")
    return Path(base) / "photopilot"


def cache_dir() -> Path:
    """缓存目录：缩略图、单实例锁、chrome profile 等可随时重建的数据。"""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(_home() / "AppData" / "Local")
        return Path(base) / "PhotoPilot" / "Cache"
    if IS_MACOS:
        return _home() / "Library" / "Caches" / "PhotoPilot"
    base = os.environ.get("XDG_CACHE_HOME") or str(_home() / ".cache")
    return Path(base) / "photopilot"


def log_dir() -> Path:
    """日志目录。"""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(_home() / "AppData" / "Local")
        return Path(base) / "PhotoPilot" / "Logs"
    if IS_MACOS:
        return _home() / "Library" / "Logs"
    state = os.environ.get("XDG_STATE_HOME")
    if state:
        return Path(state) / "photopilot"
    return cache_dir()


def pictures_dir() -> Path:
    """用户可见的图片目录，用于放置导入的照片。"""
    if IS_WINDOWS:
        # 尊重系统已知文件夹，避免语言差异；失败回退英文名。
        base = os.environ.get("USERPROFILE") or str(_home())
        return Path(base) / "Pictures" / "PhotoPilot"
    return _home() / "Pictures" / "PhotoPilot"


def move_to_trash(path: str | Path) -> None:
    """把文件移到系统废纸篓 / 回收站（可恢复，绝不永久 unlink）。

    macOS：Foundation NSFileManager（无第三方依赖）。
    Windows / Linux：send2trash（Windows 用回收站 API，Linux 用 freedesktop Trash）。

    任一方式不可用时抛 RuntimeError，调用方负责降级提示。
    """
    target = Path(path)
    if IS_MACOS:
        _trash_macos(target)
        return
    _trash_send2trash(target)


def _trash_macos(path: Path) -> None:
    try:
        from Foundation import NSFileManager, NSURL
    except ImportError as exc:
        raise RuntimeError("当前系统无法访问 macOS 废纸篓") from exc
    url = NSURL.fileURLWithPath_(str(path))
    result = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
        url, None, None)
    if isinstance(result, tuple):
        moved, _trashed_url, error = (result + (None, None, None))[:3]
    else:
        moved, error = bool(result), None
    if not moved:
        raise OSError(str(error or "macOS 未能将文件移到废纸篓"))


def _trash_send2trash(path: Path) -> None:
    try:
        from send2trash import send2trash
    except ImportError as exc:
        raise RuntimeError("当前系统缺少 send2trash，无法访问回收站") from exc
    send2trash(str(path))


def app_data_file(name: str) -> Path:
    """应用数据目录下的一个具名文件。"""
    return app_data_dir() / name
