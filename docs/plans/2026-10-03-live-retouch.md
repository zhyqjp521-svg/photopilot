# 单张实时精修与区域蒙版 Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task. 本轮不提交或推送，不改用户原图；保留所有既有未提交改动。

**Goal:** 在大图中实时调节五项人像参数与区域，统一撤销/恢复草稿，查看区域蒙版，导出一致的无蒙版成品。

**Architecture:** 现有六项调色参数保持 adjustments；新增独立 retouch_adjustments 仅允许 skin/retain/clarity/face_repair/blemish/local_region。预览和成品调用同一渲染链，不修改缓存会话；可选 show_mask 仅生成预览 PNG，捕获 polish_image 实际使用的局部区域过滤蒙版。草稿 version 2 在原 tone/history 外增加平行 retouch/retouchHistory，统一索引与手势；version 1 保持兼容并迁移。人像参数逐张保存，不自动污染批量参数。

**Tech Stack:** 现有 Python/NumPy/OpenCV/Pillow、标准库 HTTP、内联原生 JS/CSS；无新增依赖、模型权重或第三方素材。

当前基线 /tmp/photopilot-live-baseline.CaR3GE/；测试基线 144 Python、82 前端。已知 server.py 是后端与前端同一文件，两个实现任务必须串行，审核通过后再进入下一任务。

## Task 1：后端实时参数与只读区域蒙版

文件：photopilot/server.py、photopilot/polish.py；新增 tests/test_live_retouch.py。

1. 写失败测试：retouch_adjustments 更改真实像素，缓存 session.params 不变；未知键、bool/字符串数值、NaN、越界、非法区域/非布尔 show_mask 在解码前被拒绝。
2. 运行 `.venv/bin/python -m unittest discover -s tests -p test_live_retouch.py -v`，确认因缺少行为而失败。
3. 最小实现：
   ```python
   allowed = {'skin','retain','clarity','face_repair','blemish','local_region'}
   overrides = payload.get('retouch_adjustments', {})
   # strict dict/whitelist/finite numeric [0,1]/region enum validation before load_image
   params = replace(ses['params'], **overrides)
   masks = [] if preview_only and show_mask and do_polish else None
   final = polish_image(graded, params, faces=faces_f, warnings=warnings, region_masks=masks)
   # mask is RGBA [0,122,255,mask*100], immutable PNG under THUMB_DIR/edits/sid
   # export always contains final only; only previews return optional region_mask URL
   ```
   region_masks 可选参数只 append 已经生成的 local；不重复面部检测。蒙版表示局部区域过滤范围（不是所有效果的完整蒙版）。
4. 验证空区域透明、像素/尺寸对齐、切换区域对应 alpha、打开 show_mask 不改变预览成品像素；成品请求不得生成或烘焙蒙版；追色模式不做局部修复/蒙版。
5. 单测 + 完整 Python 测试通过；先规格审核，再质量审核。保存 Task1 文件快照用于 Task2 增量审核。

## Task 2：大图控件、统一历史与草稿兼容

文件：仅 server.py 的 HTML/JS/CSS、tests/frontend_regressions.cjs。不改 Task1 的 Python。

1. 先写失败行为测试：实时请求携带当前 retouch_adjustments；连续拖动只记一次统一历史；撤销/重做区域和人像参数；保存关闭重开/页面恢复保持人像草稿；v1 迁移；无效 v2 原子拒绝；50 步 cap/redo 截断/回到原值等现有回归仍通过。
2. 接口规范：
   ```javascript
   const RETOUCH_KEYS=['skin','retain','clarity','face_repair','blemish','local_region'];
   // keep editor.history as six-tone states for compatibility
   // parallel editor.retouchHistory holds six-retouch states, same length/index
   // draft.version=2 with tone/history + retouch/retouchHistory
   // v1 expands each history entry using frozen initial retouch settings
   ```
   editorBaseSignature 仍包含打开时的非调色基础设置，避免不兼容的预设/全局人像参数复用草稿。编辑器人像参数不直接修改全局控件。
3. 新增五个 slider（磨皮、纹理保留、清晰度、人脸修复、瑕疵修复），区域 select，恢复打开时人像参数按钮。保持六项基础调色。可折叠分组、控制字号/间距，不添加巨大胶囊按钮；短窗口允许面板滚动，图片仍优先占空间。已有撤销按钮说明从“调色”改成“编辑”，重置调色只重置六项调色。
4. 可选“显示区域蒙版”默认关闭；checkbox 只用于显示范围，不进入编辑历史/配方。请求传 show_mask，仅最新 revision/renderEpoch 可更新对应 PNG；区域/参数改变后旧蒙版立即隐藏；失败不得显示过时蒙版。追色编辑器不显示人像/蒙版控件。
5. 蒙版作为透明 img 绝对覆盖在比较图，object-fit:contain 对齐同一图像；与分割线手势相容，不拦截 pointer。提示“蓝色仅表示区域范围；磨皮/修脸还受人脸和肤色识别限制；不会导出”。
6. 导出必须冻结当前 tone 和 retouch：prepare 使用复制的 polish={...editor.settings.polish,...editor.retouch}；process_one 不发送 show_mask；锁新 slider/select/reset/mask 控件。保存与异步失败/切图/关闭的既有保护保留。
7. 完整前端回归 + Python 通过，先规格审核再质量审核。

## Task 3：集成实测、文档与交付

文件：README.md、docs/qa-2026-10-03-live-retouch/report.md 与截图。

1. 独立临时服务，隔离 project/export/upload/cache；用代理自有合成照片实际导入、大图调节、显示蒙版、切区域、撤销重做、关闭重开、导出下载；原图 hash 不变，成品没有蓝色蒙版。
2. 检查 1440×900、1024×768、浅色/深色布局与控制可达性、蒙版对齐；检查 JS 异常、失败后重试和过时结果。
3. 完整回归，py_compile、增量静态扫描、git diff --check；impeccable detector。禁止在实际 repo 运行会改写 demo 的 tests/run_all.py。
4. 若桌面程序运行，确认无未完成导出/用户照片变更后关闭旧 PhotoPilot，再后台启动更新版并核实只有一个主窗口，实际单张调节与导出。不得杀其他 Python/用户 app。
5. 独立最终集成审核；记录真实覆盖范围，不宣称生成式修脸、手动画笔或真实婚礼片质量均已验证。

## Checklist

- [x] Task1 后端 + RED/GREEN + spec + quality（158 项 Python 通过；独立规格及质量审核通过）
- [x] Task2 前端 + RED/GREEN + spec + quality（97 项前端通过；规格及质量审核通过；浅深主题对比度补丁已真实复测）
- [x] Task3 UI/原图/成品实测 + 最终集成审核（255 项回归；真实页面及桌面导出成功；新版仅一个窗口；报告完成）
