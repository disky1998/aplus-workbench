@echo off
chcp 65001 >nul
title Build APlusWorkbench
cd /d "%~dp0"

set "PY=C:\Users\qq117\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"

echo.
echo  [1/6] 安装打包依赖 ...
"%PY%" -m pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple pyinstaller pystray pillow openpyxl
if errorlevel 1 goto fail

echo  [2/6] 生成图标 ...
"%PY%" appicon.py assets\app.ico
if errorlevel 1 goto fail

echo  [3/6] 清理旧产物 ...
"%PY%" -c "import shutil,pathlib;
[shutil.rmtree(d, ignore_errors=True) for d in ('build','dist') if pathlib.Path(d).exists()]"

echo  [4/6] 打包中（首次约 2-5 分钟）...
"%PY%" -m PyInstaller --noconfirm --clean AmazonWorkbench.spec
if errorlevel 1 goto fail

echo  [5/6] 校验 ...
if not exist "dist\AmazonWorkbench.exe" (
  echo        [错误] 没有生成 exe
  goto fail
)
for %%A in ("dist\AmazonWorkbench.exe") do set SIZE=%%~zA

echo  [6/6] 生成 version.json ...
"%PY%" make_release.py
if errorlevel 1 goto fail

echo.
echo        产物：dist\AmazonWorkbench.exe  %SIZE% 字节
echo.
pause
exit /b 0

:fail
echo.
echo  [失败] 请查看上面的输出
pause
exit /b 1
