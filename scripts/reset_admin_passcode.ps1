# v0.3.0 首次管理员初始化；保留已有认证，不再支持命令参数传入口令。
param([string]$RuntimeRoot="")
$ErrorActionPreference="Stop"
$HubRoot=Split-Path -Parent $PSScriptRoot
if (!$RuntimeRoot) { $RuntimeRoot=$HubRoot }
if (Test-Path -LiteralPath "$RuntimeRoot\config\admin_auth.json") { throw "已存在管理员配置，已保留；本入口不会重置。" }
$secure=Read-Host "首次管理员口令（至少12字符）" -AsSecureString
$pointer=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
$previousEncoding=$OutputEncoding
$OutputEncoding=[System.Text.UTF8Encoding]::new($false)
try {
    $plain=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    $plain | & "$HubRoot\.venv-0.3.0\Scripts\python.exe" -X utf8 -B "$HubRoot\scripts\setup_admin.py" --runtime $RuntimeRoot
    if ($LASTEXITCODE -ne 0) { throw "初始化未通过。" }
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    $plain=$null; $secure.Dispose(); $OutputEncoding=$previousEncoding
}
