# Codex共享记忆首次初始化；仅在当前可见终端接收用户输入，不输出口令。
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$runtime=Join-Path $root 'runtime\codex'
if ((Test-Path -LiteralPath "$runtime\config\admin_auth.json") -or (Test-Path -LiteralPath "$runtime\data\sqlite\memory_hub.db")) { Write-Host '已有正式认证或数据库，已保留；无需重复初始化。'; exit 0 }
$first=Read-Host '设置正式记忆看板管理员口令（至少12字符，不要发到聊天）' -AsSecureString
$second=Read-Host '再次输入同一口令' -AsSecureString
$a=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($first)
$b=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($second)
$previousEncoding=$OutputEncoding
try {
    $plain=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($a)
    $confirmed=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($b)
    if ($plain.Length -lt 12 -or $plain -cne $confirmed) { throw '两次口令不一致或长度不足；未初始化。' }
    $OutputEncoding=[System.Text.UTF8Encoding]::new($false)
    $env:PYTHONDONTWRITEBYTECODE='1'
    $plain | & "$root\.venv-0.3.0\Scripts\python.exe" -X utf8 -B "$root\scripts\initialize_codex.py"
    if ($LASTEXITCODE -ne 0) { throw '初始化未通过；已有内容保持。' }
    Write-Host '初始化完成。口令只保存在你心中，服务仅保存加盐哈希。'
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($a);[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b)
    $plain=$null;$confirmed=$null;$first.Dispose();$second.Dispose();$OutputEncoding=$previousEncoding
}
