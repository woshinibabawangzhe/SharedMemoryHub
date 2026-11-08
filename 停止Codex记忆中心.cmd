@echo off
chcp 65001 >nul
"%~dp0.venv-0.3.0\Scripts\python.exe" -X utf8 -B "%~dp0scripts\codex_daily_control.py" stop
pause
