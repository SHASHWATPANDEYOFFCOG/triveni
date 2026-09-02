@echo off
REM Windows shim so `make <target>` works even without GNU make installed.
setlocal
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
"%PY%" "%~dp0tasks.py" %*
