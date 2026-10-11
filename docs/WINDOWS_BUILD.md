# Windows 打包说明

把 PhotoPilot 打包成可在 **Windows 10/11（64 位）** 上双击运行的软件。

## 一、结论摘要

| 项 | 内容 |
|---|---|
| 打包方式 | **PyInstaller**（单文件 one-file、无控制台 `--windowed`） |
| 目标架构 | **x64（amd64）**，需用 64 位 Python 构建 |
| 产物 | `dist/PhotoPilot.exe`（双击即用，含全部依赖） |
| 分发形态 | 单文件，适合 U 盘拷贝到隔离/工业电脑 |
| 是否需预装运行时 | **无需预装 Python**；图形窗口依赖系统自带 **Microsoft Edge WebView2 Runtime**（Windows 10/11 普遍已内置，缺省时才需装） |

## 二、为什么是 PyInstaller

- 项目是**纯 Python**（标准库 Web 服务器 + pywebview 桌面窗口），无编译器、无平台专用二进制源码。
- 依赖里有原生库（opencv、onnxruntime、rawpy/LibRaw、mediapipe），PyInstaller 能自动收集并打包这些动态库，得到**免 Python 环境**的独立 exe。
- 目标场景是低配工业电脑 / 隔离机器（与现有 Excel 工具的 U 盘分发流程一致），单文件 one-file 最省事。

备选方案对比（供参考）：
- **Nuitka**：可编译为原生机器码、启动更快，但编译媒体管线等大型依赖耗时极长且易出问题，性价比低。
- **纯 venv + 批处理**：体积小但要求目标机装 Python，不符合"免环境"目标。

## 三、打包前置要求（仅在"打包机"上需要）

在 **Windows 机器**上准备构建环境（64 位 Python 3.10 或 3.11）：

```bat
py -3.11 -m venv .venv-win
.venv-win\Scripts\activate
pip install -e .
pip install pyinstaller
```

> PyInstaller **不能跨平台交叉编译**：macOS / Linux 上无法产出 Windows .exe。
> 必须把本仓库复制到 Windows 机器执行。本仓库已内置 `scripts/build_windows.py`
> 一键脚本，在非 Windows 上运行会直接报错提示。

## 四、打包步骤

### 方式 A：一键脚本（推荐，零命令行）

在 Windows 上解压项目后，**双击根目录的 `build_exe.bat`** 即可。
脚本会自动完成：检测 Python → 建虚拟环境 → 装依赖 → 打包 → 打开 `dist` 文件夹。

完成后产物为 `dist\PhotoPilot.exe`。

> 前置：这台 Windows 需装 64 位 Python 3.11（安装时勾选 "Add python.exe to PATH"），
> 并保持联网（首次要下载依赖）。脚本若没找到 Python 会给出下载指引。

### 方式 B：手动命令行

```bat
:: 在 Windows 上，仓库根目录
python scripts\build_windows.py            :: 生成 dist\PhotoPilot.exe
python scripts\build_windows.py --console  :: 另生成带控制台的排障版
python scripts\build_windows.py --clean    :: 构建前清理缓存
```

该脚本内部调用 PyInstaller（单文件 `--onefile` + 无控制台 `--windowed`），等价命令：

```bat
pyinstaller --noconfirm --clean --onefile --windowed --name PhotoPilot ^
  --collect-all mediapipe --collect-all onnxruntime ^
  --collect-data cv2 --collect-data rawpy --collect-submodules send2trash ^
  --exclude-module matplotlib --exclude-module tkinter run_photopilot.py
```

## 五、运行环境与依赖（目标机）

**基本无需预装任何东西**。exe 已内置 Python、numpy、opencv、mediapipe（含人脸网格模型）、
onnxruntime、Pillow、rawpy(LibRaw)、pywebview 等全部依赖。

唯一可能的运行时要求：

1. **Microsoft Edge WebView2 Runtime**（仅 `app` 桌面窗口模式需要）
   - Windows 10/11 通常已随系统/Edge 内置；若双击 `app` 模式弹不出窗口，请安装：
     <https://developer.microsoft.com/microsoft-edge/webview2/>
   - `ui` 模式走浏览器、`cull/color/polish/run/wb` 走命令行，均**不依赖** WebView2。
