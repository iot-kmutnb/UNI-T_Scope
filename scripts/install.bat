@echo off
rem Double-click or run from cmd: creates .venv and installs PyQt6 and the other packages (see install.ps1 for options).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set RC=%ERRORLEVEL%
if "%1"=="" pause
exit /b %RC%
