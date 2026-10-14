"""局部画笔交互冒烟测试（Playwright 驱动真实 Chromium）。

验证 #8 可撤销局部画笔：打开单张编辑器 → 切到画笔区域 → 在预览图上
手刷 → 确认掩码提交（预览刷新 + 撤销可用）→ 撤销 → 重做。不触碰用户
照片，输入为临时目录合成图。

运行：.venv/bin/python tests/smoke_brush.py [--headed] [--keep]
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

ARTIFACT_DIR = ROOT / "tests" / "_smoke_out"


def _make_input_dir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="pp_brush_"))
    rng = np.random.default_rng(7)
    for i in range(3):
        arr = rng.integers(0, 255, (360, 480, 3), dtype=np.uint8)
        p = d / f"portrait_{i}.jpg"
        Image.fromarray(arr, "RGB").save(p)
    return d


def _wait_http(url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    in_dir = _make_input_dir()
    port = 8793

    from photopilot.server import serve
    t = threading.Thread(target=serve, kwargs={
        "folder": None, "port": port, "open_browser": False}, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    assert _wait_http(base), "服务未启动"

    from playwright.sync_api import sync_playwright
    checks = []
    def check(cond, msg):
        checks.append((bool(cond), msg))
        print(("✅ " if cond else "❌ ") + msg)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(base)
        page.wait_for_selector("#folder", timeout=15000)

        # 注入目录并触发扫描
        page.evaluate("(p)=>{const el=document.querySelector('#folder');el.value=p;}", str(in_dir))
        page.evaluate("()=>{if(window.scan)scan();else document.querySelector('#scanBtn')?.click();}")
        page.wait_for_selector(".card", timeout=30000)
        check(True, "扫描出照片卡片")

        # 打开单张编辑器（美化操作，只选第一张 → 单张编辑器）
        page.evaluate("()=>{selectedPaths.clear();const c=document.querySelector('.card');if(c)selectedPaths.add(c.dataset.path);document.querySelectorAll('.card').forEach(x=>x.classList.toggle('sel',selectedPaths.has(x.dataset.path)));process('polish');}")
        page.wait_for_selector("#editStage", timeout=30000)
        # 切到「人像与局部」面板，暴露区域选择器
        page.evaluate("()=>selectEditorPanel('portrait')")
        page.wait_for_selector("#editRegion:not([hidden])", timeout=15000)
        check(True, "单张编辑器打开")

        # 切到「画笔区域」
        page.select_option("#editRegion", "brush")
        page.wait_for_selector("#editBrushControls:not([hidden])", timeout=8000)
        check(True, "画笔控件出现")

        # canvas 应可见
        canvas_visible = page.evaluate("()=>{const c=document.querySelector('#editBrushCanvas');return c&&!c.hidden;}")
        check(canvas_visible, "画笔 canvas 可见")

        # 在 canvas 上刷一笔（从左上到中间）
        page.evaluate("""()=>{
          const c=document.querySelector('#editBrushCanvas');
          const r=c.getBoundingClientRect();
          const x0=r.left+10,y0=r.top+10,x1=r.left+r.width*0.6,y1=r.top+r.height*0.5;
          const ev=(t,x,y)=>new PointerEvent(t,{clientX:x,clientY:y,bubbles:true,pointerId:1,pointerType:'mouse',isPrimary:true});
          c.dispatchEvent(ev('pointerdown',x0,y0));
          c.dispatchEvent(ev('pointermove',x1,y1));
          c.dispatchEvent(ev('pointerup',x1,y1));
        }""")
        time.sleep(1.2)  # 等掩码提交 + 预览防抖

        # 确认 brush_mask 已写入 editor.retouch
        has_mask = page.evaluate("()=>{const e=CURRENT_EDITOR;return !!(e&&e.retouch&&e.retouch.brush_mask&&e.retouch.brush_mask.startsWith('data:image'));}")
        check(has_mask, "画笔掩码已提交到编辑器状态")

        # 撤销按钮应可用
        undo_enabled = page.evaluate("()=>{const b=document.querySelector('#editUndo');return b&&!b.disabled;}")
        check(undo_enabled, "撤销按钮可用（掩码进入历史）")

        page.screenshot(path=str(ARTIFACT_DIR / "brush_painted.png"))

        # 撤销 → 掩码应清空
        page.evaluate("()=>undoEditorTone()")
        time.sleep(0.8)
        mask_after_undo = page.evaluate("()=>{const e=CURRENT_EDITOR;return e&&e.retouch&&e.retouch.brush_mask;}")
        check(not mask_after_undo, "撤销后掩码清空")

        # 重做 → 掩码恢复
        page.evaluate("()=>redoEditorTone()")
        time.sleep(0.8)
        mask_after_redo = page.evaluate("()=>{const e=CURRENT_EDITOR;return !!(e&&e.retouch&&e.retouch.brush_mask);}")
        check(mask_after_redo, "重做后掩码恢复")

        page.screenshot(path=str(ARTIFACT_DIR / "brush_redo.png"))

        # 清除按钮
        page.evaluate("()=>clearBrushMask()")
        time.sleep(0.8)
        mask_after_clear = page.evaluate("()=>{const e=CURRENT_EDITOR;return e&&e.retouch&&e.retouch.brush_mask;}")
        check(not mask_after_clear, "清除后掩码为空")

        browser.close()

    if not args.keep:
        shutil.rmtree(in_dir, ignore_errors=True)

    failed = [m for ok, m in checks if not ok]
    print(f"\n画笔冒烟：{len(checks)-len(failed)}/{len(checks)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
