"""NAFNet SIDD 去噪权重 → ONNX 转换脚本（一次性）。

复现 megvii-research/NAFNet 的 NAFNet 架构（width32，SIDD 去噪配置），
加载官方 .pth 权重（BasicSR 格式，params 嵌套键），导出 ONNX 供
onnxruntime 推理。本脚本只在构建期运行，运行时不需要 torch。

来源与许可：
- NAFNet: "Simple Baselines for Image Restoration" (ECCV 2022)
  github.com/megvii-research/NAFNet，MIT 代码 + MIT 权重
- 权重镜像：huggingface.co/nyanko7/nafnet-models（经 hf-mirror.com 下载）
"""
from __future__ import annotations

import sys
from pathlib import Path

import torch
import torch.nn as nn
import torch.nn.functional as F


# ---------------------------------------------------------------- 架构复现
# 忠实复现 megvii-research/NAFNet/basicsr/models/archs/NAFNet_arch.py，
# LayerNorm2d 为 channel-wise LayerNorm（带 weight+bias，eps=1e-6）。

class LayerNorm2d(nn.Module):
    def __init__(self, channels, eps=1e-6):
        super().__init__()
        self.weight = nn.Parameter(torch.ones(channels))
        self.bias = nn.Parameter(torch.zeros(channels))
        self.eps = eps

    def forward(self, x):
        # channel-wise LayerNorm：对每个 (N,C) 在空间维归一化
        mu = x.mean(1, keepdim=True)
        var = (x - mu).pow(2).mean(1, keepdim=True)
        x = (x - mu) / torch.sqrt(var + self.eps)
        return x * self.weight.view(1, -1, 1, 1) + self.bias.view(1, -1, 1, 1)


class SimpleGate(nn.Module):
    def forward(self, x):
        x1, x2 = x.chunk(2, dim=1)
        return x1 * x2


