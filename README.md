# PhotoPilot —— 照片筛选 / 追色 / 美化 流水线

全本地运行的批量照片处理工具：**智能筛选（清晰度、曝光、人脸与眨眼检测）→
批量白平衡 → 追色统一色调 → 磨皮美化 → 导出成品 + 报告**。无云端、无订阅、
CPU 即可运行。美学评分采用 idealo/image-quality-assessment 的 Apache-2.0 权重；
商业分发前须完成依赖及资产审计，见
[商业使用记录](docs/COMMERCIAL_USE.md)。提供 **macOS 桌面软件**（双击图标、原生窗口）、
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
#    ←/→ 或 J/K 切换 · 空格 打开/返回选片 · 0-5 打星级（写 XMP）· Esc 退出

# 0c) 智能筛选：扫描后点「智能筛选」一键保留
#    勾选「美学模型」后用 NIMA 美学分（idealo MobileNet，Apache-2.0）加权综合分。

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
美化面板还提供**人脸修复、瑕疵修复与局部区域**：可将清晰度、磨皮与修复限定到皮肤、人脸、眼睛或背景；
曝光、自动曝光、白平衡和追色仍作用于全图。眼睛区域在没有面部关键点时会回退到 Haar 眼部定位；
如果皮肤/人脸/眼睛区域无法定位，或人脸修复所需的人脸未检出，单张大图预览和导出会显示未应用相应修复的提示。
修复采用本地可解释的软掩码与保纹理算法，不会凭空生成五官。预设会自动带入一组保守的人像
建议值，仍可用滑杆覆盖，适合婚礼整组先统一、再逐张微调。

**快审模式（灯箱）**：网格里**双击任意照片**进入全屏大图灯箱，键盘批量审片——
`←`/`→` 或 `J`/`K` 切换（自动预取下一张）、`空格` 打开大图或返回选片（不改变选择）、
`0`–`5` 打星级并**即时写 XMP sidecar**（手动星级覆盖自动映射，Lightroom /
darktable 导入即读）、`Esc` 退出。当前审片位置在网格上以金色描边同步高亮。

**智能分组与比较**：扫描结果默认按“组内最佳帧分数”排序，近似连拍照片会显示
`相似 1/N` 并排在一起；组内最佳帧可打开“比较 2–4 张”，四张以内同步预览，
通过 `1×–4×` 滑杆或滚轮联动放大、拖动同步平移，并可一键适合画布。
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
调起系统选择器；或粘贴路径。浏览器上传保留照片原始字节，不再降采样或重编码，
落盘到 `~/Pictures/PhotoPilot/<批次>/`（不可写时回退项目目录），
所有文件落盘后再自动扫描，确保大批量导入不会漏扫；目录结构和同名文件也会保留。

**单张调节与导出**：选中一张，点「美化」或「套用预设」打开大图前后对比，可调
曝光 EV、对比度、高光、阴影、冷暖、饱和度，并可实时调整磨皮、纹理保留、清晰度、
人脸修复、瑕疵修复、**AI 降噪（NAFNet，默认关闭）**与局部区域。调节只生成预览，点「导出成品」才写
最终文件。导出尺寸支持原始尺寸、4096/2560/2048px（不放大小图），JPEG 可选质量；
PNG 保持 PNG。成品写入 `photopilot_out/<会话>/`，不同处理会话互不覆盖。可保存、载入
原创 JSON 参数复用到批量处理；不是 Lightroom XMP/LUT 导入。
基础调色与人像局部共用「撤销 / 重做」及 `⌘/Ctrl Z`、`⌘/Ctrl Shift Z` / `Ctrl Y`；
一次拖动或键盘连按合并为一步，重置也可撤销，每张照片最多保留 50 个编辑状态。
草稿和历史随项目恢复，兼容旧版仅调色草稿；操作类型、预设或打开时的非调色处理基底改变时建立新历史。
单张人像调节不改变批量人像参数；六项基础调色仍同步到批量处理。纯追色模式不套用这套历史。
「显示区域蒙版」只以蓝色叠层显示区域过滤范围；磨皮、修脸还会进一步受肤色和人脸识别限制。
蒙版不会进入成品、历史或参数配方，也不是手动画笔蒙版。

**项目恢复**：图库、保留选择（包括手动全不选）、目标照片、星级、视图和参数会自动
保存到 `~/Library/Application Support/PhotoPilot/project.json`。工具栏的「保存项目」
会等待服务器确认最新数据已写入；关闭前可显式保存。自动保存和关闭事件是尽力而为，
不能保证立即强制退出前的最后一个操作已保存。原图仍保留在原位置，不会被项目存档备份。
原图缺失、变化或存档损坏时暂停覆盖旧项目，可「重试恢复」；完整成功的新扫描才能解除
异常恢复保护。中断扫描只恢复已存的照片清单，不恢复运行中的扫描线程。当前仅保存最近一个项目。
新的扫描／导入会替换当前图库并重建单张草稿；继续上一批修图应直接恢复项目，不必重新扫描。

