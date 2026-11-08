@echo off
chcp 65001 >nul
pwsh.exe -NoLogo -NoProfile -File "%~dp0scripts\initialize_codex.ps1"
pause
