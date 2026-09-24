# -*- mode: python ; coding: utf-8 -*-
"""红薯雷达 主程序打包配置（PyInstaller）。

输出：dist/红薯雷达.exe —— 单文件、窗口模式（无控制台黑框）。
采集时不会闪现黑色命令窗口（所有子进程均带 CREATE_NO_WINDOW）。
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(SPECPATH)

# Playwright 的 driver（内含 node 与 CLI）必须一并打包，
# 否则可见浏览器兜底 / chromium 通道无法启动。
datas = []
try:
    datas += collect_data_files("playwright", include_py_files=False)
except Exception:
    pass

datas += [(os.path.join(ROOT, "assets", "app-icon.ico"), "assets")]

hiddenimports = ["sqlite3", "tkinter", "tkinter.ttk", "tkinter.font", "tkinter.filedialog",
                 "tkinter.messagebox", "zoneinfo", "csv", "json", "queue", "webbrowser"]
try:
    hiddenimports += collect_submodules("playwright")
except Exception:
    pass

a = Analysis(
    [os.path.join(ROOT, "app.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib", "numpy", "scipy", "pandas", "PyQt5", "PyQt6", "PySide2",
        "PySide6", "IPython", "notebook", "pytest", "setuptools", "pip",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="红薯雷达",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 关键：不出现命令行黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, "assets", "app-icon.ico"),
)
