@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  py -3 -m venv .venv
  if errorlevel 1 goto failed
)
.venv\Scripts\python.exe -c "from PIL import Image; assert 11 <= int(Image.__version__.split('.')[0]) < 14" >nul 2>&1
if errorlevel 1 (
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 goto failed
)
.venv\Scripts\python.exe kindle_dashboard.py serve %*
if errorlevel 1 goto failed
exit /b 0
:failed
echo Startup failed. Please install Python 3.10 or newer and check your network.
pause
exit /b 1