**批量导出队列**：选中多张后点「美化」「追色」或「套用预设」，任务交给本地后台引擎，
可继续审片。右侧「导出队列」显示进度，支持暂停、继续、重试失败项和查看结果。
暂停会先完成当前照片；刷新窗口不会中断任务，退出应用则停止引擎，重新打开后需明确点「继续」。
重试使用提交时冻结的调色参数、参考像素与整批白平衡，只处理失败项，成功成品不重做。
每页结果最多 50 张，640px 对比预览与完整成品下载分开，原图不改动，已有输出不覆盖。
任务记录保存在 `~/Library/Application Support/PhotoPilot/exports.json`，可通过
`PHOTOPILOT_EXPORT_PATH` 指定隔离测试路径。最多保留 20 个任务；「清除记录」只删除历史，不删除成品。
源照片丢失或改变会报错。成品已发布但完成记录尚未保存时中断，恢复可能提示文件冲突，
需要检查该成品或新建任务，不会自动覆盖或接纳已有文件。提交响应丢失时，同一页面可点
「确认上次提交」用原参数确认；刷新后这个待确认意图不保留，应先检查恢复的队列，避免重复新建。

当前处理管线是 8-bit sRGB，原始尺寸不等于完整 RAW 非破坏编辑。已经剪切成纯白的高光
无法恢复不存在的细节；局部修脸是保纹理算法，不是生成式五官重建。

## 端到端自检

```bash
.venv/bin/python scripts/make_testset.py   # 生成合成测试照片（demo/testset）
.venv/bin/python tests/run_all.py          # 全量回归断言通过（含真实人像、性能行为、快审星级、AI 美学评分、批量白平衡、多参考追色与桌面导入）
.venv/bin/python -m unittest discover -s tests -q
node tests/frontend_regressions.cjs
.venv/bin/python scripts/bench_edit.py    # 单图完整渲染基准，临时目录不改用户照片
.venv/bin/python scripts/bench.py          # 性能基准（48 张扫描/追色/加载）
```

历史 `tests/run_all.py` 会写 `demo` 下的测试 XMP 和成品；需要保持现场时，请在临时项目副本运行，
并将项目、导出队列、上传和缩略图缓存定向到临时目录。本轮验收记录见
[批量导出审计](docs/AUDIT_2026-10-01_EXPORT_QUEUE.md)。
单张人像实时调节、草稿、蒙版与桌面实测见
[单张精修验收](docs/qa-2026-10-03-live-retouch/report.md)。

