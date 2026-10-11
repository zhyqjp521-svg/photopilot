#!/usr/bin/env python3
"""在 Windows 上把 PhotoPilot 打包为单文件可执行程序（PhotoPilot.exe）。

用法（在 Windows 10/11 上，64 位 Python 3.10/3.11 环境）：
    py -m venv .venv-win
    .venv-win\\Scripts\\activate
    pip install -e .
    pip install pyinstaller
    python scripts\\build_windows.py

产物：
    dist\\PhotoPilot.exe        —— 单文件、双击即用、无控制台窗口（--windowed）
    dist\\PhotoPilot-console.exe（可选，--console 便于排障）

架构：
    始终针对运行本脚本的 Python 解释器架构打包。请使用 64 位 Python
    （官方 Windows 安装包默认即 x64），产物即为 x64（amd64）。
    本项目不使用任何仅 x86 的依赖，无需 32 位构建。

本脚本只负责“在 Windows 机器上打包”，不跨平台编译：
macOS / Linux 无法产出 Windows .exe，请把本仓库拷到 Windows 上执行本脚本。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "photopilot.spec"


def _banner(msg: str) -> None:
    print(f"\n==== {msg} ====", flush=True)


def _check_platform() -> None:
    if os.name != "nt":
        sys.exit(
            "本脚本必须在 Windows 上运行：PyInstaller 不支持从 macOS/Linux "
            "交叉编译 Windows .exe。请将仓库复制到 Windows 机器后重试。"
        )


def _require_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.exit("缺少 PyInstaller：请先 `pip install pyinstaller`")


def _ensure_data_dirs() -> None:
    """确保 mediapipe 所需资源能被 collect；缺失时给出明确提示。"""
    try:
        import mediapipe
    except ImportError:
        sys.exit("缺少 mediapipe：请先 `pip install -e .` 安装项目依赖")
    mp_dir = Path(mediapipe.__file__).resolve().parent
    for rel in ("modules", "tasks"):
        if not (mp_dir / rel).exists():
            print(f"提示：mediapipe 缺少资源目录 {rel}（{mp_dir / rel}）", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 PhotoPilot.exe")
    ap.add_argument("--console", action="store_true",
                    help="附带生成一个带控制台的排障版 exe")
    ap.add_argument("--clean", action="store_true", help="构建前清理 build/ 缓存")
    args = ap.parse_args()

    _check_platform()
    _require_pyinstaller()
    _ensure_data_dirs()

    if args.clean:
        for d in ("build", "dist"):
            shutil.rmtree(ROOT / d, ignore_errors=True)

    _banner("打包 PhotoPilot.exe（x64，单文件，无控制台）")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        str(SPEC),
    ]
    print("执行：", " ".join(cmd), flush=True)
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc != 0:
        sys.exit(f"PyInstaller 返回码 {rc}，打包失败")

    if args.console:
        _banner("生成排障版（带控制台，同名 *_console.exe）")
        cmd2 = [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm", "--clean",
            "--name", "PhotoPilot-console", "--console",
            str(SPEC),
        ]
        rc2 = subprocess.call(cmd2, cwd=str(ROOT))
        if rc2 != 0:
            print(f"警告：排障版打包失败（返回码 {rc2}），主产物仍可用", flush=True)

    exe = ROOT / "dist" / "PhotoPilot.exe"
    if not exe.exists():
        sys.exit("未找到产物 dist/PhotoPilot.exe，打包失败")
    print(f"\n完成：{exe}", flush=True)
    print("双击运行，或命令行 `PhotoPilot.exe ui <照片目录>` / `cull` / `run` 等。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
