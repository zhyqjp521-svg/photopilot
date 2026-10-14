# Mac 版优化建议清单

> 审查日期：2026-10-13。范围：macOS 桌面版（`run_app` pywebview 路径、
> `scripts/build_app.py` 打包、`photopilot/platform.py` 平台层、导出/XMP 管线、
> 前端交互）。所有问题均经源码核实并标注位置；改动范围为粗估
> （小 < 半天 · 中 1–2 天 · 大 > 3 天）。

## 实施状态（2026-10-13 更新）

| # | 项目 | 状态 |
|---|---|---|
| 1 | Intel Mac 无法启动（arch 硬编码） | ✅ 已修复 |
| 2 | 二次双击 .app 无反应 | ✅ 已修复（通知 + 激活已有窗口） |
| 3 | 导出丢失 EXIF | ✅ 已修复（EXIF 透传 + Orientation 归一 + ICC 嵌入） |
| 4 | 可分发的独立 .app | ✅ 新增 `scripts/build_mac_dist.py` |
| 5 | 16-bit / TIFF / ICC | ⚠️ 部分（ICC 全接入 + TIFF 保留；16-bit 因管线 uint8 绑定搁置） |
| 6 | 导出完成通知 + Dock 徽标 | ✅ 已实现 |
| 7 | 在 Finder 中显示 | ✅ 已实现（`/api/reveal` + 灯箱按钮 + 卡片右键） |
| 8 | 可撤销局部画笔 | ⬜ 未做（P2 大项） |
| 9 | 去噪/去模糊后端 | ⬜ 未做（P2 大项） |
| 10 | 经授权美学模型 | ✅ 已实现（idealo MobileNet Apache-2.0，`--ai` 恢复） |
| 11 | 灯箱 EXIF 参数面板 | ✅ 已实现（`/api/exif` + `exif_summary()` + 灯箱 `lbExif`） |
| 12 | 窗口标题随项目更新 | ✅ 已实现 |
| 13 | 窗口尺寸/位置记忆 | ✅ 已实现（resize 防抖 + 启动恢复） |
| 14 | 桌面窗口自动化冒烟 | ✅ 已实现（`tests/smoke_desktop.py`，Playwright 真实浏览器） |
| 15 | i18n | ⬜ 未做（P3 大项，按需） |
| 附1 | venv 双 OpenCV 清理 | ✅ 已清理（仅留 headless 4.10） |
| 附2 | git 历史未授权 NIMA 权重 | ✅ 已清除（`git filter-repo` 重写历史） |

## P0 — Mac 硬伤（真 Bug，建议立即修）

### 1. Intel Mac 完全无法启动
- **现状**：`scripts/build_app.py:44` 启动器硬编码 `exec /usr/bin/arch -arm64 …`。
  Intel Mac 上 `arch -arm64` 直接报 `Bad CPU type`，.app 双击闪退；
  且本机 venv 本来就是 arm64 构建，该前缀纯属冗余。
- **建议**：删除 `arch -arm64` 前缀；如需双架构，未来用通用二进制 venv 而非启动器强制。
- **收益**：Mac 版可运行于 Intel 机型；分发给他人不再翻车。
- **改动范围**：小（1 行 + 重装 .app）。

### 2. 程序已在运行时，再次双击 .app「无反应」
- **现状**：`server.py:4162-4164` 单实例锁拒绝后仅 `print`——从 .app 启动时
  没有 stdout，用户看到的就是"点了没任何动静"。
- **建议**：拒绝时调用 `osascript -e 'display notification …'` 弹通知，并用
  `open -a PhotoPilot` / AppleScript activate 把已有窗口带到前台。
- **收益**：消除最常见的"双击没反应"困惑（与之前按钮置灰问题同类的体验黑洞）。
- **改动范围**：小（`platform.py` 加 1 个 `activate_running_instance()`，`run_app` 调用，~20 行）。

### 3. 导出成品丢失全部 EXIF 拍摄信息
- **现状**：所有导出（CLI `pipeline.py:203`、Web UI `server.py:795`）都经
  `io.py:162-169 save_image`——从 numpy 数组新建 PIL 图直接保存，无 `exif=`。
  成品的拍摄时间、镜头、光圈/快门/ISO、GPS 全部丢失。对摄影交付是硬伤。
- **建议**：读原图时抽取 EXIF 字节（Pillow `getexif()` + `exif=...` 传回），
  导出时原样写入；已横竖纠正的图同步修正 Orientation=1。
- **收益**：成品可直接归档/交付，Lightroom 读到完整拍摄信息；这是同类工具的基本预期。
- **改动范围**：小–中（io.py ~40 行 + 回归测试，注意 RAW 源无 EXIF 时跳过）。

