# PhotoPilot —— 照片筛选 / 追色 / 美化 流水线

全本地运行的批量照片处理工具：**AI 自动筛选（神经网络美学评分 + 眨眼检测）→
批量白平衡 → 追色统一色调 → 磨皮美化 → 导出成品 + 报告**。无云端、无订阅、
CPU 即可运行，依赖全部商用友好。提供 **macOS 桌面软件**（双击图标、原生窗口）、
本地 Web UI 与 CLI 三种用法。

## 快速开始

```bash
cd photopilot
python3 -m venv .venv && .venv/bin/pip install -e .        # 或直接用现成 .venv

# 0) macOS 桌面软件（推荐）：双击 /Applications/PhotoPilot.app，
#    或命令行启动原生窗口（WKWebView，非浏览器标签页）：
photopilot app /path/to/photos         # 重建：.venv/bin/python scripts/build_app.py
# 桌面模式同一用户只保留一个运行实例；重复启动会提示且不会再创建窗口。

# 0b) Web UI：浏览器里浏览缩略图、调参数、拖拽对比前后
photopilot ui /path/to/photos          # 自动打开 http://127.0.0.1:8618

#    快审模式：网格里双击任意照片进入大图灯箱
#    ←/→ 或 J/K 切换 · 空格 加入/移出保留 · 0-5 打星级（写 XMP）· Esc 退出

# 0c) AI 筛选：工具栏开「AI 美学」后扫描，点「AI 智能筛选」一键保留
#    （NIMA 神经网络美学评分融入综合分，模型 12.8MB 已随项目内置）

# 1) 批量打分筛选（连拍去重 + 眨眼检测 + AI 美学评分，可写星级）
photopilot cull  ./photos --top 30 --ai --write-xmp

# 1b) 批量白平衡：整批统一色温直接导出（不做追色/磨皮）
photopilot wb ./photos --strength 0.9 --out-dir ./out

# 2) 追色：对齐到参考图，或用内置风格预设
photopilot color ./photos/*.jpg --ref ref.jpg --algo oklab --strength 0.9
photopilot color ./photos/*.jpg --preset film_warm

# 3) 美化：自动白平衡 + 引导滤波磨皮 + 局部清晰度 + 眼部提亮
photopilot polish ./faces/*.jpg --skin 0.7

# 3b) 多参考追色：auto 模式按场景聚 ≤4 组（逆光/顺光/夜景…）独立匹配，
#     混拍批次不再被单一参考拉偏（CLI 使用 --scene-ref）
photopilot run ./mixed/ --scene-ref --ai --wb-mode batch

# 4) 一条龙：筛选 → 追色 → 美化 → 导出 + 对比小样 + JSON 报告
photopilot run ./photos --preset film_warm --top 20 --out ./out --write-xmp --ai --wb-mode batch
```

`run` 的追色参考三选一：`--ref 参考图` / `--preset 风格预设` / 都不给则
**auto 模式**（把本批入选照片的平均色调作为参考，整批统一——适合活动、
婚礼整组出片）。预设按场景分组：基础（`natural` 自然校正、`clean_cool` 清爽冷调）；
人像 / 婚礼（`portrait_soft` 人像柔和、`wedding_air` 婚礼通透、`skin_glow` 肤色发光、
`pastel_matte` 粉彩哑光）；风光 / 街拍（`travel_vibrant` 旅行鲜明、`sunset_gold` 落日金、
`golden_hour` 金色时刻、`street_neon` 街头霓虹、`forest_deep` 森林深绿、`ocean_air` 海风蓝、
`moody_teal` 暗调青橙、`cinematic_night` 电影夜色）；胶片 / 黑白（`film_warm` 温润胶片、
`mono_contrast` 黑白高反差、`retro_fade` 复古褪色、`bw_soft` 柔和黑白）。
另外提供婚礼 / 日系 / 商业场景预设：`wedding_airy` 婚礼空气感、`wedding_blush` 婚礼蜜桃、
`japanese_fresh` 日系清新、`japanese_milk` 日系奶油、`korean_cream` 韩式奶油、
`indoor_luminous` 室内明亮、`outdoor_clean` 户外通透、`golden_sunset` 夕阳电影、
`forest_story` 森林故事、`ocean_breeze` 海边清蓝、`night_city` 城市夜景、`retro_album` 旧相册胶片。
**肤色保护默认自动**：检出人脸的照片，肤色区域不会被参考色调带偏。

