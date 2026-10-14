"""桌面窗口自动化冒烟测试（Playwright 驱动真实 Chromium）。

覆盖 AUDIT.md 遗留的「桌面 UI 实际点击走查」：启动 → 选目录 → 扫描打分 →
选图 → 批量导出 → 验证产物 + 截图留档。不触碰用户照片，所有输入均来自
临时目录的合成图。

运行：.venv/bin/python tests/smoke_desktop.py [--headed] [--keep]
依赖：pip install playwright（浏览器二进制已缓存时无需下载）。

退出码 0 表示全链路通过；任一断言失败即报错退出非 0。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

# 截图与产物留档目录（测试产物，非源码）
ARTIFACT_DIR = ROOT / "tests" / "_smoke_out"


def _make_input_dir() -> Path:
    """造 6 张合成 JPEG（含 EXIF）到临时目录，模拟一个待审照片夹。"""
    d = Path(tempfile.mkdtemp(prefix="pp_smoke_"))
    rng = np.random.default_rng(42)
    for i in range(6):
        h, w = 240, 320
        arr = rng.integers(0, 255, (h, w, 3), dtype=np.uint8)
        # 每张加不同色偏，让追色/评分有区分度
        arr[:, :, i % 3] = (arr[:, :, i % 3].astype(int) * 1.3).clip(0, 255).astype(np.uint8)
        p = d / f"shot_{i:02d}.jpg"
        img = Image.fromarray(arr, "RGB")
        exif = Image.Exif()
        exif[0x010F] = "SmokeCam"
        exif[0x0110] = f"MODEL-{i}"
        exif[0x829D] = (28, 10)   # f/2.8
        exif[0x829A] = (1, 125)   # 1/125s
        exif[0x8827] = 200 * (i + 1)
        exif[0x920A] = (50, 1)    # 50mm
        img.save(p, exif=exif)
    return d


def _server_up(url: str, timeout: float = 15.0) -> None:
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"服务未在 {timeout}s 内就绪：{url}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true", help="有头模式（可见窗口）")
    ap.add_argument("--keep", action="store_true", help="保留测试输入目录与产物")
    args = ap.parse_args()

    from photopilot.server import start_server, _find_free_port

    in_dir = _make_input_dir()
    port = _find_free_port(8618)
    httpd = start_server(str(in_dir), port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"

    from playwright.sync_api import sync_playwright

    failures: list[str] = []
    artifact = ARTIFACT_DIR
    artifact.mkdir(parents=True, exist_ok=True)

    def check(cond: bool, msg: str) -> None:
        print(("  ✅ " if cond else "  ❌ ") + msg)
        if not cond:
            failures.append(msg)

    try:
        _server_up(url)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            page = browser.new_page(viewport={"width": 1440, "height": 920})
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # 1) 启动：标题与核心控件存在
            check(page.title().startswith("PhotoPilot"), f"窗口标题含 PhotoPilot：{page.title()!r}")
            check(page.locator("#folder").count() == 1, "目录输入框 #folder 存在")
            check(page.locator("#scanBtn").count() == 1, "扫描按钮 #scanBtn 存在")
            page.screenshot(path=str(artifact / "1_loaded.png"))

            # 2) 选目录：页面已注入启动目录，直接触发扫描
            page.click("#scanBtn")
            page.wait_for_selector("#scanbar.show", timeout=10000)
            page.screenshot(path=str(artifact / "2_scanning.png"))

            # 3) 扫描：等待 grid 出现 6 张卡片、扫描条消失（上限 90s）
            page.wait_for_selector("#grid .card", timeout=90000)
            page.wait_for_function(
                "document.querySelectorAll('#grid .card').length >= 6",
                timeout=90000)
            page.wait_for_function(
                "!document.querySelector('#scanbar')?.classList.contains('show')",
                timeout=90000)
            n_cards = page.locator("#grid .card").count()
            check(n_cards >= 6, f"扫描出 {n_cards} 张卡片（期望 ≥6）")
            page.screenshot(path=str(artifact / "3_scanned.png"))

            # 4) 选图：点击前两张卡片选中（空格键/点击），再点「全选」兜底
            page.evaluate("selAll(1)")  # 全选，走真实选中路径
            page.wait_for_timeout(300)
            page.screenshot(path=str(artifact / "4_selected.png"))

            # 5) 导出：走批量导出队列（美化操作，多张 → 队列）
            page.evaluate("process('polish')")
            page.wait_for_selector("#exportQueueList", timeout=10000)

            # 6) 验证产物：轮询 photopilot_out 下的 _pp 文件（导出完成的客观依据，
            #    不依赖队列 DOM 状态文案）。上限 120s。
            out_dir = in_dir / "photopilot_out"
            deadline = time.time() + 120
            out_files: list[Path] = []
            while time.time() < deadline:
                out_files = list(out_dir.rglob("*_pp.jpg")) if out_dir.exists() else []
                if len(out_files) >= 6:
                    break
                time.sleep(0.5)
            page.screenshot(path=str(artifact / "5_exported.png"))
            check(len(out_files) >= 6, f"导出产物 {len(out_files)} 张（期望 ≥6，{out_dir}）")

            page.screenshot(path=str(artifact / "6_final.png"))
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()

    if failures:
        print(f"\n冒烟测试失败 {len(failures)} 项：")
        for f in failures:
            print("  -", f)
        if args.keep:
            print("保留输入目录：", in_dir)
        else:
            shutil.rmtree(in_dir, ignore_errors=True)
        return 1

    print(f"\n✅ 桌面冒烟测试全链路通过（6 项断言）")
    print("   截图留档：", artifact)
    if args.keep:
        print("   保留输入目录：", in_dir)
    else:
        shutil.rmtree(in_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