真实人像用 `demo/hopper.jpg`（Grace Hopper 公有领域照片，来源
[Wikimedia Commons](https://commons.wikimedia.org/wiki/File:Grace_Hopper.jpg)，
亦为 TensorFlow 官方测试图）验证 FaceMesh 检测、EAR 眨眼指标、精确皮肤掩码与
眼部提亮。该照片仅作算法验证，不作为宣传人像资产。

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
├── aesthetic.py      NIMA 美学评分（idealo MobileNet，Apache-2.0，ONNX）
├── denoise.py        AI 降噪（NAFNet SIDD，MIT，ONNX，tile 分块，默认关闭）
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
| AI 美学评分 | [NIMA](https://research.google/pubs/pub46993/) 论文与 [idealo/image-quality-assessment](https://github.com/idealo/image-quality-assessment)（Apache-2.0） | MobileNet 美学权重转 ONNX 本地推理，与传统指标加权融合；硬伤规则（闭眼/模糊/连拍重复）一票否决 |
| 多参考追色 | 商业调色软件的"分场景匹配"实践 | auto 模式下 OKLab 均值 k-means++（固定种子）聚 ≤4 组，组内独立求参考统计；≥4 张生效，report.json 记录组数 |
| 批量白平衡 | Capture One / Lightroom 的整批统一色温实践 | 灰度世界逐张估计通道均值 → 中位数为整批目标（抗离群）→ 校正比直接乘回；服务/UI/CLI/pipeline 四处可用 |
| 眨眼检测 | 商业筛选软件（Aftershoot 等）的头号规则 | [MediaPipe FaceMesh](https://github.com/google-ai-edge/mediapipe) 468 点网格 + **EAR（Soukupová & Čech 2016）**，闭眼重罚、单眼眨眼标记；无 mediapipe 时回退 Haar 框 |
| 精确磨皮掩码 | 商业修图软件的关键点流程 | **关键点多边形（脸椭圆 − 眉眼唇）∩ 肤色范围**，红唇/镜框不再被误磨 |
| 连拍去重 | 商用筛选软件通用做法 | dHash（预模糊抗手抖）序列一维聚类，组内留最清晰 |
| RAW + sidecar | [QuickRawPicker](https://github.com/RawLabo/QuickRawPicker) | rawpy 解码（相机 WB）；XMP Rating/Label 写回，与 Lightroom/darktable 互通 |
| Reinhard 追色 | [hahnec/color-matcher](https://github.com/hahnec/color-matcher)、[dstein64/colortrans](https://github.com/dstein64/colortrans) | LAB 匹配；**另实现 OKLab 匹配（默认）、高斯最优传输线性映射(MKL)、逐通道 CDF、仅亮度 CDF** |
| 色域安全 | 上游普遍硬裁剪 | **线性空间单步色度压缩**（向 Rec.709 亮度轴精确收缩）：实测修掉了"镜片反光变绿"这类色相破坏 |
| 磨皮 | 商业修图软件的频率分离流程 | **引导滤波(He et al.)低频平滑 + 高频纹理按比例保留**，O(N) CPU 实时 |
| AI 降噪 | [NAFNet](https://github.com/megvii-research/NAFNet)（ECCV 2022，MIT） | SIDD 真实噪声去噪权重转 ONNX 本地推理；tile 分块 + 重叠羽化融合；默认关闭、手动触发 |
| 深度模型修脸 | [CodeFormer](https://github.com/sczhou/CodeFormer)、[facefusion](https://github.com/facefusion/facefusion) | 本仓库**未内置**（S-Lab 等许可禁止商用/需下权重）；见路线图接口 |

## 设计决策

- **评分为绝对曲线 + 标记为批内相对**：分数跨批可比，"可能模糊"等标记看
  同一批的分布，避免低纹理题材全军覆没。
- **闭眼是硬伤**：闭眼照片综合分 ×0.75 并标"闭眼"，选片时自动掉出保留集。
- **肤色自动保护**：追色参考无论是参考图还是预设，只要检出人脸，
  肤色区域按掩码衰减迁移量——脸不能被"氛围色"带走。
- **XMP 写在原图旁**：不改动原图，Lightroom/darktable/Bridge 导入即读星级。
- **导出与打分分离**：打分在 1024px，成品按用户指定尺寸重新处理（Web UI 默认 2560px），
  人脸框/关键点跨分辨率映射。

## 已知环境注意事项

### 选片与对比（2026-10-11）

- 1200 张以上的图库按可见窗口创建照片卡片，普通排序与相似分组均支持；滚动离开后释放离屏卡片，评分与选择仍保留。
- 选择、加星、项目移除及手动分组支持本次会话内撤销/重做（⌘Z / ⌘⇧Z）。本地照片移入废纸篓不由这个撤销按钮恢复，需在系统废纸篓操作。
- 扫描前可选择严格/均衡/宽松相似度；选择照片后可拆组或合组。档位变化在下一次扫描生效，自动分组仍是启发式判断。
- 对比窗口可同步缩放/移动；点击某个窗格后，0–5 加星、P 保留、X 排除只作用于该窗格。100% 指预览像素（最长边最多 2200），不是原始 RAW 像素。
- 自动曝光保留提亮后的高光层次；已经拍成纯白的细节无法凭曝光调整找回。导出遇到断盘或重启可检查队列并明确恢复，已完成的输出不会重新覆盖。

- mediapipe 固定 **0.10.x**（纯 CPU、模型内置零下载）。1.x 在部分 macOS 上
  有 Metal 崩溃（`DrishtiMetalHelper` abort），且其 numpy>=2 要求与本栈冲突。
- 依赖三角已钉死：numpy 1.26 + opencv 4.10 + mediapipe 0.10.21（见 pyproject）。

## 路线图（按性价比排序）

1. **16-bit 色彩与 RAW 编辑**：把曝光/白平衡前移到显影和线性光阶段，补齐 ICC 工作流。
2. **手动局部画笔**：人脸/皮肤自动掩码之外，增加可撤销笔刷与蒙版可视化。
3. **去噪/去模糊模型**：去噪已接入 **NAFNet width32（MIT，SIDD）**；去模糊（GoPro/REDS）
   因域特异性与婚礼/人像场景匹配度低暂不纳入，Restormer 因注意力对空间变化模糊弱暂不选用。
4. **人像深度修复**：仅在具体代码、权重和第三方组件许可都核实后接入；不默认使用 CodeFormer。
5. **工程化**：增量图库、可取消导出队列、历史版本和更多真实相机 RAW 测试。

## 许可说明

本仓库尚未由权利人选定项目许可证，不因使用开源依赖而自动成为 MIT 项目。
rawpy 自身为 MIT，但其捆绑 LibRaw 等组件需单独核实；模型权重许可与代码许可分开审查。
本次未引入第三方 LUT、商业预设、竞争产品素材或新深度模型。
逐项来源、限制与发布前检查见 [商业使用记录](docs/COMMERCIAL_USE.md)。
