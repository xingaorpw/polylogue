@echo off
REM polylogue 双击启动。不用记命令，双击这个文件就行。
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo 没找到 python。先装 Python 3.9 以上版本，安装时勾选 "Add to PATH"。
  pause
  exit /b 1
)

REM 装一次就够了，之后会直接启动
python -c "import polylogue" >nul 2>nul
if errorlevel 1 (
  echo 第一次运行，正在安装...
  python -m pip install -e . --quiet
  if errorlevel 1 (
    echo 安装失败。手动执行：python -m pip install -e .
    pause
    exit /b 1
  )
)

python -m polylogue --app
if errorlevel 1 pause
