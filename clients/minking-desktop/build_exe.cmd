@echo off
setlocal
set "ROOT=%~dp0"
set "PY=D:\Users\DELL\Desktop\transfer-station\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
cd /d "%ROOT%"
"%PY%" -m PyInstaller --noconfirm --clean MinKingAI.spec
if errorlevel 1 exit /b 1
if exist "%ROOT%dist\MinKingAI\MinKingAI.exe" (
  echo One-dir build is not allowed. Expected a single dist\MinKingAI.exe
  exit /b 1
)
echo EXE: %ROOT%dist\MinKingAI.exe
set "DL=%ROOT%..\..\data\downloads"
if not exist "%DL%" mkdir "%DL%"
copy /Y "%ROOT%dist\MinKingAI.exe" "%DL%\MinKingAI.exe" >nul
if errorlevel 1 (
  echo downloads\MinKingAI.exe is in use; staging MinKingAI.exe.new
  copy /Y "%ROOT%dist\MinKingAI.exe" "%DL%\MinKingAI.exe.new"
)
"%PY%" -c "import hashlib, pathlib, sys; src=pathlib.Path(sys.argv[1]); root=pathlib.Path(sys.argv[2]); digest=hashlib.sha256(src.read_bytes()).hexdigest(); (root/'MinKingAI.exe.sha256').write_text(digest+'  MinKingAI.exe\n', encoding='utf-8'); print('SHA256', digest)" "%ROOT%dist\MinKingAI.exe" "%DL%"
