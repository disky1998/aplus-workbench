@echo off
chcp 65001 >nul
title Build APlusWorkbench
cd /d "%~dp0"

set "PY=C:\Users\qq117\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"

echo.
echo  [1/5] 安装打包依赖 ...
"%PY%" -m pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple pyinstaller pystray pillow
if errorlevel 1 goto fail

echo  [2/5] 生成图标 ...
"%PY%" appicon.py assets\app.ico
if errorlevel 1 goto fail

echo  [3/5] 清理旧产物 ...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo  [4/5] 打包中（首次约 2-5 分钟）...
"%PY%" -m PyInstaller --noconfirm --clean APlusWorkbench.spec
if errorlevel 1 goto fail

echo  [5/5] 完成
if exist "dist\APlusWorkbench.exe" (
  for %%A in ("dist\APlusWorkbench.exe") do echo        产物：dist\APlusWorkbench.exe  %%~zA 字节
) else (
  echo        [错误] 没有生成 exe
  goto fail
)
echo.
pause
exit /b 0

:fail
echo.
echo  [失败] 请查看上面的输出
pause
exit /b 1
