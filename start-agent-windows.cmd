@echo off
setlocal
cd /d "%~dp0"
if not exist clients\agent.json (
  echo Missing clients\agent.json. Copy the pairing config from the hub computer first.
  pause
  exit /b 1
)
py -3 kindle_dashboard.py agent --config clients\agent.json --relay-port 8001
pause
