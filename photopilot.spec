# -*- mode: python ; coding: utf-8 -*-
# PhotoPilot PyInstaller 打包配置（Windows x64 单文件）
#
# 用法：在 Windows 上，装好项目依赖后执行
#     pyinstaller --noconfirm photopilot.spec
# 或直接用 scripts/build_windows.py。
#
# 关键点：
#  1) mediapipe 内置 tflite 模型与 tasks 资源必须随包收集（collect_data_files）。
#  2) onnxruntime / opencv 是带原生二进制的大型依赖，PyInstaller 会自动收集。
#  3) pywebview 的 GUI 后端在 Windows 走 Edge WebView2，无需额外文件。
#  4) 项目里 __version__ / 入口在 photopilot.cli:main。
#  5) 单文件 --onefile 便于 U 盘分发；首次启动会解压到临时目录，稍慢属正常。

import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_submodules,
)

ROOT = Path(SPECPATH)  # SPECPATH = spec 所在目录（仓库根）

# ---------------------------------------------------------------------------
# 入口：命令行工具。桌面模式（app）与 Web UI（ui）都从 cli.main 进入，
# 因此打一个 exe 即可覆盖全部子命令。
# ---------------------------------------------------------------------------
block_cipher = None

a = Analysis(
    ["run_photopilot.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# ---------------------------------------------------------------------------
# mediapipe：内置 tflite 模型 + tasks 资源（人脸网格模型等）
# 使用 collect_all 收集其数据文件与子模块，避免遗漏。
# ---------------------------------------------------------------------------
try:
    mp_datas, mp_bins, mp_hidden = collect_all("mediapipe")
    a.datas += mp_datas
    a.binaries += mp_bins
    a.hiddenimports += mp_hidden
except Exception as e:
    print(f"[warn] collect_all('mediapipe') failed: {e}", file=sys.stderr)

# onnxruntime：需要收集其原生动态库（DirectML/CPU 后端）
try:
    ort_datas, ort_bins, ort_hidden = collect_all("onnxruntime")
    a.datas += ort_datas
    a.binaries += ort_bins
    a.hiddenimports += ort_hidden
except Exception as e:
    print(f"[warn] collect_all('onnxruntime') failed:{e}", file=sys.stderr)

# opencv：cv2 原生库由 PyInstaller 钩子自动处理，此处补全子模块，
# 确保 CascadeClassifier 的 haarcascade 数据文件被收集。
try:
    a.datas += collect_data_files("cv2")
except Exception as e:
    print(f"[warn] collect_data_files('cv2') failed:{e}", file=sys.stderr)

# pywebview：GUI 后端，Windows 走 WebView2；无需数据文件，但补 hidden imports
a.hiddenimports += collect_submodules("webview")

# rawpy：LibRaw 原生绑定，PyInstaller 自带钩子会收集 libraw 动态库
try:
    a.datas += collect_data_files("rawpy")
except Exception as e:
    print(f"[warn] collect_data_files('rawpy') failed:{e}", file=sys.stderr)

# send2trash：Windows/Linux 回收站（macOS 走 Foundation，无需此包）
try:
    a.hiddenimports += collect_submodules("send2trash")
except Exception as e:
    print(f"[warn] collect_submodules('send2trash') failed:{e}", file=sys.stderr)

# ---------------------------------------------------------------------------
# 常见缺漏补丁：这些包在隐式导入时可能被 PyInstaller 漏掉
# ---------------------------------------------------------------------------
a.hiddenimports += [
    "mediapipe.python.solutions.face_mesh",
    "mediapipe.python.solutions.face_mesh_connections",
    "onnxruntime.capi._pybind_state",
    "cv2",
    "PIL._tkinter_finder",  # Pillow 导入探测，避免误报
    "numpy",
    "scipy",  # 若 polish 用到频率分离的 scipy 变体
]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PhotoPilot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 图形界面/后台工具：双击不弹黑框
    disable_windowed_traceback=False,
    target_arch=None,       # 跟随当前解释器架构（x64 环境即产出 x64）
    codesign_identity=None,
    entitlements_file=None,
)
