@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title KoveerBridge - Restart

call "%~dp0stop_windows.bat"
timeout /t 3 /nobreak >nul
call "%~dp0start_windows.bat"
