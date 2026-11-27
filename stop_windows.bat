@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title KoveerBridge - Safe Stop

echo Stopping only KoveerBridge processes launched from this folder...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$r=[IO.Path]::GetFullPath('%~dp0').TrimEnd('\'); $p=Get-WmiObject Win32_Process | Where-Object { $_.Name -match '^python(w)?.exe$' -and $_.CommandLine -and $_.CommandLine.IndexOf($r,[StringComparison]::OrdinalIgnoreCase) -ge 0 -and ($_.CommandLine -match 'multi_runner_2.py|dashboard_server_2.py|dashboard_with_chart.py') }; if($p){ foreach($x in $p){ Write-Host ('Stopping PID ' + $x.ProcessId); Stop-Process -Id $x.ProcessId -Force -ErrorAction Continue } } else { Write-Host 'No project processes found.' }"

echo.
echo Stop completed. Data files were not touched.
pause