Web UI 是 Apple 风格的单页应用（毛玻璃材质、动态壁纸光斑、活动圆环评分、
iOS 分段控件/开关/滑杆、卡片错峰入场动画），支持跟随系统浅色/深色；处理时
**逐张实时反馈进度**，结果浮层里可**左右拖动分割线**对比原图与成品。

Web UI 的**追色**与**美化**是两个独立操作：先在照片卡片上点击「设为目标」
指定一张目标照片，再选中要处理的照片点击「追色」（目标照片不会被改写）；
点击「美化」则只执行白平衡、磨皮、清晰度和眼部提亮，不会执行追色。
美化面板还提供**人脸修复、瑕疵修复与局部区域**：可将处理限定到皮肤、人脸、眼睛或背景；
修复采用本地可解释的软掩码与保纹理算法，不会凭空生成五官。预设会自动带入一组保守的人像
建议值，仍可用滑杆覆盖，适合婚礼整组先统一、再逐张微调。

**快审模式（灯箱）**：网格里**双击任意照片**进入全屏大图灯箱，键盘批量审片——
`←`/`→` 或 `J`/`K` 切换（自动预取下一张，翻页秒开）、`空格` 加入/移出保留集、
`0`–`5` 打星级并**即时写 XMP sidecar**（手动星级覆盖自动映射，Lightroom /
darktable 导入即读）、`Esc` 退出。当前审片位置在网格上以金色描边同步高亮。

**智能分组与比较**：扫描结果默认按“组内最佳帧分数”排序，近似连拍照片会显示
`相似 1/N` 并排在一起；组内最佳帧可打开“比较 2–4 张”，四张以内同步预览，
通过 `1×–4×` 滑杆联动放大，避免把细微表情/清晰度差异藏在狭长缩略图里。
工具栏还支持按智能分组、综合分、文件名或星级排序，以及推荐/相似组/已加星/待复核筛选。
卡片右上角的星标会立即写入同名 XMP，重新扫描仍能保留人工星级。

