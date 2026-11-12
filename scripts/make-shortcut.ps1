# 生成/修复启动快捷方式（换机器或移动目录后重跑一次即可）
# 用法：在项目根目录执行
#   powershell -ExecutionPolicy Bypass -File scripts\make-shortcut.ps1

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$pyw  = Join-Path $root '.venv\Scripts\pythonw.exe'
$ico  = Join-Path $root 'assets\w2d.ico'

if (-not (Test-Path $pyw)) {
    Write-Host "找不到 $pyw" -ForegroundColor Red
    Write-Host "请先创建虚拟环境（注意：必须用官方签名的 Python，见 README「安装」一节）：" -ForegroundColor Yellow
    Write-Host '  uv venv .venv --python "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"' -ForegroundColor Yellow
    Write-Host '  uv pip install --python .venv\Scripts\python.exe -e .' -ForegroundColor Yellow
    exit 1
}

# 关键检查：pythonw 未签名会被 Windows SmartScreen 拦截（"已保护你的电脑"）
$sig = Get-AuthenticodeSignature $pyw
if ($sig.Status -ne 'Valid') {
    Write-Host "警告：$pyw 没有有效数字签名（$($sig.Status)）" -ForegroundColor Yellow
    Write-Host "双击启动时 Windows 可能弹出「已保护你的电脑」。换成官方 Python 重建 .venv 即可解决。" -ForegroundColor Yellow
} else {
    Write-Host "pythonw 签名有效：$($sig.SignerCertificate.Subject -replace '^CN=([^,]+).*','$1')" -ForegroundColor Green
}

$shell = New-Object -ComObject WScript.Shell

function New-AppShortcut([string]$path) {
    $s = $shell.CreateShortcut($path)
    $s.TargetPath        = $pyw
    $s.Arguments         = '-m w2d.app'
    $s.WorkingDirectory  = $root
    $s.Description       = '公众号文章 → Word（双击启动程序窗口）'
    if (Test-Path $ico) { $s.IconLocation = "$ico,0" }
    $s.WindowStyle       = 1
    $s.Save()
    Write-Host "已创建：$path" -ForegroundColor Green
}

New-AppShortcut (Join-Path $root '公众号导出Word.lnk')

$desktop = @([Environment]::GetFolderPath('Desktop'), "$env:USERPROFILE\Desktop") |
    Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if ($desktop) { New-AppShortcut (Join-Path $desktop '公众号导出Word.lnk') }

Write-Host ""
Write-Host "完成。双击「公众号导出Word」即可启动，关闭窗口程序自动退出。" -ForegroundColor Cyan
