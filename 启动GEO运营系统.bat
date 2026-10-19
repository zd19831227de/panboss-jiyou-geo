@echo off
chcp 65001 > nul
setlocal
cd /d "%~dp0"

echo ==========================================
echo   GEO Ops Launcher
echo ==========================================

REM Prefer the bundled full Python (runtime/python, folder is self-contained and
REM copies to any machine); fall back to project venv for local development.
set "PYTHON_EXE=runtime\python\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=backend\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
  echo [ERROR] Python not found: runtime\python\python.exe or backend\.venv
  echo.
  pause
  exit /b 1
)

REM restart = stop + start (safe for first launch)
"%PYTHON_EXE%" start_services.py restart
if errorlevel 1 (
  echo.
  echo Launch failed. Check output above or logs:
  echo   runtime-data\logs\
  pause
  exit /b 1
)

REM Wait for services to be fully ready
echo Waiting for services...
ping 127.0.0.1 -n 4 > nul

REM UI address follows port config and frontend mode (vite dev vs backend static)
set "UI_URL="
for /f "usebackq delims=" %%u in (`"%PYTHON_EXE%" start_services.py uiurl 2^>nul`) do set "UI_URL=%%u"
if not defined UI_URL set "UI_URL=http://localhost:15173"

start "" %UI_URL%
echo.
echo UI:       %UI_URL%
echo API:      http://localhost:18770
echo Account:  admin / admin123
echo.
echo Closing this window will NOT stop services.
echo To stop: run "stop" bat or "%PYTHON_EXE%" start_services.py stop
pause
