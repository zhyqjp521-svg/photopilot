"""AI 降噪（NAFNet）回归测试；仅合成图像，绝不用真实用户照片。

覆盖：模型可用性探测、优雅降级、参数校验、强度混合、tile 分块逻辑。
真实 onnxruntime 推理依赖模型文件（不进 git），这里用 mock 验证管线契约。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from photopilot import denoise, polish


class DenoiseAvailabilityTests(unittest.TestCase):
    def test_available_returns_false_when_model_missing(self):
        """模型文件缺失时 available() 返回 False，不抛异常。"""
        with patch.object(denoise, 'MODEL_PATH', Path('/nonexistent/nafnet.onnx')):
            denoise._available = None
            self.assertFalse(denoise.available())

    def test_denoise_returns_none_when_unavailable(self):
        """模型缺失时 denoise() 优雅返回 None。"""
        img = np.zeros((64, 64, 3), dtype=np.uint8)
        with patch.object(denoise, 'available', return_value=False):
            self.assertIsNone(denoise.denoise(img))

    def test_denoise_rejects_non_uint8_rgb(self):
        """非 RGB uint8 输入应抛 ValueError。"""
        with patch.object(denoise, 'available', return_value=True):
            with self.assertRaises(ValueError):
                denoise.denoise(np.zeros((64, 64), dtype=np.uint8))  # 灰度
            with self.assertRaises(ValueError):
                denoise.denoise(np.zeros((64, 64, 3), dtype=np.float32))  # 非 uint8


class DenoiseStrengthTests(unittest.TestCase):
    def test_zero_strength_returns_original(self):
        """strength=0 直接返回原图（不触发推理）。"""
        img = np.random.default_rng(1).integers(0, 255, (32, 32, 3), dtype=np.uint8)
        out = denoise.denoise_strength(img, 0.0)
        np.testing.assert_array_equal(out, img)

    def test_unavailable_returns_none(self):
        """模型缺失时 denoise_strength 返回 None（调用方降级）。"""
        img = np.zeros((32, 32, 3), dtype=np.uint8)
        with patch.object(denoise, 'denoise', return_value=None):
            self.assertIsNone(denoise.denoise_strength(img, 1.0))

    def test_partial_strength_blends(self):
        """0 < strength < 1 时混合原图与降噪结果。"""
        img = np.full((16, 16, 3), 100, dtype=np.uint8)
        cleaned = np.full((16, 16, 3), 200, dtype=np.uint8)
        with patch.object(denoise, 'denoise', return_value=cleaned):
            out = denoise.denoise_strength(img, 0.5)
        np.testing.assert_allclose(out, 150, atol=1)


class DenoiseTileTests(unittest.TestCase):
    def _fake_run(self, tile):
        """模拟 onnxruntime：tile 全量返回自身（恒等），用于验证分块拼接。"""
        return tile

    def test_small_image_uses_single_tile(self):
        img = np.random.default_rng(2).integers(0, 255, (64, 64, 3), dtype=np.uint8)
        with patch.object(denoise, 'available', return_value=True), \
             patch.object(denoise, '_run_tile', side_effect=self._fake_run):
            out = denoise.denoise(img, tile_size=512, overlap=32)
        # 恒等推理 + [0,1] 往返，应近似还原（容许 1/255 舍入误差）
        self.assertEqual(out.shape, img.shape)
        self.assertTrue(np.abs(out.astype(int) - img.astype(int)).max() <= 1)

    def test_large_image_tiles_without_gap(self):
        img = np.random.default_rng(3).integers(0, 255, (1000, 800, 3), dtype=np.uint8)
        calls = []
        def fake(tile):
            calls.append(tile.shape)
            return tile
        with patch.object(denoise, 'available', return_value=True), \
             patch.object(denoise, '_run_tile', side_effect=fake):
            out = denoise.denoise(img, tile_size=512, overlap=64)
        self.assertEqual(out.shape, img.shape)
        # 分块应覆盖整个图像（多块）
        self.assertGreater(len(calls), 1)
        # 恒等分块 + 羽化融合应近似还原
        self.assertTrue(np.abs(out.astype(int) - img.astype(int)).max() <= 2)


class PolishParamsDenoiseTests(unittest.TestCase):
    def test_ai_denoise_defaults_to_zero(self):
        p = polish.PolishParams()
        self.assertEqual(p.ai_denoise, 0.0)

    def test_ai_denoise_validated_in_range(self):
        polish.PolishParams(ai_denoise=0.5)  # 合法
        polish.PolishParams(ai_denoise=1.0)
        with self.assertRaises(ValueError):
            polish.PolishParams(ai_denoise=-0.1)
        with self.assertRaises(ValueError):
            polish.PolishParams(ai_denoise=1.5)

    def test_polish_skips_denoise_when_model_missing(self):
        """ai_denoise>0 但模型缺失时，polish_image 优雅降级并告警。"""
        img = np.random.default_rng(4).integers(0, 255, (64, 64, 3), dtype=np.uint8)
        warnings = []
        with patch.object(denoise, 'available', return_value=False):
            out = polish.polish_image(
                img, polish.PolishParams(wb=0, skin=0, clarity=0, ai_denoise=0.8),
                faces=[], warnings=warnings)
        self.assertEqual(out.shape, img.shape)
        self.assertTrue(any('AI 降噪' in w for w in warnings))

    def test_polish_applies_denoise_when_available(self):
        """模型可用时，ai_denoise 参与处理（输出与输入不同）。"""
        img = np.random.default_rng(5).integers(50, 200, (64, 64, 3), dtype=np.uint8)
        with patch.object(denoise, 'available', return_value=True), \
             patch.object(denoise, 'denoise_strength',
                          return_value=np.clip(img.astype(np.float32) * 0.5, 0, 255).astype(np.uint8)):
            out = polish.polish_image(
                img, polish.PolishParams(wb=0, skin=0, clarity=0, ai_denoise=1.0),
                faces=[])
        self.assertFalse(np.array_equal(out, img))


if __name__ == '__main__':
    unittest.main()
