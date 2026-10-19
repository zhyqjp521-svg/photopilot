#!/usr/bin/env python3
"""在 macOS 上把 PhotoPilot 打包为可分发、拷给任何 Mac 即用的 .app。

用法（macOS 上，本机 venv 已装好依赖 + pyinstaller）：
    .venv/bin/pip install pyinstaller
    .venv/bin/python scripts/build_mac_dist.py

产物：
    dist/PhotoPilot.app   —— 独立 App 包（内置全部依赖，可拷贝到其他 Mac）

与 scripts/build_app.py 的区别：
    build_app.py      生成的是“指向本机 venv 的启动器”，不可移交给其他机器；
    本脚本用 PyInstaller --onedir 把引擎与依赖一起打进 .app，可独立分发。

架构：
    始终针对运行本脚本的 Python 解释器架构打包。Apple Silicon（arm64）机器上
    产出 arm64，Intel（x86_64）机器上产出 x86_64；如需通用二进制，需在两个
    架构下分别构建后 lipo 合并（本脚本不做）。目标机需为 macOS 11+。

签名：
    默认做 ad-hoc 签名（codesign -s -），消除 Gatekeeper 部分「未知开发者」拦截。
    拥有开发者证书时可用 --identity "Developer ID Application: ..." 正式签名。
"""
from __future__ import annotations

import argparse
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "run_photopilot.py"
APP_VERSION = (re.search(r'^version\s*=\s*"([^"]+)"',
                         (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
                         re.MULTILINE).group(1) if (ROOT / "pyproject.toml").exists()
               else "0.0.0")


def _banner(msg: str) -> None:
    print(f"\n==== {msg} ====", flush=True)


def _check_platform() -> None:
    if sys.platform != "darwin":
        sys.exit("本脚本必须在 macOS 上运行（PyInstaller 不跨平台编译）。")


def _require_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.exit("缺少 PyInstaller：请先 `.venv/bin/pip install pyinstaller`")


def _build_onedir(*, allow_missing_aesthetic: bool = False) -> Path:
    """PyInstaller --onedir --windowed，产出 dist/PhotoPilot（可执行文件目录）。"""
    model = ROOT / "models" / "nima_mobilenet_aesthetic_apache.onnx"
    if not model.exists() and not allow_missing_aesthetic:
        sys.exit(
            "缺少美学模型 models/nima_mobilenet_aesthetic_apache.onnx：\n"
            "请先从 idealo/image-quality-assessment（Apache-2.0）下载 "
            "weights_mobilenet_aesthetic_0.07.hdf5 并转为 ONNX，或从发布包恢复。")
    if not model.exists():
        print('提示：本次明确跳过缺失的美学评分模型；传统评分及其他功能仍可使用。', flush=True)
    # NAFNet 去噪模型（可选）：缺失时打包仍继续，仅 AI 降噪功能不可用
    denoise_model = ROOT / "models" / "nafnet_sidd_width32.onnx"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onedir", "--windowed",
        "--name", "PhotoPilot",
        # 收集内置 tflite 人脸模型与子模块（缺失则人脸检测失效）
        "--collect-all", "mediapipe",
        # NIMA 美学评分（idealo MobileNet，Apache-2.0）需要 onnxruntime 原生推理库
        "--collect-data", "onnxruntime",
        "--collect-binaries", "onnxruntime",
        # pywebview selects its Cocoa backend dynamically.
        "--hidden-import", "webview.platforms.cocoa",
        "--collect-data", "cv2",
        "--collect-data", "rawpy",
        "--collect-submodules", "send2trash",
        # MediaPipe drawing_utils imports pyplot even when no plot is drawn.
        # Removing matplotlib silently disables the face-mesh backend.
        "--exclude-module", "tkinter",
        "--exclude-module", "pandas",
        "--exclude-module", "IPython",
        # Only ONNX inference is used. Optional training/quantization imports
        # otherwise pull globally installed multi-GB ML frameworks into the app.
        "--exclude-module", "onnxruntime.quantization",
        "--exclude-module", "onnxruntime.tools",
        "--exclude-module", "torch",
        "--exclude-module", "torchvision",
        "--exclude-module", "torchaudio",
        "--exclude-module", "tensorflow",
        "--exclude-module", "jax",
        "--exclude-module", "webview.platforms.qt",
        "--exclude-module", "PyQt6",
        "--exclude-module", "PyQt5",
        "--exclude-module", "PySide6",
        "--exclude-module", "PySide2",
        str(ENTRY),
    ]
    if model.exists():
        cmd[-1:-1] = ['--add-data', f'{model}:models']
    if denoise_model.exists():
        # AI 降噪（NAFNet SIDD）权重，denoise.py 同样按 models/ 定位
        cmd.insert(cmd.index(str(ENTRY)),
                   "--add-data")
        cmd.insert(cmd.index(str(ENTRY)),
                   f"{denoise_model}:models")
    else:
        print("提示：缺少 models/nafnet_sidd_width32.onnx，AI 降噪将不可用（其余功能正常）。",
              flush=True)
    print("执行：", " ".join(cmd), flush=True)
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc != 0:
        sys.exit(f"PyInstaller 返回码 {rc}，打包失败")
    bundle = ROOT / "dist" / "PhotoPilot.app"
    if not bundle.exists():
        sys.exit("未找到 dist/PhotoPilot.app，打包失败")
    return bundle


