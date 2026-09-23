@echo off
setlocal
set "ROOT=%~dp0"
set "PYTHONPATH=%ROOT%"
set "PY=D:\Users\DELL\Desktop\transfer-station\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
cd /d "%ROOT%"
"%PY%" "%ROOT%run.py" %*
