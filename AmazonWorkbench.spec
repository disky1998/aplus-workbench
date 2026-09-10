# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：A+ 抓取工作台（单文件 exe，带托盘）

不打包 Playwright 浏览器内核（约 150MB）—— 运行时复用系统
%LOCALAPPDATA%\\ms-playwright，缺失时界面上一键下载。
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).resolve()

datas, binaries, hiddenimports = [], [], []

# playwright 的 node driver 必须整包带上（含 node.exe 与 package/cli.js）
for pkg in ("playwright",):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# 前端页面与图标
datas += [
    (str(ROOT / "web"), "web"),
    (str(ROOT / "assets"), "assets"),
]

hiddenimports += [
    "appicon", "updater", "version", "tray", "product", "exporter",
    "uvicorn.logging",
    "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan", "uvicorn.lifespan.on",
    "pystray._win32",
    "bs4", "lxml", "lxml.etree", "openpyxl", "et_xmlfile",
    "requests", "fastapi", "starlette", "pydantic",
    "anyio", "h11", "click", "sniffio", "idna", "certifi", "charset_normalizer",
]

EXCLUDES = [
    "tkinter", "matplotlib", "numpy", "pandas", "scipy",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "IPython", "pytest",
    "notebook", "sqlalchemy", "pytz", "PIL.ImageQt", "pandas",
]

a = Analysis(
    [str(ROOT / "tray.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
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
    name="AmazonWorkbench",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ROOT / "assets" / "app.ico"),
)
