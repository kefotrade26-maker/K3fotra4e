@echo off
setlocal
cd /d "%~dp0"
echo ==========================================
echo KoveerBridge - Windows setup (Python 3.8)
echo ==========================================
where py >nul 2>nul
if errorlevel 1 (
  echo ERROR: Python Launcher not found. Install Python 3.8.10 and enable the launcher.
  pause
  exit /b 1
)
py -3.8 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,8) else 1)"
if errorlevel 1 (
  echo ERROR: Python 3.8 was not found. This package is configured for Python 3.8.x.
  pause
  exit /b 1
)
py -3.8 -m pip install --upgrade "pip<25.0"
if errorlevel 1 goto failed
py -3.8 -m pip install -r requirements-windows-py38.txt
if errorlevel 1 goto failed
echo.
echo Dependencies installed successfully for Python 3.8.
pause
exit /b 0
:failed
echo.
echo ERROR: Dependency installation failed. Check the messages above.
pause
exit /b 1
