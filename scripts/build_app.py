#!/usr/bin/env python3
"""构建 PhotoPilot.app（macOS 双击图标）。

用法：.venv/bin/python scripts/build_app.py [--dest /Applications|~/Applications]
- 可执行脚本指向本机 .venv（绝对路径），引擎/模型/依赖零拷贝
- 图标 scripts/PhotoPilot.icns；Info.plist 按 Apple 规范
"""
from __future__ import annotations

import argparse
import os
import plistlib
import re
import shutil
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ".venv" / "bin" / "python"
_PROJECT_VERSION = re.search(
    r'^version\s*=\s*"([^"]+)"',
    (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
    re.MULTILINE,
)
APP_VERSION = _PROJECT_VERSION.group(1) if _PROJECT_VERSION else "0.0.0"


LAUNCHER = """#!/bin/bash
# PhotoPilot 桌面启动器：引擎 = 本机 venv + 本项目包
export PYTHONUNBUFFERED=1
cd "{root}"
# LaunchServices may append its private -psn_* token to script-based apps.
# It is not a PhotoPilot CLI argument and would make argparse exit immediately.
args=()
for arg in "$@"; do
    case "$arg" in
        -psn_*) ;;
        *) args+=("$arg") ;;
    esac
done
log_dir="$HOME/Library/Logs"
mkdir -p "$log_dir"
exec /usr/bin/arch -arm64 "{python}" -m photopilot app "${{args[@]}}" \
    >> "$log_dir/PhotoPilot-launcher.log" 2>&1
"""


def build(dest: Path) -> Path:
    if not VENV_PY.exists():
        sys.exit(f"找不到虚拟环境解释器：{VENV_PY}")
    app = dest / "PhotoPilot.app"
    macos = app / "Contents" / "MacOS"
    res = app / "Contents" / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    res.mkdir(parents=True, exist_ok=True)

    # Info.plist
    info = {
        "CFBundleName": "PhotoPilot",
        "CFBundleDisplayName": "PhotoPilot",
        "CFBundleIdentifier": "local.photopilot.desktop",
        "CFBundleVersion": APP_VERSION,
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundlePackageType": "APPL",
        "CFBundleExecutable": "PhotoPilot",
        "CFBundleIconFile": "PhotoPilot",
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.photography",
        "NSHumanReadableCopyright": "MIT License",
    }
    (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps(info, fmt=plistlib.FMT_XML))

    # 启动器
    launcher = macos / "PhotoPilot"
    launcher.write_text(LAUNCHER.format(root=ROOT, python=VENV_PY))
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # 图标
    icns = ROOT / "scripts" / "PhotoPilot.icns"
    if icns.exists():
        shutil.copy(icns, res / "PhotoPilot.icns")

    return app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default=None,
                    help="安装位置（默认 /Applications，不可写回退 ~/Applications）")
    args = ap.parse_args()
    if args.dest:
        dests = [Path(args.dest).expanduser()]
    else:
        dests = [Path("/Applications"), Path.home() / "Applications"]
    for dest in dests:
        try:
            dest.mkdir(parents=True, exist_ok=True)
            probe = dest / ".pp_write_test"
            probe.write_text("x")
            probe.unlink()
            app = build(dest)
            print(f"PhotoPilot.app 已安装：{app}")
            print(f"启动：open {app}   （或 Spotlight 搜 PhotoPilot）")
            return
        except OSError:
            continue
    sys.exit("无法写入 /Applications 或 ~/Applications")


if __name__ == "__main__":
    main()
