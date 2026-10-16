#!/usr/bin/env python3
"""在 Windows 上把 PhotoPilot 打包为单文件可执行程序（PhotoPilot.exe）。

用法（在 Windows 10/11 上，64 位 Python 3.10/3.11 环境）：
    py -3.11 -m venv .venv-win
    .venv-win\\Scripts\\activate
    pip install -e .
    pip install pyinstaller
    python scripts\\build_windows.py

产物：
    dist\\PhotoPilot.exe            —— 单文件、双击即用、无控制台窗口
    dist\\PhotoPilot-console.exe    ——（可选 --console）带控制台，便于排障

架构：
    始终针对运行本脚本的 Python 解释器架构打包。请使用 64 位 Python
    （官方 Windows 安装包默认即 x64），产物即为 x64（amd64）。

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

# Windows 控制台默认编码可能是 cp1252 / GBK，中文提示会抛 UnicodeEncodeError；
# 强制标准输出用 UTF-8 并替换不可编码字符，保证在任何区域设置下都能运行。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "run_photopilot.py"


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


def _ensure_deps() -> None:
    """确认核心依赖已随 `pip install -e .` 装好，缺失时给出明确提示。"""
    for mod, hint in (("numpy", "numpy"),
                      ("cv2", "opencv-python-headless"),
                      ("PIL", "Pillow"),
                      ("mediapipe", "mediapipe"),
                      ("onnxruntime", "onnxruntime")):
        try:
            __import__(mod)
        except ImportError:
            sys.exit(f"缺少依赖 {hint}：请先执行 `pip install -e .`")


def _build_command(name: str, console: bool) -> list[str]:
    """构造 PyInstaller 命令行。

    用命令行而非 .spec：交给 PyInstaller 自己管理内部 TOC，避免手写 spec
    在不同 PyInstaller 版本间的兼容问题。

    --collect-all mediapipe   收集内置 tflite 人脸模型与子模块（缺了人脸检测会失效）
    --collect-all onnxruntime 收集原生推理库（NIMA 美学评分）
    --add-data models/...     美学模型权重（Apache-2.0 idealo MobileNet）
    --collect-data cv2        收集 haarcascade 级联数据
    --collect-data rawpy      收集 LibRaw 附带数据
    --collect-submodules send2trash  Windows/Linux 回收站
    """
    window_flag = "--console" if console else "--windowed"
    model = ROOT / "models" / "nima_mobilenet_aesthetic_apache.onnx"
    if not model.exists():
        sys.exit(
            "缺少美学模型 models/nima_mobilenet_aesthetic_apache.onnx：\n"
            "请先从 idealo/image-quality-assessment（Apache-2.0）下载 "
            "weights_mobilenet_aesthetic_0.07.hdf5 并转为 ONNX，或从发布包恢复。")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",
        window_flag,
        "--name", name,
        "--collect-all", "mediapipe",
        "--collect-all", "onnxruntime",
        "--add-data", f"{model};models",
        "--collect-data", "cv2",
        "--collect-data", "rawpy",
        "--collect-submodules", "send2trash",
        "--exclude-module", "matplotlib",
        "--exclude-module", "tkinter",
        "--exclude-module", "pandas",
        "--exclude-module", "IPython",
        str(ENTRY),
    ]
    denoise_model = ROOT / "models" / "nafnet_sidd_width32.onnx"
    if denoise_model.exists():
        # AI 降噪（NAFNet SIDD）权重，denoise.py 同样按 models/ 定位
        idx = cmd.index(str(ENTRY))
        cmd[idx:idx] = ["--add-data", f"{denoise_model};models"]
    else:
        print("提示：缺少 models/nafnet_sidd_width32.onnx，AI 降噪将不可用（其余功能正常）。",
              flush=True)
    return cmd


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 PhotoPilot.exe")
    ap.add_argument("--console", action="store_true",
                    help="附带生成一个带控制台的排障版 exe")
    ap.add_argument("--clean", action="store_true", help="构建前清理 build/ 缓存")
    args = ap.parse_args()

    _check_platform()
    _require_pyinstaller()
    _ensure_deps()
    if not ENTRY.exists():
        sys.exit(f"找不到入口脚本：{ENTRY}")

    if args.clean:
        for d in ("build", "dist"):
            shutil.rmtree(ROOT / d, ignore_errors=True)

    _banner("打包 PhotoPilot.exe（x64，单文件，无控制台）")
    cmd = _build_command("PhotoPilot", console=False)
    print("执行：", " ".join(cmd), flush=True)
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc != 0:
        sys.exit(f"PyInstaller 返回码 {rc}，打包失败")

    if args.console:
        _banner("生成排障版（带控制台，PhotoPilot-console.exe）")
        rc2 = subprocess.call(_build_command("PhotoPilot-console", console=True),
                              cwd=str(ROOT))
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
