@echo off
chcp 65001 >nul
title MT4 Cleanup - Koveer9

set "FILES_DIR=%~dp0Files"
set "HIST_DIR=%~dp0..\history\MetaQuotes-Demo"

echo.
echo ============================================================
echo   Cleaning up Koveer9 temporary files...
echo ============================================================

if not exist "%FILES_DIR%" goto :HistStep


if exist "%FILES_DIR%\candle_cache" (
    del /f /q "%FILES_DIR%\candle_cache\*.json" >nul 2>&1
    echo   [OK] Candle cache cleared
)

:HistStep
if not exist "%HIST_DIR%" goto :End

del /f /q "%HIST_DIR%\*.hst.tmp" >nul 2>&1
echo   [OK] Temporary HST files removed

:End
echo.
echo Cleanup finished.
timeout /t 2 /nobreak >nul
exit /b