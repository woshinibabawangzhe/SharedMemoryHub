# SharedMemoryHub v0.3.0；保留既有认证与数据，检测旧 Schema 时停止。
param([string]$RuntimeRoot="",[int]$Port=8765,[int]$QdrantPort=6333,[string]$Prefix="memories",[ValidateSet("cuda","cpu")][string]$Device="cuda")
$ErrorActionPreference="Stop"
$HubRoot=Split-Path -Parent $PSScriptRoot
if (!$RuntimeRoot) { $RuntimeRoot=$HubRoot }
& "$HubRoot\.venv-0.3.0\Scripts\python.exe" -X utf8 -B "$HubRoot\scripts\runtime_control.py" start --runtime $RuntimeRoot --port $Port --qdrant-port $QdrantPort --prefix $Prefix --device $Device
if ($LASTEXITCODE -ne 0) { throw "启动未通过；请查看指定运行目录的 logs。" }