**预设许可**：内置预设是本项目原创的 OKLab 相对参数，不携带 LUT、样片或第三方
受版权保护的风格资产，可随商业成片使用。算法设计只参考公开的色彩科学资料；没有复制
第三方代码或预设文件。可复用的色彩基础设施可优先参考 BSD-3-Clause 的
[OpenColorIO](https://github.com/AcademySoftwareFoundation/OpenColorIO) 与
[Colour Science](https://github.com/colour-science/colour)；照片分组交互则参考
Lightroom 的堆栈/比较视图、Aftershoot 的相似组与星级筛选、Narrative Select 的
Survey Mode，以及 digiKam 的相似度查找。许可证只约束被复制的代码/资产，本项目实现
保持原创并不依赖这些项目。

**三种导入方式**：直接把文件/文件夹（含子文件夹）拖进窗口；点「文件夹…」
调起系统选择器；或粘贴路径。拖入/选择的照片经客户端降采样（2560px，
RAW 原样）后落盘到 `~/Pictures/PhotoPilot/<批次>/`（不可写时回退项目目录），
所有文件落盘后再自动扫描，确保大批量导入不会漏扫；目录结构和同名文件也会保留。

## 端到端自检

```bash
.venv/bin/python scripts/make_testset.py   # 生成合成测试照片（demo/testset）
.venv/bin/python tests/run_all.py          # 全量回归断言通过（含真实人像、性能行为、快审星级、AI 美学评分、批量白平衡、多参考追色与桌面导入）
.venv/bin/python scripts/bench.py          # 性能基准（48 张扫描/追色/加载）
```

真实人像用 `demo/hopper.jpg`（TensorFlow 官方分发的 Grace Hopper 公有领域
照片）验证 FaceMesh 检测、EAR 眨眼指标、精确皮肤掩码与眼部提亮。

## 性能

Web UI 的快速扫描对 RAW 优先读取相机内嵌 JPEG 预览（没有预览时才回退
LibRaw 完整显影）；完整显影只在后续追色/美化处理时执行，因此大批 ARW 导入
会先持续出图，不会被 RAW 解码线程卡在 `0/N`。

Apple M1 实测（`scripts/bench.py`，机器负载会有波动）：

| 项 | 优化前 | 优化后 | 手段 |
|---|---|---|---|
| 追色（同一参考批量） | 868 ms/张 | ≈450-630 ms/张 | 参考统计/MKL目标/CDF序列缓存；uint8 LUT 正向转换；`cv2.pow` 反 gamma；色域压缩只算出界子集；统计直接取自已算 lab 的步长子视图 |
| 扫描打分（48 张） | 纯串行 | **1.4-2.1x**（`--jobs 4`） | 线程池 + 每线程 FaceMesh/Haar 实例（TFLite 推理释放 GIL；避开 macOS spawn 进程池每 worker 重导 mediapipe 的 ~2s 开销） |
| 大图 JPEG 加载（4800px→1024px） | 全解码 | **1.8-3x** | Pillow `draft()` DCT 域降采样（仅 ≤1600px 目标启用，成品导出仍全解码） |

优化过程中发现并修复：**OpenCV `CascadeClassifier.detectMultiScale` 多线程
共用实例会在内部 `scaleData` 断言崩溃**（现按线程独立实例化）；检测器偶发
错误一律降级为"无人脸"而不是把照片丢出报告；并行扫描与串行结果逐张一致
（有测试锁定）。

## 模块结构

```
photopilot/
├── io.py             JPEG/PNG(Pillow+EXIF 纠偏) 与 RAW(rawpy/LibRaw) 统一读取
├── colorops.py       sRGB↔OKLab、色域压缩、肤色掩码（追色/美化共用）
├── face_analysis.py  MediaPipe FaceMesh：EAR 眨眼检测、关键点皮肤掩码、眼部定位
├── aesthetic.py      NIMA 神经网络美学评分（MobileNetV2 ONNX，12.8MB，本地 CPU）
├── cull.py           筛选引擎：多维打分 + 连拍分组（dHash）+ 闭眼重罚，线程池并行
├── color.py          追色：5 种算法 + 参考统计缓存 + 强度/保亮度/肤色保护 + 预设
├── polish.py         美化：白平衡 → 清晰度 → 频率分离磨皮 → 眼部
├── xmp.py            XMP sidecar（Rating/Label，Lightroom/darktable 导入即读）
├── pipeline.py       编排 + 对比小样 + report.json
├── server.py         本地 Web UI（纯标准库，仅绑 127.0.0.1）
└── cli.py            命令行入口（cull / color / polish / run / ui）
```

## 技术来源：借鉴了谁，扩展了什么

| 能力 | 借鉴的开源项目 | 本项目的实现与扩展 |
|---|---|---|
| 多维打分 | [Facet](https://github.com/ncoevoet/facet)（9 维评分思路） | 轻量重实现：全图+分块 P75 双尺度清晰度、曝光/裁剪、RMS 对比度、Hasler 色彩丰富度、人脸质量；**批内相对模糊判定** |
| AI 美学评分 | [NIMA](https://research.google/pubs/pub46993/)（Google, Talebi & Milanfar 2018）+ [Facet](https://github.com/ncoevoet/facet) 的加权思路 | MobileNetV2 + 10 bins 分布头（AVA 训练）ONNX 本地推理约 90ms/张；与传统指标 0.65/0.35 融合，硬伤规则（闭眼/模糊/连拍）仍一票否决；模型经 [hf-mirror](https://hf-mirror.com/cromsc/nima-mobilenet-aesthetic) 获取，随项目内置 |
| 多参考追色 | 商业调色软件的"分场景匹配"实践 | auto 模式下 OKLab 均值 k-means++（固定种子）聚 ≤4 组，组内独立求参考统计；≥4 张生效，report.json 记录组数 |
| 批量白平衡 | Capture One / Lightroom 的整批统一色温实践 | 灰度世界逐张估计通道均值 → 中位数为整批目标（抗离群）→ 校正比直接乘回；服务/UI/CLI/pipeline 四处可用 |
| 眨眼检测 | 商业筛选软件（Aftershoot 等）的头号规则 | [MediaPipe FaceMesh](https://github.com/google-ai-edge/mediapipe) 468 点网格 + **EAR（Soukupová & Čech 2016）**，闭眼重罚、单眼眨眼标记；无 mediapipe 时回退 Haar 框 |
| 精确磨皮掩码 | 商业修图软件的关键点流程 | **关键点多边形（脸椭圆 − 眉眼唇）∩ 肤色范围**，红唇/镜框不再被误磨 |
| 连拍去重 | 商用筛选软件通用做法 | dHash（预模糊抗手抖）序列一维聚类，组内留最清晰 |
| RAW + sidecar | [QuickRawPicker](https://github.com/RawLabo/QuickRawPicker) | rawpy 解码（相机 WB）；XMP Rating/Label 写回，与 Lightroom/darktable 互通 |
| Reinhard 追色 | [hahnec/color-matcher](https://github.com/hahnec/color-matcher)、[dstein64/colortrans](https://github.com/dstein64/colortrans) | LAB 匹配；**另实现 OKLab 匹配（默认）、高斯最优传输线性映射(MKL)、逐通道 CDF、仅亮度 CDF** |
| 色域安全 | 上游普遍硬裁剪 | **线性空间单步色度压缩**（向 Rec.709 亮度轴精确收缩）：实测修掉了"镜片反光变绿"这类色相破坏 |
| 磨皮 | 商业修图软件的频率分离流程 | **引导滤波(He et al.)低频平滑 + 高频纹理按比例保留**，O(N) CPU 实时 |
| 深度模型修脸 | [CodeFormer](https://github.com/sczhou/CodeFormer)、[facefusion](https://github.com/facefusion/facefusion) | 本仓库**未内置**（S-Lab 等许可禁止商用/需下权重）；见路线图接口 |

## 设计决策

- **评分为绝对曲线 + 标记为批内相对**：分数跨批可比，"可能模糊"等标记看
  同一批的分布，避免低纹理题材全军覆没。
- **闭眼是硬伤**：闭眼照片综合分 ×0.75 并标"闭眼"，选片时自动掉出保留集。
- **肤色自动保护**：追色参考无论是参考图还是预设，只要检出人脸，
  肤色区域按掩码衰减迁移量——脸不能被"氛围色"带走。
- **XMP 写在原图旁**：不改动原图，Lightroom/darktable/Bridge 导入即读星级。
- **导出与打分分离**：打分在 1024px，成品按 2560px（Web UI 2048px）重新处理，
  人脸框/关键点跨分辨率映射。

## 已知环境注意事项

- mediapipe 固定 **0.10.x**（纯 CPU、模型内置零下载）。1.x 在部分 macOS 上
  有 Metal 崩溃（`DrishtiMetalHelper` abort），且其 numpy>=2 要求与本栈冲突。
- 依赖三角已钉死：numpy 1.26 + opencv 4.10 + mediapipe 0.10.21（见 pyproject）。

## 路线图（按性价比排序）

1. **深度学习人脸修复后端**：接入 CodeFormer/GFPGAN 权重（注意 CodeFormer
   为 S-Lab 非商用许可；商用可换 Qwen-Image-Edit（Apache-2.0）或自训模型），
   建议通过 [ComfyUI](https://github.com/comfyanonymous/ComfyUI) API 调用。
2. **更强的美学评分**：CLIP+线性头（LAION aesthetic predictor，~300MB），
   补足传统指标对"构图/氛围"的盲区。
3. **多参考图追色**：按场景聚类（逆光/夜景/人像）分别匹配参考统计。
4. **GUI 打磨**：键盘快审（WASD/空格，参考 VibeCulling 的交互）、放大对比。
5. **RAW 全流程**：曝光/白平衡在 rawpy postprocess 参数层暴露给 UI。

## 许可说明

- 本仓库代码：MIT（建议）。
- 依赖：numpy(BSD) / OpenCV(Apache-2.0) / Pillow(HPND-CMU) / rawpy(LGPL-2.1，
  动态链接不影响商用) / mediapipe(Apache-2.0)。
- 若接入 CodeFormer 等模型权重，注意其研究许可限制——这是本仓库默认
  不内置它们的原因。
