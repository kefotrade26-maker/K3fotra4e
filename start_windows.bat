@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title KoveerBridge - Start

REM ── 1) اكتشاف Python 3.8 ──
set "PYEXE="
where py >nul 2>nul
if not errorlevel 1 set "PYEXE=py -3.8"
if not defined PYEXE (
    where python >nul 2>nul
    if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE (
    echo ERROR: Python not found.
    pause
    exit /b 1
)

REM ── 2) التحقق من الإصدار ──
%PYEXE% -c "import sys; assert sys.version_info[:2]==(3,8), sys.version"
if errorlevel 1 (
    echo ERROR: Python 3.8 required.
    pause
    exit /b 1
)

REM ── 3) الملفات الأساسية ──
for %%F in (multi_runner_2.py dashboard_server_2.py dashboard_with_chart.py) do (
    if not exist "%~dp0%%F" (
        echo ERROR: Missing %%F
        pause
        exit /b 1
    )
)

REM ── 4) الاعتماديات ──
%PYEXE% -c "import websockets,msgpack,flask,psutil,certifi" >nul 2>nul
if errorlevel 1 (
    echo ERROR: Missing dependencies. Run:
    echo    py -3.8 -m pip install -r requirements-windows-py38.txt
    pause
    exit /b 1
)

REM ── 5) منع التشغيل المكرر ──
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$r=[IO.Path]::GetFullPath('%~dp0').TrimEnd('\'); $p=Get-WmiObject Win32_Process | Where-Object { $_.Name -match '^python(w)?.exe$' -and $_.CommandLine -and $_.CommandLine.IndexOf($r,[StringComparison]::OrdinalIgnoreCase) -ge 0 -and ($_.CommandLine -match 'multi_runner_2.py|dashboard_server_2.py|dashboard_with_chart.py') }; if($p){ exit 10 }"
if errorlevel 10 (
    echo ERROR: KoveerBridge already running. Use restart_windows.bat.
    pause
    exit /b 2
)

REM ── 6) SSL cert ──
set "SSL_CERT_FILE="
for /f "delims=" %%C in ('%PYEXE% -c "import certifi; print(certifi.where())" 2^>nul') do set "SSL_CERT_FILE=%%C"
if not defined SSL_CERT_FILE echo WARNING: certifi missing — TLS may fail.

echo.
echo ============================================
echo   KoveerBridge — Starting services
echo ============================================
echo.

if defined SSL_CERT_FILE (
    start "KoveerBridge Runner" cmd.exe /k "set SSL_CERT_FILE=%SSL_CERT_FILE%&& %PYEXE% multi_runner_2.py"
) else (
    start "KoveerBridge Runner" cmd.exe /k "%PYEXE% multi_runner_2.py"
)
timeout /t 2 /nobreak >nul

start "KoveerBridge Dashboard" cmd.exe /k "%PYEXE% dashboard_server_2.py"
timeout /t 2 /nobreak >nul

start "KoveerBridge Chart" cmd.exe /k "%PYEXE% dashboard_with_chart.py"

echo.
echo ============================================
echo   Dashboard : http://127.0.0.1:8080
echo   Chart     : http://127.0.0.1:8081
echo   LAN       : http://YOUR-LAN-IP:8080
echo ============================================
pause
