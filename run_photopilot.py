"""PhotoPilot 打包入口（供 PyInstaller 使用）。

直接作为顶层脚本被 PyInstaller 分析；内部委托到 photopilot.cli:main。
命令行子命令：cull / color / polish / run / wb / ui / app。
"""
import sys

from photopilot.cli import main

if __name__ == "__main__":
    sys.exit(main())
