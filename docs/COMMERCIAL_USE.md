# 商业使用与能力对标记录

检查日期：2026-09-30。此文件记录本次核实范围，不是整套产品已完成法律审计的证明。

## 本次取舍

- 对标像素蛋糕的工作流与公开能力，不复制它的品牌、界面素材、商业预设或私有实现。
- 本次新增基础调色、同步对比平移、原图上传、高清导出、自定义 JSON 参数由本项目实现，
  未下载第三方 LUT、样片或模型。
- 原有 `models/nima_mobilenet_aesthetic.onnx` 的分发页没有模型卡或明确权重许可证。
  `MODEL_LICENSE_VERIFIED=False` 阻止默认加载；本机文件保留以免丢失用户数据。
  `MANIFEST.in` 排除模型目录。Git 历史曾包含该文件，商业分发不能直接复制整仓或历史。
- 没有把项目擅自改为 MIT；原 README 的“MIT（建议）”和“依赖全部商用友好”已更正。

来源：[旧 NIMA 分发页](https://huggingface.co/cromsc/nima-mobilenet-aesthetic)、
[公开产品能力](https://www.pixcakes.com/)。

## 后续模型筛选

| 项目 | 已核实的公开许可信息 | 当前决策 |
|---|---|---|
| CodeFormer | S-Lab 1.0 非商用；商业用途需联系贡献者 | 不纳入默认商业构建 |
| GFPGAN | 主体 Apache-2.0，但许可文件包含第三方及非商用组件说明 | 不能仅凭仓库顶层 Apache 标记判定全部可商用，未接入 |
| Restormer | 仓库代码 MIT | 候选去噪/去模糊；具体权重与数据仍待核实 |
| NAFNet | 仓库许可及第三方组件需按所选版本核实 | 候选去噪/去模糊；具体权重与数据仍待核实 |

来源：[CodeFormer LICENSE](https://github.com/sczhou/CodeFormer/blob/master/LICENSE)、
[GFPGAN LICENSE](https://github.com/TencentARC/GFPGAN/blob/master/LICENSE)、
[Restormer LICENSE](https://github.com/swz30/Restormer/blob/main/LICENSE.md)、
[NAFNet](https://github.com/megvii-research/NAFNet)。没有下载或整合这些项目。

## 商业打包前检查

1. 由权利人确定本项目源码与资产授权，核实已有预设/图标等资产来源，不能用功能描述替代授权。
2. 锁定实际分发版本，导出 Python、原生库及其递归依赖的清单；保留各自 LICENSE、NOTICE。
3. rawpy 自身 [MIT](https://github.com/letmaik/rawpy/blob/main/LICENSE)，但捆绑的 LibRaw、可选组件
   许可须按实际 wheel 单独处理，不能写成“动态链接就无条件可商用”。
4. MediaPipe 内置模型、ONNX 权重、训练数据、样片分别记录来源、版本/哈希、授权和分发义务。
5. 测试照片不是可随意作为宣传人像使用的资产；默认发行包不要混入测试集和个人照片。
6. 安装脚本当前是本机路径启动器，不是可移交给其他机器的独立签名发行版。

## 功能边界与下一步

当前已经支持：30 个已有算法风格、批量追色、自动曝光、关键点皮肤/眼睛掩码、传统保纹理修复、
连拍分组、四图同步缩放/平移与加星、单图基础调色、原始尺寸导出、原创参数保存/载入。
这不是像素蛋糕等商业产品的完整替代。

下一步优先：16-bit/ICC/RAW 显影参数 → 可撤销局部画笔 → 去噪/去模糊后端 → 经授权的人脸模型。
每个深度后端分别核实代码、权重和附属组件；未经核实的资源不默认启用、不打包。
