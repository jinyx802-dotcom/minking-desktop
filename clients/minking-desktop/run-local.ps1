param([int]$Port = 18787, [switch]$Headless)
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { $pythonExe = 'python' }
$localArgs = @((Join-Path $PSScriptRoot 'run.py'))
if ($Headless) { $localArgs += @('--local-api', '--port', "$Port") }
& $pythonExe @localArgs