## P1 — Mac 体验升级（收益大，改动可控）

### 4. 产出可分发的独立 .app（当前只是"本机路径启动器"）
- **现状**：`build_app.py` 生成的 .app 内部是指向本机 `.venv` 的 bash 启动器，
  `docs/COMMERCIAL_USE.md` 第 6 条自己也注明"不可移交给其他机器"。
- **建议**：本机直接用 PyInstaller `--windowed --onedir` 打 Mac 包
  （复用现有 Windows 构建经验），配 `.icns` + Info.plist，产出拷贝即用的
  PhotoPilot.app；可选 ad-hoc 签名消除 Gatekeeper「未知开发者」拦截。
- **收益**：Mac 版从"仅本机可用"升级为"可拷给任何 Mac 的软件"，与 Windows exe 对齐；
  体积约 150–250MB（onedir 可拆分，冷启动反而更快）。
- **改动范围**：中（新 `scripts/build_mac_dist.py` ~150 行；mediapipe/rawpy 数据收集同 Windows 踩坑经验）。

### 5. 16-bit / TIFF / ICC 输出
- **现状**：`io.py:144-149` RAW 显影固定 `output_bps=8`；导出仅 JPEG/PNG、
  不嵌 ICC。`COMMERCIAL_USE.md` 路线图第 1 项就是它。
- **建议**：导出设置增加 TIFF 与 16-bit 选项（rawpy `output_bps=16` + Pillow
  `mode="I;16"` 写 TIFF）；JPEG/PNG 路径嵌入 sRGB ICC profile（Pillow 自带 littlecms）。
- **收益**：成品再进 Photoshop/Lightroom/Capture One 不损失带宽，专业婚礼/商业
  工作流可用；8-bit JPEG 不受影响。
- **改动范围**：中（io.py + export_recipe + 前端两个下拉项，~80 行 + 测试）。

### 6. 后台导出完成通知 + Dock 进度徽标
- **现状**：批量导出（上万张耗时数分钟）期间窗口常被最小化，完成无任何系统级提示。
- **建议**：导出队列完成钩子里发 macOS 通知
  （`osascript display notification "已导出 N 张"`）；扫描/导出进度同步 Dock badge
  （`NSApplication.dockTile.badgeLabel`，经 pywebview 不可达时跳过）。
- **收益**：摄影师切去修图不用盯进度条，多任务效率直接提升。
- **改动范围**：小（export_queue 完成回调 + platform.py 通知函数，~30 行）。

### 7. 「在 Finder 中显示」
- **现状**：网格卡片/结果浮层没有任何打开原片所在位置的方式；找原片、
  检查 sidecar、拖原图到微信都只能手动翻目录。
- **建议**：加 `/api/reveal` 后端（`subprocess.run(["open", "-R", path])`），
  卡片右键菜单 + 结果浮层 + 灯箱各加一个入口。
- **收益**：高频小操作，摄影师每天用几十次；与 Mac 系统习惯一致。
- **改动范围**：小（后端 ~15 行 + 前端菜单 ~30 行）。

## P2 — 功能补全（项目路线图已规划，属大项）

### 8. 可撤销局部画笔（蒙版手刷）
- **现状**：区域控制只有"皮肤/人脸/眼睛/背景"四类自动区域，无法手刷
  局部加强/修复。`COMMERCIAL_USE.md` 路线图第 2 项。
- **收益**：从"半自动修图"跨到"可交付级修图"，人像工单刚需。
- **改动范围**：大（前端 canvas 笔刷 + 蒙版合成管线 + 草稿/撤销序列化，估 500+ 行）。

### 9. 去噪/去模糊后端（Restormer / NAFNet）
- **现状**：`COMMERCIAL_USE.md` 已预审许可（Restormer 仓库 MIT 候选），
  尚未接入；`--exclude-module onnxruntime` 的决定也要随之复盘。
- **收益**：高 ISO 婚礼夜景、运动糊片起死回生；但须同步解决权重授权（见清单第 10 项教训）。
- **改动范围**：大（模型选型 + 授权核实 + 推理管线 + UI）。

### 10. 经授权的人脸/美学模型
- **现状**：mediapipe（Apache-2.0）人脸可用；NIMA 美学权重因无授权停用
  （`MODEL_LICENSE_VERIFIED=False`），`--ai` 回退传统指标。
- **建议**：选 Apache-2.0/CC-BY 明确授权的现成权重（如部分 PhotoNet/AVA 二次
  发布需逐一核对），接入后恢复 `--ai` 与「美学模型」开关。
