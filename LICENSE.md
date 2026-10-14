# PhotoPilot 许可与资产来源声明

> 本文件是项目许可状态的说明，不是对任何开源许可证的套用。
> 截至 2026-10-13，项目**尚未选定并公布**统一的源代码许可证。
> 在权利人明确许可策略之前，本项目应视为**保留所有权利（All Rights Reserved）**，
> 不得被他人擅自再分发或商用。

## 1. 项目自身代码

- 源码归权利人所有，当前**未开源授权**。
- 历史 README 曾出现的「MIT（建议）」措辞已废止：项目不能仅凭一句建议
  就被视为 MIT 授权。见 `docs/COMMERCIAL_USE.md`。
- 若要开源，请由权利人选择合适许可证（如 MIT / Apache-2.0 / BSD-3-Clause）
  并正式在本文件落款后再发布。

## 2. 内置预设与算法

- 内置的 30 个风格预设、白平衡、追色、磨皮、相似度等算法均为**本项目原创实现**，
  基于公开的色彩科学资料，未复制第三方 LUT、样片、预设文件或受版权保护的风格资产。
- 可复用的色彩基础设施参考了 BSD-3-Clause 的
  [OpenColorIO](https://github.com/AcademySoftwareFoundation/OpenColorIO) 与
  [Colour Science](https://github.com/colour-science/colour)，但仅参考接口/概念，
  不包含其代码，无传染义务。

## 3. 图标（icon_1024.png / PhotoPilot.icns）

- 由 **AI 生成**。
- AI 生成素材的著作权归属与可商用性目前存在不确定性；如用于商业分发，
  建议替换为权利人自绘图标，或确认所用生成工具的商用授权条款。

## 4. 示例照片

- `demo/hopper.jpg`：Grace Hopper 照片，来源
  `https://commons.wikimedia.org/wiki/File:Grace_Hopper.jpg`，公有领域，
  TensorFlow 官方亦曾用作测试图。仅作算法验证，不作为宣传人像使用。
- `demo/testset/` 等其余为脚本合成照片（`scripts/make_testset.py`），无肖像权风险。

## 5. 第三方依赖

详见 [`docs/THIRD_PARTY_LICENSES.md`](THIRD_PARTY_LICENSES.md)。关键：
rawpy 捆绑的 LibRaw 为 LGPL-2.1；mediapipe 为 Apache-2.0（含内置人脸模型）。

## 6. 商业分发前必须完成

1. 权利人确定本项目源码与资产（含图标）的授权方式。
2. ~~从 git 跟踪与历史中移除未授权的 NIMA 权重~~ ✅ 已于 2026-10-14 完成：
   未授权权重已移除，改采 idealo/image-quality-assessment 的 Apache-2.0 权重。
3. 锁定分发版本并收集全部第三方 LICENSE/NOTICE。
4. 替换或确认图标授权。
