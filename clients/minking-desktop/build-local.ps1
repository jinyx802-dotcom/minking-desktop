$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw '请先安装项目 Python 环境和客户端依赖。' }
Push-Location -LiteralPath $PSScriptRoot
try {
    & $pythonExe -m PyInstaller --noconfirm MinKingAI.spec
    if ($LASTEXITCODE -ne 0) { throw 'EXE 构建失败。若本地版正在运行，请先从托盘退出。' }
    Write-Output (Join-Path $PSScriptRoot 'dist\MinKingAI.exe')
} finally {
    Pop-Location
}