def _wrap_app(bundle: Path) -> None:
    """把 PyInstaller 生成的 .app 补齐 Info.plist 元数据与图标。"""
    icns = ROOT / "scripts" / "PhotoPilot.icns"
    if icns.exists():
        dst = bundle / "Contents" / "Resources" / "PhotoPilot.icns"
        shutil.copy(icns, dst)
    info_path = bundle / "Contents" / "Info.plist"
    info = plistlib.loads(info_path.read_bytes()) if info_path.exists() else {}
    info.update({
        "CFBundleDisplayName": "PhotoPilot",
        "CFBundleIdentifier": "local.photopilot.desktop",
        "CFBundleVersion": APP_VERSION,
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleIconFile": "PhotoPilot",
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.photography",
        "NSHumanReadableCopyright":
            "PhotoPilot — third-party notices: docs/COMMERCIAL_USE.md",
    })
    info_path.write_bytes(plistlib.dumps(info, fmt=plistlib.FMT_XML))


def _codesign(bundle: Path, identity: str | None) -> None:
    """签名 .app。identity 为 None 时用 ad-hoc（-s -）。"""
    sign = ["codesign", "--force", "--deep", "--sign",
            identity if identity else "-", str(bundle)]
    print("签名：", " ".join(sign), flush=True)
    rc = subprocess.call(sign)
    if rc != 0:
        print("警告：签名失败（返回码 %d），App 仍可运行，但 Gatekeeper 可能拦截"
              % rc, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="打包可分发的 PhotoPilot.app")
    ap.add_argument("--identity", default=None,
                    help="签名身份（默认 ad-hoc：-s -）")
    ap.add_argument("--no-sign", action="store_true", help="跳过签名")
    ap.add_argument('--allow-missing-aesthetic', action='store_true',
                    help='明确允许缺少美学评分模型，保留传统评分与其他功能')
    args = ap.parse_args()

    _check_platform()
    _require_pyinstaller()
    if not ENTRY.exists():
        sys.exit(f"找不到入口脚本：{ENTRY}")

    _banner("打包 PhotoPilot.app（onedir，内置依赖，可分发）")
    bundle = _build_onedir(allow_missing_aesthetic=args.allow_missing_aesthetic)
    _wrap_app(bundle)
    if not args.no_sign:
        _codesign(bundle, args.identity)

    print(f"\n完成：{bundle}", flush=True)
    print("已内置 Python 运行环境；仅适用于相同 CPU 架构。其他 macOS 版本需另行验证。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
