@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=runtime\python\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=backend\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
  echo [ERROR] Python not found: runtime\python\python.exe or backend\.venv
  pause
  exit /b 1
)

"%PYTHON_EXE%" start_services.py stop
pause
