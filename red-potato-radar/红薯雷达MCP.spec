# -*- mode: python ; coding: utf-8 -*-
"""红薯雷达 MCP 只读服务打包配置（PyInstaller）。

输出：dist/红薯雷达MCP.exe
  * 控制台模式（stdio 通信需要标准输入输出，不能用 windowed）；
  * 不含 tkinter / Pillow / Playwright，体积小、启动快；
  * 与主程序放在同一文件夹，默认读取同一个 monitor.db。
"""

import os

ROOT = os.path.abspath(SPECPATH)

a = Analysis(
    [os.path.join(ROOT, "mcp_server.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[],
    hiddenimports=["sqlite3", "zoneinfo", "json"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "PIL", "playwright", "matplotlib", "numpy", "scipy", "pandas",
        "PyQt5", "PyQt6", "PySide2", "PySide6", "IPython", "pytest", "pip",
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
    name="红薯雷达MCP",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,           # stdio 通信必须保留控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(ROOT, "assets", "app-icon.ico"),
)
