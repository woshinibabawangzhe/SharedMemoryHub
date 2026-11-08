@echo off
setlocal
set "SMH_CLEANUP_PS=C:\Program Files\PowerShell\7\pwsh.exe"
if not exist "%SMH_CLEANUP_PS%" set "SMH_CLEANUP_PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
"%SMH_CLEANUP_PS%" -NoLogo -NoProfile -File "%~dp0scripts\cleanup_temp_030.ps1"
set "SMH_CLEANUP_EXIT=%ERRORLEVEL%"
echo.
pause
exit /b %SMH_CLEANUP_EXIT%
