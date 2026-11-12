# 生成/修复启动快捷方式（换机器或移动目录后重跑一次即可）
# 用法：在项目根目录右键「使用 PowerShell 运行」，或执行
#   powershell -ExecutionPolicy Bypass -File scripts\make-shortcut.ps1

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$pyw  = Join-Path $root '.venv\Scripts\pythonw.exe'
$ico  = Join-Path $root 'assets\w2d.ico'

if (-not (Test-Path $pyw)) {
    Write-Host "找不到 $pyw" -ForegroundColor Red
    Write-Host "请先创建虚拟环境：uv venv .venv --python 3.12; uv pip install --python .venv\Scripts\python.exe -e ." -ForegroundColor Yellow
    exit 1
}

$shell = New-Object -ComObject WScript.Shell

function New-AppShortcut([string]$path) {
    $s = $shell.CreateShortcut($path)
    $s.TargetPath       = $pyw
    $s.Arguments         = '-m w2d.app'
    $s.WorkingDirectory  = $root
    $s.Description       = '公众号文章 → Word（双击启动程序窗口）'
    if (Test-Path $ico) { $s.IconLocation = "$ico,0" }
    $s.WindowStyle       = 7
    $s.Save()
    Write-Host "已创建：$path" -ForegroundColor Green
}

New-AppShortcut (Join-Path $root '公众号导出Word.lnk')

$desktop = [Environment]::GetFolderPath('Desktop')
if ($desktop -and (Test-Path $desktop)) {
    New-AppShortcut (Join-Path $desktop '公众号导出Word.lnk')
}

Write-Host ""
Write-Host "完成。双击「公众号导出Word」即可启动，关闭窗口程序自动退出。" -ForegroundColor Cyan
