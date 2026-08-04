@echo off
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo 未找到 Python，请先安装 Python 3.10 或更高版本。
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo 正在创建项目独立运行环境...
  python -m venv .venv
  if errorlevel 1 goto :failed
)

call ".venv\Scripts\activate.bat"
python -c "import aiohttp, websockets, sounddevice, opuslib, soxr, numpy, sherpa_onnx, colorlog" >nul 2>nul
if errorlevel 1 (
  echo 正在安装运行依赖，首次运行需要联网...
  python -m pip install -r requirements.txt
  if errorlevel 1 goto :failed
)

python run.py
exit /b %errorlevel%

:failed
echo 启动准备失败，请查看上方错误信息。
pause
exit /b 1
