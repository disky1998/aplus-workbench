@echo off
chcp 65001 >nul
title Amazon Workbench
cd /d "%~dp0"

set "PY=C:\Users\qq117\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"

echo.
echo   ============================================
echo     亚马逊采集工作台（源码模式）
echo     URL   http://127.0.0.1:8788
echo     关闭本窗口即停止服务；打包版请用 dist\AmazonWorkbench.exe
echo   ============================================
echo.

echo   [1/4] 检查依赖 ...
"%PY%" -c "import fastapi,uvicorn,requests,playwright,bs4,openpyxl" >nul 2>&1
if errorlevel 1 (
  echo         安装中 ...
  "%PY%" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
)

echo   [2/4] 检查 Playwright 内核 ...
"%PY%" -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); p.chromium.executable_path; p.stop()" >nul 2>&1
if errorlevel 1 (
  echo         下载 chromium 约 150MB，只需一次 ...
  "%PY%" -m playwright install chromium
)

echo   [3/4] 启动服务 ...
echo.
"%PY%" webapp.py

echo.
echo   服务已停止，按任意键关闭 ...
pause >nul