2. **无 GPU 要求**：人脸检测与美化走 CPU（mediapipe 0.10.x 纯 CPU）。
3. 内存建议 ≥ 4GB（本项目在 16.7GB 低配机上的 Excel 工具同款环境可运行）。

## 六、平台差异说明（功能已对齐）

代码已做跨平台适配（新增 `photopilot/platform.py` 统一收敛路径与废纸篓差异），
**macOS 上有的功能，Windows 上全部可用**：

| 功能 | macOS | Windows 表现 |
|---|---|---|
| 删除原片到废纸篓 | 系统 Foundation 废纸篓 | **系统回收站**（send2trash → Windows 回收站 API，可恢复） |
| `app` 桌面窗口 | WKWebView 原生窗口 | Edge WebView2 原生窗口（pywebview） |
| 桌面单实例锁 | fcntl.flock | msvcrt.locking（代码已按 `os.name` 分支） |
| 配置/项目存档目录 | `~/Library/Application Support/PhotoPilot` | `%APPDATA%\PhotoPilot` |
| 缓存/缩略图目录 | `~/Library/Caches/PhotoPilot` | `%LOCALAPPDATA%\PhotoPilot\Cache` |
| 日志目录 | `~/Library/Logs` | `%LOCALAPPDATA%\PhotoPilot\Logs` |
| 导入照片目录 | `~/Pictures/PhotoPilot` | `%USERPROFILE%\Pictures\PhotoPilot` |
| Chrome/Edge 回退窗口 | 探测 Chrome/Edge | 探测 Chrome/Edge（含 `msedge.exe`） |
| Linux 支持 | — | 额外支持：XDG 路径 + freedesktop Trash |

其余核心能力（扫描打分、追色、磨皮美化、白平衡、XMP 星级、RAW 解码、批量导出队列、
单张精修、撤销重做、项目恢复、快审灯箱、智能分组比较）与平台无关，三种系统行为一致。

> 唯一需要目标机自备的运行时是 **Edge WebView2 Runtime**（仅 `app` 桌面窗口模式；
> Windows 10/11 通常已内置）。`ui` 走浏览器、`cull/color/polish/run/wb` 走命令行均不依赖它。

## 七、已知打包注意事项

1. **入口脚本**：打包入口是仓库根的 `run_photopilot.py`（顶层脚本，避免
   `photopilot/__main__.py` 相对导入在 PyInstaller 下失败）。
2. **mediapipe 资源**：脚本用 `--collect-all mediapipe` 收集内置 tflite 人脸模型，
   缺失会导致运行时人脸检测报错，勿删除该参数。
3. **双 OpenCV 共存**：若构建环境同时装了 `opencv-python-headless` 与
   `opencv-contrib-python`，建议二选一（推荐仅保留 `opencv-python-headless` 4.10，
   与 `pyproject.toml` 一致），避免体积膨胀与符号冲突。
4. **体积**：单文件约 200–400MB（主要是 mediapipe + onnxruntime + opencv），
   首次启动解压到临时目录会稍慢，属 one-file 正常现象。
5. **旧 NIMA 权重**：源码分发不含未授权权重，exe 同样不内置，`--ai` 会回退传统指标。
6. **send2trash**：Windows/Linux 回收站依赖 `send2trash`（`pyproject.toml` 已按平台声明，
   macOS 不装）。脚本已带 `--collect-submodules send2trash`，缺失时删除原片会提示"缺少 send2trash"。

## 八、验证建议（打包后）

在 Windows 上对 `dist/PhotoPilot.exe` 做冒烟：

```bat
PhotoPilot.exe --help
PhotoPilot.exe cull  demo\testset --no-faces --top 3
PhotoPilot.exe run   demo\testset --preset film_warm --top 3 --out %TEMP%\pp_test
PhotoPilot.exe ui    demo\testset --no-browser   :: 手动打开 http://127.0.0.1:8618 验证
PhotoPilot.exe app   demo\testset                :: 验证 WebView2 窗口
```
