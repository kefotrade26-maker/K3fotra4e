@echo off
setlocal
cd /d "%~dp0"
call "%~dp0stop_windows.bat"
timeout /t 3 /nobreak >nul
call "%~dp0start_windows.bat"
