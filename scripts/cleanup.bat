@echo off
rem Double-click or run from cmd: removes .venv (asks first; see cleanup.ps1 for -Force, -Caches, -Settings).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0cleanup.ps1" %*
set RC=%ERRORLEVEL%
if "%1"=="" pause
exit /b %RC%
