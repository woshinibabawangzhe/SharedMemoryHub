# SharedMemoryHub v0.3.0；核对进程创建时间和程序身份，正常退出后确认端口释放。
param([string]$RuntimeRoot="")
$ErrorActionPreference="Stop"
$HubRoot=Split-Path -Parent $PSScriptRoot
if (!$RuntimeRoot) { $RuntimeRoot=$HubRoot }
& "$HubRoot\.venv-0.3.0\Scripts\python.exe" -X utf8 -B "$HubRoot\scripts\runtime_control.py" stop --runtime $RuntimeRoot
if ($LASTEXITCODE -ne 0) { throw "停止未确认；未执行强杀。" }
