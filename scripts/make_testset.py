"""生成合成测试照片集（无版权问题，可重复构建）。

demo/testset/      被筛/被处理的照片（清晰、模糊、欠曝、过曝、偏色、连拍、肤色块）
demo/ref/ref_warm.jpg  追色参考图（暖调）
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


def base_scene(w=1200, h=800, seed=7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    # 每个种子生成视觉上明显不同的场景（相位/转置/色块随机化），
    # 避免合成图之间被 dHash 误判为连拍
    f1, f2, f3 = 130 + seed * 17, 170 + seed * 23, 220 + seed * 29
    ph = seed * 1.13  # 大相位偏移 → 改变条纹走向与明暗分布
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    r = 120 + 55 * np.sin(xx / f1 + ph) + 35 * np.cos(yy / (f2 - 20) - ph)
    g = 130 + 48 * np.cos(xx / f2 + 2 * ph) + 42 * np.sin(yy / (f1 + 20) + ph)
    b = 150 + 52 * np.sin((xx + yy) / f3 - ph)
    img = np.stack([r, g, b], -1)
    img += rng.normal(0, 8.0, (h, w, 1))
    cx = int(w * (0.2 + 0.15 * rng.random()))
    cy = int(h * (0.25 + 0.3 * rng.random()))
    cv2.circle(img, (cx, cy), int(60 + 50 * rng.random()),
               (int(150 + 60 * rng.random()), 70, 60), -1)
    x0, y0 = int(w * 0.5 * rng.random()), int(h * 0.4 * rng.random())
    cv2.rectangle(img, (x0, y0), (x0 + int(w * 0.25), y0 + int(h * 0.3)),
                  (70, int(140 + 80 * rng.random()), 90), -1)
    kx = (xx - w / 2) / (w / 2)
    ky = (yy - h / 2) / (h / 2)
    vig = 1.0 - 0.18 * np.clip(kx ** 2 + ky ** 2, 0, 1.6)
    img = np.clip(img * vig[..., None], 0, 255).astype(np.uint8)
    if seed % 2 == 1:  # 奇数种子：成品横竖转置，布局彻底不同
        img = np.ascontiguousarray(img.transpose(1, 0, 2))
    return img


def save(img, folder: Path, name: str):
    folder.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(folder / name), cv2.cvtColor(img, cv2.COLOR_RGB2BGR),
                [cv2.IMWRITE_JPEG_QUALITY, 92])


def generate(root: str | Path, force: bool = False) -> tuple[Path, Path]:
    root = Path(root)
    ts = root / "testset"
    ref_dir = root / "ref"
    if (ts / "01_sharp_bright.jpg").exists() and not force:
        return ts, ref_dir / "ref_warm.jpg"

    # 每张图独立场景（种子刻意拉开，确保 dHash 互相远离）；连拍组共享 seed=99
    seeds = {"01": 2, "02": 11, "03": 20, "04": 31, "05": 43, "06": 57, "07": 85}
    scenes = {k: base_scene(seed=s) for k, s in seeds.items()}
    burst = base_scene(seed=99)
    ref_scene = base_scene(seed=77)

    save(np.clip(scenes["01"] * 1.05, 0, 255).astype(np.uint8), ts, "01_sharp_bright.jpg")
    save(np.clip(scenes["02"] * 0.30, 0, 255).astype(np.uint8), ts, "02_sharp_dark.jpg")
    save(np.clip(cv2.GaussianBlur(scenes["03"], (0, 0), 5.5) * 1.05, 0, 255).astype(np.uint8),
         ts, "03_blurry_bright.jpg")
    save(np.clip(cv2.GaussianBlur(scenes["04"], (0, 0), 6.0) * 0.38, 0, 255).astype(np.uint8),
         ts, "04_blurry_dark.jpg")

    cb = scenes["05"].astype(np.float32)
    cb[..., 0] *= 0.62
    cb[..., 2] *= 1.35
    save(np.clip(cb, 0, 255).astype(np.uint8), ts, "05_cast_blue.jpg")

    cg = scenes["06"].astype(np.float32)
    cg[..., 1] *= 1.28
    save(np.clip(cg, 0, 255).astype(np.uint8), ts, "06_cast_green.jpg")

    flat = scenes["07"].astype(np.float32) * 0.35 + 128
    save(np.clip(flat, 0, 255).astype(np.uint8), ts, "07_low_contrast.jpg")

    # 连拍 4 张：同一场景近似构图，第 2 张糊
    for i in range(1, 5):
        m = np.float32([[1, 0, i * 3], [0, 1, i]])
        shifted = cv2.warpAffine(burst, m, (burst.shape[1], burst.shape[0]))
        if i == 2:
            shifted = cv2.GaussianBlur(shifted, (0, 0), 3.5)
        save(np.clip(shifted * (1.0 + i * 0.01), 0, 255).astype(np.uint8),
             ts, f"burst_{i:02d}.jpg")

    # 肤色块"人像"：两个椭圆 + 五官暗块，供磨皮测试
    h, w = base_scene(seed=42).shape[:2]
    sk = (base_scene(seed=42).astype(np.float32) * 0.45 + 60)
    for (cx, cy, rx, ry) in [(int(w * 0.38), int(h * 0.42), 150, 200),
                             (int(w * 0.70), int(h * 0.55), 120, 160)]:
        cv2.ellipse(sk, (cx, cy), (rx, ry), 0, 0, 360, (224, 172, 138), -1)
        cv2.circle(sk, (cx - rx // 3, cy - ry // 4), 14, (90, 70, 60), -1)   # 眼
        cv2.circle(sk, (cx + rx // 3, cy - ry // 4), 14, (90, 70, 60), -1)
        cv2.ellipse(sk, (cx, cy + ry // 5), (rx // 4, ry // 12), 0, 0, 360, (150, 90, 90), -1)
    sk += np.random.default_rng(3).normal(0, 5.5, (h, w, 1))  # 皮肤噪点
    save(np.clip(sk, 0, 255).astype(np.uint8), ts, "08_portrait_skin.jpg")

    # 参考图：独立场景的暖调"落日金"版本
    wr = ref_scene.astype(np.float32)
    wr[..., 0] *= 1.30
    wr[..., 1] *= 1.04
    wr[..., 2] *= 0.62
    wr += 8
    save(np.clip(wr, 0, 255).astype(np.uint8), ref_dir, "ref_warm.jpg")
    return ts, ref_dir / "ref_warm.jpg"


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1] / "demo"
    ts, ref = generate(root, force=True)
    n = len(list(ts.glob("*.jpg")))
    print(f"生成 {n} 张测试照片于 {ts}，参考图 {ref}")
