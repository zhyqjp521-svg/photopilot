"""追色模块：把一张/一批照片的色彩分布对齐到参考图或风格预设。

算法（自研实现，参考 hahnec/color-matcher 与 dstein64/colortrans）：
- reinhard   ：LAB 空间均值/方差匹配（经典 Reinhard et al. 2001）
- oklab      ：OKLab 空间均值/方差匹配（默认，感知均匀，肤色偏移更自然）
- mkl        ：RGB 空间高斯最优传输线性映射（Monge-Kantorovich，保留协方差结构）
- histogram  ：逐通道分位数（CDF）匹配
- luma_hist  ：仅亮度做 CDF 匹配（只压调性不动色相）

扩展（上游没有的）：
- strength    强度混合，0=原图 1=完全匹配
- preserve_luma 只迁移色度、保留原亮度（适合"套色调不改曝光"）
- skin_protect 肤色区域按掩码衰减迁移量，避免人脸被参考图色调带偏
- 风格预设    以「相对偏移」定义（dL/da/db），对任意源图都稳定，
  比 color-matcher 的绝对目标统计更不容易翻车
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from .io import load_image, resize_max
from .colorops import (rgb_to_oklab, rgb_u8_to_oklab, oklab_to_rgb,
                       oklab_to_linear_rgb, linear_to_srgb, skin_mask)

STATS_PIXELS = 250_000  # 统计采样上限


# ------------------------------------------------------------ 统计与工具

def _sample(img: np.ndarray, n: int = STATS_PIXELS) -> np.ndarray:
    """对 (...,C) 数组均匀步长采样至多 n 行（确定性、零随机开销）。"""
    flat = img.reshape(-1, img.shape[-1])
    if len(flat) > n:
        flat = flat[::max(1, len(flat) // n)]
    return flat


def _stats_from_lab(lab: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """从已算好的全图 OKLab 上取步长子视图统计——近乎免费。"""
    flat = lab.reshape(-1, 3)
    sub = flat[::max(1, len(flat) // STATS_PIXELS)]
    return sub.mean(0), sub.std(0)


def oklab_stats(img_rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """OKLab 均值/标准差。先采样后转换：只对 ≤25 万像素做色彩空间变换。"""
    lab = rgb_u8_to_oklab(_sample(img_rgb))
    return lab.mean(0), lab.std(0)


def _oklab_to_rgb_gamut(lab: np.ndarray) -> np.ndarray:
    """OKLab → sRGB：出界像素沿色度轴向亮度轴精确收缩，保亮度不偏色相。"""
    from .colorops import gamut_compress
    lin = oklab_to_linear_rgb(lab)
    return linear_to_srgb(gamut_compress(lin))


def _match_mean_std(src: np.ndarray, mu_s, sd_s, mu_t, sd_t) -> np.ndarray:
    return (src - mu_s) / (sd_s + 1e-6) * (sd_t + 1e-6) + mu_t


def _sqrtm_psd(m: np.ndarray, power: float = 0.5) -> np.ndarray:
    """对称 PSD 矩阵的分数次幂（eigh 实现，负特征值截断）。"""
    w, v = np.linalg.eigh((m + m.T) / 2)
    w = np.clip(w, 0, None) ** power
    return (v * w) @ v.T


def _mkl(img, ref, reference) -> np.ndarray:
    # 参考侧 (mu_t, cov_t) 只算一次；源侧随图必算
    if "mkl_target" not in reference._cache:
        t = _sample(ref.astype(np.float32) / 255.0)
        reference._cache["mkl_target"] = (t.mean(0), np.cov(t.T) + 1e-6 * np.eye(3))
    mu_t, cov_t = reference._cache["mkl_target"]
    s_lin = img.astype(np.float32) / 255.0
    A, b = _mkl_map_src(_sample(s_lin.reshape(-1, 3)), mu_t, cov_t)
    out = s_lin.reshape(-1, 3) @ A.T + b
    out = np.clip(out.reshape(s_lin.shape), 0, 1) * 255.0
    return out


def _mkl_map_src(src_lin: np.ndarray, mu_t, cov_t) -> tuple[np.ndarray, np.ndarray]:
    mu_s = src_lin.mean(0)
    cov_s = np.cov(src_lin.T) + 1e-6 * np.eye(3)
    is_sqrt = _sqrtm_psd(cov_s)
    mid = _sqrtm_psd(is_sqrt @ cov_t @ is_sqrt)
    A = np.linalg.solve(is_sqrt, mid @ np.linalg.inv(is_sqrt))
    b = mu_t - A @ mu_s
    return A, b


def _cdf_match(src: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """一维分位数映射：把 src 的经验分布映射到 ref。"""
    s_sorted = np.sort(src.ravel())
    t_sorted = np.sort(ref.ravel())
    n = len(s_sorted)
    t_q = np.interp(np.linspace(0, 1, n), np.linspace(0, 1, len(t_sorted)), t_sorted)
    order = np.argsort(src.ravel(), kind="stable")
    out = np.empty(n, np.float32)
    out[order] = t_q.astype(np.float32)
    return out.reshape(src.shape)


# ------------------------------------------------------------ 风格预设

# 相对偏移（OKLab 单位）：dL 亮度、da 绿↔红、db 蓝↔黄；sat = a/b 方差倍率
PRESETS: dict[str, dict] = {
    # These looks are original OKLab offsets, not copied LUT tables or bundled
    # image assets.  They are intentionally restrained so they survive mixed
    # camera profiles and remain safe for commercial deliverables.
    "natural":      {"dL": +0.008, "da": +0.002, "db": +0.004, "sat": 0.96},  # 自然校正
    "portrait_soft":{"dL": +0.018, "da": +0.010, "db": +0.018, "sat": 0.86},  # 人像柔和
    "travel_vibrant":{"dL": +0.006, "da": +0.012, "db": +0.020, "sat": 1.12},  # 旅行鲜明
    "mono_contrast": {"dL": +0.000, "da": 0.000, "db": 0.000, "sat": 0.02, "contrast": 1.15},  # 黑白高反差
    "film_warm":   {"dL": +0.020, "da": +0.022, "db": +0.045, "sat": 0.92},  # 温润胶片
    "clean_cool":  {"dL": +0.025, "da": -0.015, "db": -0.028, "sat": 0.85},  # 清爽冷调
    "moody_teal":  {"dL": -0.030, "da": -0.020, "db": -0.010, "sat": 1.05},  # 暗调青橙
    "sunset_gold": {"dL": +0.010, "da": +0.035, "db": +0.065, "sat": 1.10},  # 落日金
    # 场景预设：只用 OKLab 相对参数与曲线，不依赖 LUT 或第三方素材。
    "wedding_air":  {"dL": +0.040, "da": +0.006, "db": +0.014, "sat": 0.78, "contrast": 0.90, "lift": +0.020},
    "skin_glow":    {"dL": +0.028, "da": +0.018, "db": +0.022, "sat": 0.90, "contrast": 0.96, "lift": +0.010},
    "pastel_matte":  {"dL": +0.020, "da": +0.008, "db": +0.010, "sat": 0.72, "contrast": 0.80, "lift": +0.035},
    "golden_hour":  {"dL": +0.015, "da": +0.026, "db": +0.050, "sat": 0.98, "contrast": 1.02, "lift": +0.005},
    "street_neon":  {"dL": -0.010, "da": +0.012, "db": -0.020, "sat": 1.22, "contrast": 1.12},
    "forest_deep":   {"dL": -0.018, "da": -0.010, "db": +0.015, "sat": 1.08, "contrast": 1.06, "lift": -0.003},
    "ocean_air":     {"dL": +0.015, "da": -0.018, "db": -0.045, "sat": 1.00, "contrast": 1.00, "lift": +0.005},
    "cinematic_night":{"dL": -0.055, "da": -0.015, "db": -0.018, "sat": 0.92, "contrast": 1.15, "lift": -0.005},
    "retro_fade":    {"dL": +0.005, "da": +0.015, "db": +0.025, "sat": 0.68, "contrast": 0.82, "lift": +0.045},
    "bw_soft":       {"dL": +0.005, "da": 0.000, "db": 0.000, "sat": 0.02, "contrast": 0.88, "lift": +0.020},
    # 婚礼 / 日系 / 商业场景：仍是相对参数，批量套用时不依赖某一台相机的绝对曝光。
    "wedding_airy":   {"dL": +0.052, "da": +0.004, "db": +0.018, "sat": 0.76, "contrast": 0.86, "lift": +0.026},
    "wedding_blush":  {"dL": +0.030, "da": +0.020, "db": +0.028, "sat": 0.84, "contrast": 0.92, "lift": +0.014},
    "japanese_fresh": {"dL": +0.034, "da": -0.004, "db": +0.006, "sat": 0.82, "contrast": 0.90, "lift": +0.018},
    "japanese_milk":  {"dL": +0.046, "da": +0.006, "db": +0.012, "sat": 0.68, "contrast": 0.78, "lift": +0.040},
    "korean_cream":   {"dL": +0.038, "da": +0.012, "db": +0.020, "sat": 0.74, "contrast": 0.84, "lift": +0.030},
    "indoor_luminous":{"dL": +0.028, "da": +0.004, "db": +0.010, "sat": 0.88, "contrast": 0.94, "lift": +0.012},
    "outdoor_clean":  {"dL": +0.018, "da": -0.010, "db": -0.018, "sat": 0.96, "contrast": 1.04, "lift": +0.004},
    "golden_sunset":  {"dL": +0.012, "da": +0.030, "db": +0.058, "sat": 1.04, "contrast": 1.00, "lift": +0.008},
    "forest_story":   {"dL": -0.010, "da": -0.014, "db": +0.010, "sat": 1.04, "contrast": 1.02, "lift": +0.002},
    "ocean_breeze":   {"dL": +0.024, "da": -0.022, "db": -0.052, "sat": 0.94, "contrast": 1.00, "lift": +0.010},
    "night_city":     {"dL": -0.038, "da": -0.010, "db": -0.026, "sat": 1.14, "contrast": 1.10, "lift": -0.004},
    "retro_album":    {"dL": +0.010, "da": +0.018, "db": +0.030, "sat": 0.62, "contrast": 0.78, "lift": +0.052},
}

PRESET_META: dict[str, dict] = {
    "natural": {"label": "自然校正", "description": "轻微提亮、保留肤色与现场色彩", "category": "基础"},
    "portrait_soft": {"label": "人像柔和", "description": "低饱和暖肤，适合人像与婚礼", "category": "人像 / 婚礼"},
    "travel_vibrant": {"label": "旅行鲜明", "description": "提高色彩分离度，保留高光", "category": "风光 / 街拍"},
    "mono_contrast": {"label": "黑白高反差", "description": "去色并强化明暗层次", "category": "胶片 / 黑白"},
    "film_warm": {"label": "温润胶片", "description": "暖色、低饱和的胶片感", "category": "胶片 / 黑白"},
    "clean_cool": {"label": "清爽冷调", "description": "冷静通透，适合城市与室内", "category": "基础"},
    "moody_teal": {"label": "暗调青橙", "description": "压暗氛围并增加青色层次", "category": "风光 / 街拍"},
    "sunset_gold": {"label": "落日金", "description": "增强夕阳金色与暖色分离", "category": "风光 / 街拍"},
    "wedding_air": {"label": "婚礼通透", "description": "提亮高光、柔化对比，适合婚礼与室内人像", "category": "人像 / 婚礼"},
    "skin_glow": {"label": "肤色发光", "description": "暖肤但不泛红，保留面部层次", "category": "人像 / 婚礼"},
    "pastel_matte": {"label": "粉彩哑光", "description": "低对比、轻褪色，适合生活方式照片", "category": "人像 / 婚礼"},
    "golden_hour": {"label": "金色时刻", "description": "强化夕阳暖光与层次", "category": "风光 / 街拍"},
    "street_neon": {"label": "街头霓虹", "description": "高饱和冷暖分离，适合夜景街拍", "category": "风光 / 街拍"},
    "forest_deep": {"label": "森林深绿", "description": "压暗背景、保留绿色厚度", "category": "风光 / 街拍"},
    "ocean_air": {"label": "海风蓝", "description": "清透冷蓝，适合海边与城市天空", "category": "风光 / 街拍"},
    "cinematic_night": {"label": "电影夜色", "description": "深暗对比与轻微青色氛围", "category": "风光 / 街拍"},
    "retro_fade": {"label": "复古褪色", "description": "抬黑、低饱和，保留胶片旧相册感", "category": "胶片 / 黑白"},
    "bw_soft": {"label": "柔和黑白", "description": "低对比黑白，适合人像与纪实", "category": "胶片 / 黑白"},
    "wedding_airy": {"label": "婚礼空气感", "description": "高光通透、低对比，适合白纱与室内婚礼", "category": "人像 / 婚礼"},
    "wedding_blush": {"label": "婚礼蜜桃", "description": "轻暖蜜桃肤色，适合仪式与合影", "category": "人像 / 婚礼"},
    "japanese_fresh": {"label": "日系清新", "description": "提亮、低饱和、偏青绿，适合旅拍与生活照", "category": "人像 / 婚礼"},
    "japanese_milk": {"label": "日系奶油", "description": "柔和抬黑与奶油高光，适合写真与室内人像", "category": "人像 / 婚礼"},
    "korean_cream": {"label": "韩式奶油", "description": "柔亮暖肤、轻微褪色，适合肖像与商业人像", "category": "人像 / 婚礼"},
    "indoor_luminous": {"label": "室内明亮", "description": "压制混合光偏色，保留室内肤色和细节", "category": "人像 / 婚礼"},
    "outdoor_clean": {"label": "户外通透", "description": "清理灰雾、保留天空层次，适合户外人像", "category": "风光 / 街拍"},
    "golden_sunset": {"label": "夕阳电影", "description": "加强金色逆光与暖色层次，适合婚纱外景", "category": "风光 / 街拍"},
    "forest_story": {"label": "森林故事", "description": "深绿背景与柔和肤色分离，适合森系写真", "category": "风光 / 街拍"},
    "ocean_breeze": {"label": "海边清蓝", "description": "清透蓝调与柔和高光，适合海边旅拍", "category": "风光 / 街拍"},
    "night_city": {"label": "城市夜景", "description": "冷暖霓虹分离，保留夜景暗部质感", "category": "风光 / 街拍"},
    "retro_album": {"label": "旧相册胶片", "description": "抬黑、低饱和、暖色偏移，适合纪实与复古婚礼", "category": "胶片 / 黑白"},
}

# 预设不只是色彩，也给出一组保守的人像处理建议；用户仍可在 UI 滑杆上覆盖。
_PRESET_POLISH_DEFAULTS = {
    "portrait_soft": {"skin": .42, "retain": .62, "clarity": .18, "wb": .24, "face_repair": .24, "blemish": .16, "local_region": "skin"},
    "wedding_air": {"skin": .46, "retain": .60, "clarity": .16, "wb": .26, "face_repair": .26, "blemish": .20, "local_region": "skin"},
    "skin_glow": {"skin": .44, "retain": .58, "clarity": .18, "wb": .25, "face_repair": .30, "blemish": .22, "local_region": "skin"},
    "pastel_matte": {"skin": .38, "retain": .64, "clarity": .12, "wb": .22, "face_repair": .22, "blemish": .14, "local_region": "face"},
    "wedding_airy": {"skin": .48, "retain": .58, "clarity": .16, "wb": .28, "face_repair": .28, "blemish": .22, "local_region": "skin"},
    "wedding_blush": {"skin": .52, "retain": .54, "clarity": .18, "wb": .30, "face_repair": .34, "blemish": .28, "local_region": "skin"},
    "japanese_fresh": {"skin": .34, "retain": .70, "clarity": .22, "wb": .20, "face_repair": .20, "blemish": .12, "local_region": "face"},
    "japanese_milk": {"skin": .40, "retain": .62, "clarity": .14, "wb": .24, "face_repair": .26, "blemish": .18, "local_region": "skin"},
    "korean_cream": {"skin": .46, "retain": .56, "clarity": .16, "wb": .25, "face_repair": .30, "blemish": .24, "local_region": "skin"},
    "indoor_luminous": {"skin": .38, "retain": .66, "clarity": .20, "wb": .45, "face_repair": .22, "blemish": .16, "local_region": "face"},
    "outdoor_clean": {"skin": .28, "retain": .74, "clarity": .34, "wb": .18, "face_repair": .16, "blemish": .10, "local_region": "all"},
    "golden_sunset": {"skin": .32, "retain": .70, "clarity": .24, "wb": .20, "face_repair": .18, "blemish": .12, "local_region": "face"},
    "forest_story": {"skin": .34, "retain": .68, "clarity": .24, "wb": .22, "face_repair": .20, "blemish": .16, "local_region": "face"},
    "ocean_breeze": {"skin": .26, "retain": .76, "clarity": .28, "wb": .16, "face_repair": .14, "blemish": .08, "local_region": "all"},
    "night_city": {"skin": .22, "retain": .80, "clarity": .30, "wb": .32, "face_repair": .24, "blemish": .12, "local_region": "face"},
    "retro_album": {"skin": .30, "retain": .72, "clarity": .10, "wb": .24, "face_repair": .18, "blemish": .10, "local_region": "face"},
}
for _name, _tuning in _PRESET_POLISH_DEFAULTS.items():
    PRESET_META[_name]["polish"] = _tuning


# ------------------------------------------------------------ 参考来源

@dataclass
class ColorReference:
    kind: str                      # "image" | "preset" | "stats"
    image: np.ndarray | None = None    # 参考图（RGB uint8）
    preset: str | None = None
    stats: tuple[np.ndarray, np.ndarray] | None = None  # (mean, std) OKLab，"stats" 模式用
    # 派生统计缓存：同一参考批量处理 N 张照片时只算一次（追色的主要热点）
    _cache: dict = field(default_factory=dict, repr=False, compare=False)


def reference_from_image(path_or_arr) -> ColorReference:
    if isinstance(path_or_arr, np.ndarray):
        img = path_or_arr
    else:
        img = load_image(path_or_arr, max_size=1024)
    return ColorReference(kind="image", image=img)


def reference_from_preset(name: str) -> ColorReference:
    if name not in PRESETS:
        raise ValueError(f"未知预设 {name}，可选：{list(PRESETS)}")
    return ColorReference(kind="preset", preset=name)


def cluster_scenes(mus: np.ndarray, k: int = 4, iters: int = 25
                   ) -> tuple[np.ndarray, np.ndarray]:
    """对一批 OKLab 均值向量做 k-means++（固定种子，结果确定）。

    用途：多参考图追色——把整批按色彩特征（明暗/冷暖/场景）聚成 ≤k 组，
    每组内部各自求参考统计，避免"逆光和顺光被同一个参考拉偏"。
    返回 (labels, centers)；空簇会被重指到距所属中心最远的点。
    """
    X = np.asarray(mus, np.float32)
    n = len(X)
    k = max(1, min(int(k), n))
    rng = np.random.default_rng(42)
    # k-means++ 初始化
    centers = [X[int(rng.integers(n))]]
    for _ in range(1, k):
        d2 = ((X[:, None, :] - np.stack(centers)[None]) ** 2).sum(-1).min(1)
        p = d2 / d2.sum() if d2.sum() > 1e-12 else np.full(n, 1.0 / n)
        centers.append(X[int(rng.choice(n, p=p))])
    C = np.stack(centers)
    labels = ((X[:, None, :] - C[None]) ** 2).sum(-1).argmin(1)
    for _ in range(iters):
        new = ((X[:, None, :] - C[None]) ** 2).sum(-1).argmin(1)
        if (new == labels).all():
            break
        labels = new
        for j in range(k):
            m = labels == j
            if m.any():
                C[j] = X[m].mean(0)
    # 空簇修复：中心重指到最远点
    for j in range(k):
        if not (labels == j).any():
            d = ((X - C[labels]) ** 2).sum(1)
            far = int(d.argmax())
            labels[far] = j
            C[j] = X[far]
    return labels, C


def reference_from_stats(mean: np.ndarray, std: np.ndarray) -> ColorReference:
    """auto 模式：把一批已选照片的平均色彩统计作为参考（批量统一色调）。"""
    return ColorReference(kind="stats", stats=(np.asarray(mean, np.float32),
                                               np.asarray(std, np.float32)))


# ------------------------------------------------------------ 主入口

def _ref_cache(reference: ColorReference, key: str, compute):
    if key not in reference._cache:
        reference._cache[key] = compute()
    return reference._cache[key]


def match_color(img_rgb: np.ndarray,
                reference: ColorReference,
                algo: str = "oklab",
                strength: float = 1.0,
                preserve_luma: bool = False,
                skin_protect: bool | None = None,
                faces: list | None = None) -> np.ndarray:
    """把 img_rgb 追色到 reference；返回 RGB uint8。

    strength=0 恒等返回；strength∈(0,1] 线性混合。
    skin_protect=None 时自动：检出人脸则保护肤色，无脸则全图迁移。
    参考侧统计在 reference 对象上缓存，批量处理 N 张只算一次。
    """
    img = img_rgb
    if strength <= 0:
        return img.copy()
    if skin_protect is None:
        skin_protect = bool(faces)

    if reference.kind == "preset":
        return _apply_preset(img, PRESETS[reference.preset], strength,
                             preserve_luma, skin_protect, faces)

    if reference.kind == "stats":
        ref_mean, ref_std = reference.stats
        return _match_oklab(img, ref_mean, ref_std, strength, preserve_luma, skin_protect, faces)

    ref = reference.image
    if algo == "reinhard":
        out = _reinhard_cv(img, ref, reference)
    elif algo == "oklab":
        mu_t, sd_t = _ref_cache(reference, "oklab", lambda: oklab_stats(ref))
        out = _match_oklab(img, mu_t, sd_t, 1.0, preserve_luma, skin_protect, faces)
    elif algo == "mkl":
        out = _mkl(img, ref, reference)
    elif algo == "histogram":
        out = _histogram(img, ref, reference)
    elif algo == "luma_hist":
        out = _luma_hist(img, ref, reference)
    else:
        raise ValueError(f"未知算法 {algo}，可选 reinhard/oklab/mkl/histogram/luma_hist")

    return _blend(img, out, strength)


def _blend(src: np.ndarray, dst: np.ndarray, strength: float) -> np.ndarray:
    if strength >= 1.0:
        return np.clip(dst, 0, 255).astype(np.uint8)
    out = src.astype(np.float32) * (1 - strength) + dst * strength
    return np.clip(out, 0, 255).astype(np.uint8)


def _reinhard_cv(img: np.ndarray, ref: np.ndarray, reference) -> np.ndarray:
    """经典 Reinhard：OpenCV LAB（L∈[0,255]）均值/方差匹配（参考侧缓存）。"""
    mu_t, sd_t = _ref_cache(reference, "lab_stats", lambda: _lab_stats(ref))
    s = cv2.cvtColor(img, cv2.COLOR_RGB2LAB).astype(np.float32)
    flat = _sample(s)
    mu_s, sd_s = flat.mean(0), flat.std(0)
    out = _match_mean_std(s, mu_s, sd_s, mu_t, sd_t)
    return cv2.cvtColor(np.clip(out, 0, 255).astype(np.uint8), cv2.COLOR_LAB2RGB)


def _lab_stats(img: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    lab = cv2.cvtColor(img, cv2.COLOR_RGB2LAB).astype(np.float32)
    flat = _sample(lab)
    return flat.mean(0), flat.std(0)


def _oklab_finish(img, matched, strength, preserve_luma, skin_protect, faces, lab=None):
    """OKLab 匹配结果的公共收尾：保亮度 → 色域压缩 → 肤色保护 → 强度混合。"""
    if preserve_luma:
        matched = matched.copy()
        if lab is None:
            lab = rgb_to_oklab(img.astype(np.float32) / 255.0)
        matched[..., 0] = lab[..., 0]
    out = (np.clip(_oklab_to_rgb_gamut(matched), 0, 1) * 255).astype(np.float32)
    if skin_protect and strength > 0:
        # faces: (x,y,w,h) 列表，与 img 同一分辨率
        m = skin_mask(img.astype(np.uint8), faces)[..., None]
        out = m * img + (1 - m) * out
    # Preset/OKLab paths must match the public color API contract: callers and
    # the JPEG/PNG writers expect RGB uint8, never an intermediate float array.
    return np.clip(out, 0, 255).astype(np.uint8) if strength >= 1.0 else _blend(img, out, strength)


def _match_oklab(img, ref_mean, ref_std, strength, preserve_luma, skin_protect, faces) -> np.ndarray:
    lab = rgb_u8_to_oklab(img)                 # uint8 LUT 快速路径
    mu_s, sd_s = _stats_from_lab(lab)          # 复用全图 lab，统计近乎免费
    matched = _match_mean_std(lab, mu_s, sd_s, ref_mean, ref_std)
    return _oklab_finish(img, matched, strength, preserve_luma, skin_protect, faces, lab=lab)


def _apply_preset(img, p: dict, strength, preserve_luma, skin_protect=False, faces=None) -> np.ndarray:
    lab = rgb_u8_to_oklab(img)
    mu_s, sd_s = _stats_from_lab(lab)
    mu_t = mu_s + np.array([p["dL"], p["da"], p["db"]], np.float32)
    sd_t = sd_s * np.array([1.0, p["sat"], p["sat"]], np.float32)
    matched = _match_mean_std(lab, mu_s, sd_s, mu_t, sd_t)
    # 在 OKLab L 轴上做轻量 S 曲线控制。contrast/lift 是可选参数，
    # 保持旧预设的行为不变，同时让“哑光/电影/通透”能拉开层次差异。
    contrast = float(p.get("contrast", 1.0))
    lift = float(p.get("lift", 0.0))
    if contrast != 1.0 or lift:
        matched = matched.copy()
        matched[..., 0] = (matched[..., 0] - 0.5) * contrast + 0.5 + lift
    return _oklab_finish(img, matched, strength, preserve_luma, skin_protect, faces, lab=lab)


def _histogram(img, ref, reference) -> np.ndarray:
    # 参考通道的有序序列缓存（排序是大头）
    if "sorted_rgb" not in reference._cache:
        reference._cache["sorted_rgb"] = [
            np.sort(ref[..., c].astype(np.float32).ravel()) for c in range(3)]
    out = np.empty_like(img, np.float32)
    for c in range(3):
        out[..., c] = _cdf_match(img[..., c].astype(np.float32),
                                 reference._cache["sorted_rgb"][c])
    return out


def _luma_hist(img, ref, reference) -> np.ndarray:
    """只对齐亮度分布：LAB 的 L 通道做 CDF 匹配，色度保持不变。"""
    if "sorted_L" not in reference._cache:
        t = cv2.cvtColor(ref, cv2.COLOR_RGB2LAB).astype(np.float32)
        reference._cache["sorted_L"] = np.sort(t[..., 0].ravel())
    s = cv2.cvtColor(img, cv2.COLOR_RGB2LAB).astype(np.float32)
    s[..., 0] = _cdf_match(s[..., 0], reference._cache["sorted_L"])
    return cv2.cvtColor(np.clip(s, 0, 255).astype(np.uint8), cv2.COLOR_LAB2RGB)