class NAFBlock(nn.Module):
    def __init__(self, c, DW_Expand=2, FFN_Expand=2, drop_out_rate=0.0):
        super().__init__()
        dw_channel = c * DW_Expand
        self.conv1 = nn.Conv2d(c, dw_channel, 1, 1, 0, groups=1, bias=True)
        self.conv2 = nn.Conv2d(dw_channel, dw_channel, 3, 1, 1, groups=dw_channel, bias=True)
        self.conv3 = nn.Conv2d(dw_channel // 2, c, 1, 1, 0, groups=1, bias=True)
        self.sca = nn.Sequential(
            nn.AdaptiveAvgPool2d(1),
            nn.Conv2d(dw_channel // 2, dw_channel // 2, 1, 1, 0, groups=1, bias=True),
        )
        self.sg = SimpleGate()
        ffn_channel = FFN_Expand * c
        self.conv4 = nn.Conv2d(c, ffn_channel, 1, 1, 0, groups=1, bias=True)
        self.conv5 = nn.Conv2d(ffn_channel // 2, c, 1, 1, 0, groups=1, bias=True)
        self.norm1 = LayerNorm2d(c)
        self.norm2 = LayerNorm2d(c)
        self.beta = nn.Parameter(torch.zeros((1, c, 1, 1)))
        self.gamma = nn.Parameter(torch.zeros((1, c, 1, 1)))

    def forward(self, inp):
        x = self.norm1(inp)
        x = self.conv1(x)
        x = self.conv2(x)
        x = self.sg(x)
        x = x * self.sca(x)
        x = self.conv3(x)
        y = inp + x * self.beta
        x = self.conv4(self.norm2(y))
        x = self.sg(x)
        x = self.conv5(x)
        return y + x * self.gamma


class NAFNet(nn.Module):
    def __init__(self, img_channel=3, width=32, middle_blk_num=12,
                 enc_blk_nums=(2, 2, 4, 8), dec_blk_nums=(2, 2, 2, 2)):
        super().__init__()
        self.intro = nn.Conv2d(img_channel, width, 3, 1, 1, groups=1, bias=True)
        self.ending = nn.Conv2d(width, img_channel, 3, 1, 1, groups=1, bias=True)

        self.encoders = nn.ModuleList()
        self.decoders = nn.ModuleList()
        self.middle_blks = nn.ModuleList()
        self.ups = nn.ModuleList()
        self.downs = nn.ModuleList()

        chan = width
        for num in enc_blk_nums:
            self.encoders.append(nn.Sequential(*[NAFBlock(chan) for _ in range(num)]))
            self.downs.append(nn.Conv2d(chan, 2 * chan, 2, 2))
            chan = chan * 2

        self.middle_blks = nn.Sequential(*[NAFBlock(chan) for _ in range(middle_blk_num)])

        for num in dec_blk_nums:
            self.ups.append(nn.Sequential(
                nn.Conv2d(chan, chan * 2, 1, bias=False),
                nn.PixelShuffle(2),
            ))
            chan = chan // 2
            self.decoders.append(nn.Sequential(*[NAFBlock(chan) for _ in range(num)]))

        self.padder_size = 2 ** len(self.encoders)

    def check_image_size(self, x):
        _, _, h, w = x.size()
        mod_pad_h = (self.padder_size - h % self.padder_size) % self.padder_size
        mod_pad_w = (self.padder_size - w % self.padder_size) % self.padder_size
        return F.pad(x, (0, mod_pad_w, 0, mod_pad_h))

    def forward(self, inp):
        B, C, H, W = inp.shape
        inp = self.check_image_size(inp)
        x = self.intro(inp)
        encs = []
        for encoder, down in zip(self.encoders, self.downs):
            x = encoder(x)
            encs.append(x)
            x = down(x)
        x = self.middle_blks(x)
        for decoder, up, enc_skip in zip(self.decoders, self.ups, encs[::-1]):
            x = up(x)
            x = x + enc_skip
            x = decoder(x)
        x = self.ending(x)
        x = x + inp
        return x[:, :, :H, :W]


def load_official_pth(path: str) -> dict:
    """加载 BasicSR 格式的 .pth，剥离 params/params_ema 前缀。"""
    raw = torch.load(path, map_location="cpu", weights_only=False)
    if isinstance(raw, dict) and "params" in raw:
        sd = raw["params"]
    elif isinstance(raw, dict) and "params_ema" in raw:
        sd = raw["params_ema"]
    else:
        sd = raw
    # 去掉可能的 "module." 前缀（DataParallel 保存）
    if all(k.startswith("module.") for k in sd):
        sd = {k[len("module."):]: v for k, v in sd.items()}
    return sd


def main():
    pth_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/NAFNet-SIDD-width32.pth"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "/tmp/nafnet_sidd_width32.onnx"

    model = NAFNet(img_channel=3, width=32, middle_blk_num=12,
                   enc_blk_nums=(2, 2, 4, 8), dec_blk_nums=(2, 2, 2, 2))
    sd = load_official_pth(pth_path)
    missing, unexpected = model.load_state_dict(sd, strict=False)
    print(f"[load] missing={len(missing)} unexpected={len(unexpected)}")
    if missing:
        print("  missing keys (前10):", missing[:10])
    if unexpected:
        print("  unexpected keys (前10):", unexpected[:10])
    assert not missing, "存在缺失权重，架构复现不匹配"

    model.eval()
    # 固定输入尺寸 256x256（与训练 patch 一致），tile 分块在推理端处理
    dummy = torch.randn(1, 3, 256, 256)
    with torch.no_grad():
        pt_out = model(dummy)

    torch.onnx.export(
        model, dummy, out_path,
        input_names=["input"], output_names=["output"],
        opset_version=17,
        do_constant_folding=True,
        dynamic_axes={"input": {2: "height", 3: "width"},
                      "output": {2: "height", 3: "width"}},
    )
    print(f"[export] 已导出 {out_path}")

    # torch 2.x 默认把大权重存到外部 .data 文件；这里内联回单文件，
    # 便于分发（打包只需一个 .onnx，onnxruntime 直接加载）。
    import onnx
    from onnx.external_data_helper import convert_model_to_external_data, \
        convert_model_from_external_data
    m = onnx.load(out_path, load_external_data=True)
    data_path = out_path + ".data"
    if Path(data_path).exists():
        convert_model_from_external_data(m)
        onnx.save(m, out_path)
        Path(data_path).unlink()
        print(f"[inline] 权重已内联，删除外部数据 {data_path}")

    # 一致性校验
    import onnx
    import onnxruntime as ort
    onnx_model = onnx.load(out_path)
    onnx.checker.check_model(onnx_model)
    sess = ort.InferenceSession(out_path, providers=["CPUExecutionProvider"])
    onnx_out = sess.run(None, {"input": dummy.numpy()})[0]
    diff = float(abs(pt_out.numpy() - onnx_out).max())
    print(f"[verify] PyTorch vs ONNX max_abs_diff = {diff:.6e}")
    assert diff < 1e-4, "ONNX 输出与 PyTorch 不一致"


if __name__ == "__main__":
    main()
