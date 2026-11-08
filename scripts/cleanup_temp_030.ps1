# SharedMemoryHub v0.3.0 本轮临时文件清理入口；工具版本 0.1.0。
# 双击根目录 CMD 执行；-Preview 仅核验并展示，不移动或删除。
param([switch]$Preview)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ProjectRoot = [IO.Path]::GetFullPath('E:\AI运行区\SharedMemoryHub')
$ReceiptRoot = Join-Path $ProjectRoot 'receipts\v0.3.0-implementation'
$HelperHashes = [ordered]@{
    'patch_lifecycle.py' = 'b0f34c612b694db1a4950c95d4b9f805cfa3d7012291e3a9dd27b453b175c100'
    'patch_purge.py' = 'd1cafa7146083fb724132b034c2704c4903f8b0db2e246404f429a833957f1da'
    'patch_vectors.py' = 'c51cc63d092bd7c863c45923003fc9ebb34dc4309ab3ce102e7a8930ac716148'
    'patch_ui_cli.py' = '872976a256a29915c1956e22a0d0dc160a785400f8905255ead687b55e0451cc'
    'replace_runner.py' = 'c0388d7fc6d64292b4555a0d930545a21a11a6df39af8406b74a8c055ec4968a'
    'update_spec.py' = 'b1e69f9b1ed5a324d7c1a166ea5f8266374919290d52a71159f5aac6112bedaa'
}

function Assert-SafePath([string]$Target) {
    $full = [IO.Path]::GetFullPath($Target)
    if ($full -ne $ProjectRoot -and -not $full.StartsWith($ProjectRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "目标越出指定项目，已停止：$full"
    }
    $cursor = $full
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "发现符号链接或目录联接，已停止：$cursor"
            }
        }
        $parent = [IO.Directory]::GetParent($cursor)
        if ($null -eq $parent) { break }
        $cursor = $parent.FullName
    }
    return $full
}

function Get-CheckedSegments([string]$Target) {
    $null = Assert-SafePath $Target
    $items = @(Get-ChildItem -LiteralPath $Target -Force)
    if ($items.Count -ne 329) { throw '分片数量已变化，已停止；不会清理未知文件。' }
    foreach ($item in $items) {
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
            $item.Name -notmatch '^\d{4}\.segment$' -or [int]$item.BaseName -gt 328) {
            throw "分片目录有未知内容，已停止：$($item.Name)"
        }
    }
    $bytes = ($items | Measure-Object -Property Length -Sum).Sum
    if ($bytes -ne 2753196039) { throw '分片大小已变化，已停止。' }
    return $bytes
}

try {
    $null = Assert-SafePath $ProjectRoot
    $null = Assert-SafePath $PSCommandPath
    if ([IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)) -ne $ProjectRoot) {
        throw '请从原项目位置运行此脚本。'
    }
    if (([IO.DriveInfo]::new('E:\')).DriveType -ne [IO.DriveType]::Fixed) {
        throw 'E 盘不是本地固定磁盘，已停止。'
    }
    $plan = [Collections.Generic.List[object]]::new()
    $segments = Join-Path $ProjectRoot 'cache\cuda-wheel-segments'
    if (Test-Path -LiteralPath $segments) {
        $bytes = Get-CheckedSegments $segments
        $wheel = Assert-SafePath (Join-Path $ProjectRoot 'cache\wheels-0.3.0\torch-2.11.0+cu128-cp313-cp313-win_amd64.whl')
        Write-Host '正在核验保留的完整安装包，请稍候……'
        if ((Get-FileHash -LiteralPath $wheel -Algorithm SHA256).Hash.ToLowerInvariant() -ne
            '6f367e62fd81b75cdf23ca4b75ced834d2db2cf98d1588ac935bde345de9de23') {
            throw '完整安装包校验未通过，重复分片将保留。'
        }
        $plan.Add([pscustomobject]@{ Path=$segments; Directory=$true; Bytes=$bytes; Hash='' })
    }
    foreach ($entry in $HelperHashes.GetEnumerator()) {
        $path = Assert-SafePath (Join-Path $ReceiptRoot $entry.Key)
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $item = Get-Item -LiteralPath $path -Force
        if ($item.PSIsContainer -or (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.Value) {
            throw "临时脚本已改变，已停止：$($entry.Key)"
        }
        $plan.Add([pscustomobject]@{ Path=$path; Directory=$false; Bytes=$item.Length; Hash=$entry.Value })
    }
    if ($plan.Count -eq 0) { Write-Host '目标已经清理，无需操作。'; return }
    Write-Host "本次仅处理以下 $($plan.Count) 项："
    foreach ($item in $plan) { Write-Host ('  ' + $item.Path) }
    Write-Host ('总大小：{0:N2} GB；移入回收站后，清空回收站才释放磁盘空间。' -f (($plan | Measure-Object -Property Bytes -Sum).Sum / 1e9))
    Add-Type -AssemblyName Microsoft.VisualBasic
    $null = [Microsoft.VisualBasic.FileIO.FileSystem]
    if ($Preview) { Write-Host '预览通过，未移动或删除任何文件。'; return }
    foreach ($item in $plan) {
        $null = Assert-SafePath $item.Path
        if ($item.Directory) {
            $null = Get-CheckedSegments $item.Path
            [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($item.Path,
                [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
                [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin,
                [Microsoft.VisualBasic.FileIO.UICancelOption]::ThrowException)
        } else {
            if ((Get-FileHash -LiteralPath $item.Path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $item.Hash) {
                throw '执行前文件已变化，停止后续操作。'
            }
            [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($item.Path,
                [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
                [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin,
                [Microsoft.VisualBasic.FileIO.UICancelOption]::ThrowException)
        }
        if (Test-Path -LiteralPath $item.Path) { throw '未能确认移入回收站，停止后续操作。' }
        Write-Host ('已移入回收站：' + $item.Path)
    }
    Write-Host '清理完成。如需恢复，请打开回收站还原对应项目。'
} catch {
    Write-Host ('清理已停止：' + $_.Exception.Message) -ForegroundColor Red
    Write-Host '不会自动重试，也不会改为永久删除。'
    exit 1
}