- **收益**：筛片准确度显著提升（美学分是智能筛选的核心维度）。
- **改动范围**：中–大（模型审计 + aesthetic.py 换权重 + 阈值重标定）。
- **实施（2026-10-14）**：采用 idealo/image-quality-assessment 的 MobileNet 美学
  权重（**Apache-2.0**，AVA 训练，EMD 0.071 / LCC 0.626 / SRCC 0.609）。经隔离
  venv（tensorflow 2.21 + tf2onnx）一次性把 Keras .hdf5 转 ONNX
  （`models/nima_mobilenet_aesthetic_apache.onnx`，12.9MB）；`aesthetic.py` 的
  `MODEL_LICENSE_VERIFIED=True`，推理改为直接取 softmax 分布的期望分（新模型
  dense 层已带 softmax，去掉旧的二次 softmax）；预处理 `/127.5-1` 与旧代码等价。
  前端「美学模型」开关恢复，打包脚本恢复 onnxruntime 收集 + 模型 add-data。
  旧的未授权权重已从工作区与 git 历史彻底移除。

### 11. 灯箱显示拍摄参数（EXIF 面板）
- **现状**：快审灯箱只有分数/星级，看不到光圈、快门、ISO、焦段、机身；
  判断"糊没糊/虚没虚"全靠目测。
- **建议**：读图时顺带提取 EXIF（与第 3 项共用提取代码），灯箱角落显示
  `f/2.8 · 1/125 · ISO 6400 · 85mm`。
- **收益**：筛片决策提速，尤其运动/暗光场景；与第 3 项一起做摊薄成本。
- **改动范围**：小–中（~100 行，大部分与第 3 项共享）。
- **实施（2026-10-13）**：`io.py` 新增 `exif_summary()`（解析机身/镜头/光圈/快门/
  ISO/焦段/等效焦段/拍摄时间/分辨率/GPS 为可读字段，RAW 或无 EXIF 返回 None）；
  `server.py` 新增 `GET /api/exif?p=` 路由；灯箱 `lbBar` 加 `lbExif` 元素，
  `lbUpdate` 按需异步拉取（带请求序号 `lbExifSeq` 防翻页竞态），GPS 可点击跳 OpenStreetMap。

## P3 — 打磨项（顺手可做）

### 12. 窗口标题随项目更新
- 现状恒为 "PhotoPilot"（`server.py:4204`）；改 `document.title = 文件夹名 · N 张`
  即可同步到 WKWebView 窗口标题（多窗口/Spotlight 切换时有用）。小，~10 行。

### 13. 窗口尺寸/位置记忆
- pywebview 不自带；用 localStorage 存尺寸 + 启动参数恢复。小，~20 行。

### 14. 桌面窗口自动化冒烟测试
- `AUDIT.md` 自己承认"桌面 UI 的实际点击走查未完成"。为 `run_app` 加一条
  Playwright/AppleScript 冒烟（启动→选目录→扫描→导出），防回归。中。
- **实施（2026-10-13）**：新增 `tests/smoke_desktop.py`（Playwright + 真实
  Chromium，无头模式）。自造 6 张带 EXIF 的合成图到临时目录，走全链路：
  启动加载 → 注入目录自动扫描 → 6 张卡片出齐 → 全选 → 批量导出队列 →
  轮询产物文件 ≥6 张。每步截图留档 `tests/_smoke_out/`（视觉验收，
  已入 .gitignore）。运行：`.venv/bin/python tests/smoke_desktop.py`
  （`--headed` 可见窗口 / `--keep` 保留输入目录）。首次运行发现并修正了
  队列 DOM 状态无 `data-state` 属性导致等待超时的问题（改为产物文件轮询）。

### 15. 界面英文切换（i18n）
- 现为纯中文；如有海外客户/协作再考虑，文案抽取工程量大。大，按需启动。

## 附：与 Mac 版相关的两个前置事项（非 Mac 专属）

- **venv 双 OpenCV 清理**：`opencv-contrib-python 4.11` 与 `opencv-python-headless 4.10`
  共存（实际加载 4.10）；Mac 打包前卸载 contrib，避免体积膨胀与符号冲突。小。
- **git 历史中的未授权 NIMA 权重**：商业分发 Mac 版前必须清理（已另行报告，
  等授权后执行 `git filter-repo` 重写）。

## 建议实施顺序

```
第 1 批（P0，半天内）：#1 arch 前缀 → #2 二次启动激活 → #3 EXIF 保留
第 2 批（P1，2–3 天）：#4 独立 .app → #7 Finder 显示 → #6 通知 → #12/#13
第 3 批（P1 收尾）：#5 16-bit/TIFF/ICC → #11 EXIF 面板
第 4 批（P2，按需排期）：#8 画笔 → #10 授权模型 → #9 去噪后端
```
