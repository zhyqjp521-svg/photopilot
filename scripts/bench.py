"""性能基准：python scripts/bench.py

度量三块：
1. 扫描打分（串行 vs 并行 jobs）
2. 追色（同一参考批量处理 N 张，验证参考统计缓存）
3. JPEG 加载（draft 降采样 vs 全解码）

首次运行自动把 demo/testset 扩充为 demo/bench（48 张）。
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from photopilot.io import load_image                     # noqa: E402
from photopilot.cull import cull_folder, WORK_SIZE       # noqa: E402
from photopilot.color import (reference_from_image, match_color,    # noqa: E402
                              reference_from_preset)


def build_bench(n_copies: int = 4) -> Path:
    ts = ROOT / "demo" / "testset"
    bench = ROOT / "demo" / "bench"
    if bench.exists() and len(list(bench.glob("*.jpg"))) >= len(list(ts.glob("*.jpg"))) * n_copies:
        return bench
    shutil.rmtree(bench, ignore_errors=True)
    bench.mkdir(parents=True)
    i = 0
    for _ in range(n_copies):
        for p in sorted(ts.glob("*.jpg")):
            shutil.copy(p, bench / f"img_{i:03d}.jpg")
            i += 1
    return bench


def main():
    bench = build_bench()
    imgs = sorted(bench.glob("*.jpg"))
    print(f"基准集：{len(imgs)} 张（{bench}）\n")

    # 1. 扫描
    t0 = time.time(); cull_folder(bench, jobs=1); t_seq = time.time() - t0
    t0 = time.time(); cull_folder(bench, jobs=4); t_par = time.time() - t0
    print(f"[扫描] 串行     {t_seq:6.1f} s  ({t_seq/len(imgs)*1000:4.0f} ms/张)")
    print(f"[扫描] 并行 ×4  {t_par:6.1f} s  ({t_par/len(imgs)*1000:4.0f} ms/张)  "
          f"加速 {t_seq/t_par:.1f}x")

    # 2. 追色（参考缓存：同一 reference 连续处理 12 张）
    ref_img = load_image(ROOT / "demo" / "ref" / "ref_warm.jpg", max_size=1024)
    ref = reference_from_image(ref_img)
    srcs = [load_image(p, max_size=WORK_SIZE) for p in imgs[:12]]
    match_color(srcs[0], ref)  # 预热 + 建缓存
    t0 = time.time()
    for s in srcs:
        match_color(s, ref, algo="oklab", strength=1.0)
    dt = (time.time() - t0) / len(srcs)
    print(f"\n[追色] oklab 带参考缓存  {dt*1000:6.0f} ms/张（参考统计只算一次；"
          f"优化前每张重算 ≈ +230ms）")

    t0 = time.time()
    for s in srcs:
        match_color(s, reference_from_preset("film_warm"))
    dt = (time.time() - t0) / len(srcs)
    print(f"[追色] 预设模式          {dt*1000:6.0f} ms/张")

    # 3. JPEG 加载：小图无 draft 收益，大图（真实相机 JPEG 4000-6000px）收益显著
    big_path = ROOT / "demo" / "bench_big.jpg"
    if not big_path.exists():
        import cv2
        base = load_image(imgs[0], max_size=None)
        big = cv2.resize(base, (4800, 3200), interpolation=cv2.INTER_CUBIC)
        from photopilot.io import save_image
        save_image(big, big_path, quality=90)
    t0 = time.time()
    for _ in range(5):
        load_image(big_path, max_size=1024)     # ≤DRAFT_MAX → DCT 域降采样
    t_small = (time.time() - t0) / 5
    t0 = time.time()
    for _ in range(5):
        load_image(big_path, max_size=2048)     # >DRAFT_MAX → 全解码
    t_full = (time.time() - t0) / 5
    print(f"\n[加载] 4800px JPEG → 1024px draft {t_small*1000:4.0f} ms "
          f"vs 全解码 {t_full*1000:4.0f} ms  ({t_full/t_small:.1f}x)")


if __name__ == "__main__":
    main()
