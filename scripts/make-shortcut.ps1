# 生成/修复启动快捷方式（换机器或移动目录后重跑一次即可）
# 用法：在项目根目录执行
#   powershell -ExecutionPolicy Bypass -File scripts\make-shortcut.ps1

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$pyw  = Join-Path $root '.venv\Scripts\pythonw.exe'
$icoSrc = Join-Path $root 'assets\w2d.ico'

# 图标必须放在「纯 ASCII 路径」下：实测 Shell 读取含中文路径（如 D:\...\工具\...）
# 的 .ico 会失败，快捷方式就显示成空白文档图标。
$ico = $icoSrc
$icoAsciiDir = Join-Path $env:LOCALAPPDATA 'w2d'

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

# 图标缺失时自动生成（scripts\make-icon.py）
if (-not (Test-Path $ico)) {
    $py = Join-Path $root '.venv\Scripts\python.exe'
    $gen = Join-Path $root 'scripts\make-icon.py'
    if ((Test-Path $py) -and (Test-Path $gen)) {
        Write-Host "图标不存在，正在生成..." -ForegroundColor Yellow
        & $py $gen | Out-Null
    }
}

# 部署图标到 ASCII 路径（失败则退回项目内路径）
try {
    New-Item -ItemType Directory -Path $icoAsciiDir -Force -ErrorAction Stop | Out-Null
    $deployed = Join-Path $icoAsciiDir 'app-icon.ico'
    Copy-Item $icoSrc $deployed -Force -ErrorAction Stop
    $ico = $deployed
    Write-Host "图标已部署到：$ico" -ForegroundColor Green
} catch {
    Write-Host "图标复制失败，沿用项目内路径（中文路径可能导致图标空白）" -ForegroundColor Yellow
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
