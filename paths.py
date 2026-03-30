"""
paths.py
统一解析运行时根目录：
  - 普通 Python 脚本运行时 → 本文件所在目录
  - PyInstaller 打包后运行时 → .exe 所在目录
"""

import os
import sys


def get_app_dir() -> str:
    """返回应用根目录（.exe 或脚本所在目录）。"""
    if getattr(sys, "frozen", False):
        # PyInstaller 打包环境：sys.executable 是 .exe 路径
        return os.path.dirname(sys.executable)
    # 普通脚本环境
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR: str = get_app_dir()
