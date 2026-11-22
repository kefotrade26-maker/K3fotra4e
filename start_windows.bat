@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title KoveerBridge - Launcher

echo ==========================================
echo KoveerBridge - OPEN THREE VISIBLE WINDOWS
echo ==========================================

if not exist "multi_runner_2.py" (echo ERROR: multi_runner_2.py missing.& pause& exit /b 1)
if not exist "dashboard_server_2.py" (echo ERROR: dashboard_server_2.py missing.& pause& exit /b 1)
if not exist "dashboard_with_chart.py" (echo ERROR: dashboard_with_chart.py missing.& pause& exit /b 1)

where py >nul 2>nul
if errorlevel 1 (echo ERROR: Python Launcher py.exe not found.& pause& exit /b 1)

py -3.8 -c "import sys; print(sys.version)"
if errorlevel 1 (echo ERROR: Python 3.8 unavailable.& pause& exit /b 1)

set "SSL_CERT_FILE="
for /f "delims=" %%C in ('py -3.8 -c "import certifi; print(certifi.where())" 2^>nul') do set "SSL_CERT_FILE=%%C"
if defined SSL_CERT_FILE (echo Certifi: %SSL_CERT_FILE%) else (echo WARNING: certifi not found; runner may report TLS error.)

echo Opening three separate CMD windows. Leave them open.
if defined SSL_CERT_FILE (
  start "KoveerBridge Runner" /D "%~dp0" cmd.exe /k "set SSL_CERT_FILE=%SSL_CERT_FILE%&& py -3.8 multi_runner_2.py"
) else (
  start "KoveerBridge Runner" /D "%~dp0" cmd.exe /k "py -3.8 multi_runner_2.py"
)
timeout /t 2 /nobreak >nul
start "KoveerBridge Dashboard" /D "%~dp0" cmd.exe /k "py -3.8 dashboard_server_2.py"
timeout /t 2 /nobreak >nul
start "KoveerBridge Chart" /D "%~dp0" cmd.exe /k "py -3.8 dashboard_with_chart.py"

echo.
echo Launch commands sent.
echo Dashboard: http://127.0.0.1:8080
echo Chart: http://127.0.0.1:8081
pause
exit /b 0
