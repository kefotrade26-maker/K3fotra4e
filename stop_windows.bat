@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title KoveerBridge - Safe Stop
echo Stopping only KoveerBridge scripts launched from this folder...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$root=[IO.Path]::GetFullPath('%~dp0').TrimEnd('\'); $names=@('multi_runner_2.py','dashboard_server_2.py','dashboard_with_chart.py'); $ps=Get-WmiObject Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf($root,[StringComparison]::OrdinalIgnoreCase) -ge 0 -and (($_.CommandLine -match 'multi_runner_2\.py') -or ($_.CommandLine -match 'dashboard_server_2\.py') -or ($_.CommandLine -match 'dashboard_with_chart\.py')) }; if($ps){ foreach($p in $ps){ Write-Host ('Stopping PID '+$p.ProcessId); Stop-Process -Id $p.ProcessId -Force -ErrorAction Continue } } else { Write-Host 'No matching project processes found.' }"
echo.
pause
