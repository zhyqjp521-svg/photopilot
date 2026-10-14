# 第三方依赖许可证清单

> 本清单记录 PhotoPilot 运行时依赖的第三方组件及其许可，供商业分发前核对。
> 生成日期：2026-10-13。版本号为本项目开发环境的实测版本；分发时请以
> 实际锁定的 wheel 为准重新导出并复核。本清单不构成法律意见。

## 直接依赖

| 组件 | 版本 | 许可证 | 分发义务 / 注意 |
|---|---|---|---|
| numpy | 1.26.4 | BSD-3-Clause | 保留版权声明与许可文本 |
| opencv-python-headless | 4.10.0.84 | Apache-2.0 | 保留 LICENSE、NOTICE |
| Pillow | 12.3.0 | MIT-CMU（HPND 变体） | 保留许可文本 |
| rawpy | 0.27.1 | MIT（自身） | 见下方 LibRaw 说明 |
| LibRaw（rawpy 捆绑） | 0.22.1 | **LGPL-2.1** | 动态链接；须提供 LibRaw 源码/目标再链接途径，保留 LICENSE.LibRaw |
| mediapipe | 0.10.21 | Apache-2.0 | 框架与 solution 均 Apache-2.0 |
| onnxruntime | 1.30.0 | MIT | 保留许可文本（随 NIMA 美学评分打包） |
| pywebview | 6.2.1 | BSD-3-Clause | 保留版权声明 |
| send2trash | （仅非 macOS） | BSD-3-Clause | 保留版权声明 |
| protobuf | 4.25.9 | BSD-3-Clause | mediapipe 传递依赖 |
| scipy | 1.17.1 | BSD-3-Clause | 传递依赖 |

## 关键说明

### 1. LibRaw（rawpy 捆绑）—— LGPL-2.1

`rawpy` 自身是 MIT，但它是对 **LibRaw** 库的封装，而 LibRaw 是 **LGPL-2.1**。
PyPI 官方 wheel 明确**不包含** GPL2/GPL3 的 Demosaic Pack（因 MIT 与 GPL 不兼容），
因此本项目的 RAW 解码不涉及 GPL 传染。但 **LGPL-2.1 仍要求**：

- 保留 LibRaw 的 `LICENSE.LibRaw` 文本（随 rawpy wheel 一并分发）；
- 允许用户以修改后的 LibRaw 重新链接本项目（动态链接一般可满足）。

分发 exe 时，应将 rawpy/LibRaw 的许可文本一并放入 `THIRD_PARTY_LICENSES` 目录。

### 2. MediaPipe 内置模型

MediaPipe 框架及 Python solution 均为 Apache-2.0。人脸检测所用的
`face_detection_short_range` 图与内置 tflite 模型随 mediapipe 包以 Apache-2.0
分发，可随软件打包。打包时通过 `--collect-all mediapipe` 收集其数据文件。

### 3. onnxruntime —— 随 NIMA 美学评分打包

`onnxruntime`（MIT）用于 NIMA 美学评分推理。构建脚本通过
`--collect-all onnxruntime` 将其原生库与模块一并打包。分发时保留其 MIT 许可文本。

### 4. NIMA 美学权重 —— Apache-2.0（已核实）

美学评分模型权重来自 **idealo/image-quality-assessment**
（https://github.com/idealo/image-quality-assessment），仓库明确 **Apache-2.0** 许可。

- 权重：`weights_mobilenet_aesthetic_0.07.hdf5`（MobileNet 骨干，AVA 数据集训练，
  性能 EMD 0.071 / LCC 0.626 / SRCC 0.609）。
- 本项目由 `scripts/` 一次性脚本将其转为 ONNX
  （`models/nima_mobilenet_aesthetic_apache.onnx`，约 12.9MB），供 `aesthetic.py` 推理。
- `MODEL_LICENSE_VERIFIED=True`，`--ai` 与前端「美学模型」开关已恢复。
- 分发时保留 idealo 仓库的 Apache-2.0 LICENSE 与 NOTICE（如存在）。

> 历史遗留：早期曾提交一个来源不明的 NIMA ONNX（HuggingFace `cromsc/nima-mobilenet-aesthetic`，
> 无明确许可），已于 2026-10-14 从工作区与 git 历史中彻底移除，由上述 Apache-2.0 权重取代。

## 打包时的动作清单

1. 锁定实际分发版本，导出递归依赖清单（`pip freeze` / `pip-licenses`）。
2. 收集每个直接依赖的 LICENSE / NOTICE，放入分发包的
   `THIRD_PARTY_LICENSES/` 目录，与 exe 一并交付。
3. 确认 `rawpy` 的 `LICENSE.LibRaw`（LGPL-2.1）在包内。
4. 确认最终 exe 不含 `models/`、`demo/` 测试照片与个人照片。
