@echo off
chcp 65001 >nul
title A+ Scraper Workbench
cd /d "%~dp0"

set "PY=C:\Users\qq117\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"

echo.
echo   ============================================
echo     A+ Scraper Workbench
echo     URL   http://127.0.0.1:8788
echo     Close this window to stop the server.
echo   ============================================
echo.

echo   [1/3] Checking dependencies ...
"%PY%" -c "import fastapi,uvicorn,requests,playwright,bs4" >nul 2>&1
if errorlevel 1 (
  echo         Installing dependencies ...
  "%PY%" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
  "%PY%" -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple fastapi uvicorn requests
)

echo   [2/3] Checking Playwright chromium ...
"%PY%" -c "from playwright.sync_api import sync_playwright; import sys; p=sync_playwright().start(); p.chromium.executable_path; p.stop()" >nul 2>&1
if errorlevel 1 (
  echo         Downloading chromium ~150MB, one time only ...
  "%PY%" -m playwright install chromium
)

echo   [3/3] Starting server ...
echo.
"%PY%" webapp.py

echo.
echo   Server stopped. Press any key to close ...
pause >nul
