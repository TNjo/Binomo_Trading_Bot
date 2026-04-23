@echo off
setlocal
title Binomo Signal Bot

cd /d "%~dp0"

if not exist ".env" (
    echo [ERROR] .env not found. Copy .env.example to .env and fill in your credentials.
    pause
    exit /b 1
)

REM Use local venv if present, else system python
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

REM Read DASHBOARD_PORT from .env (default 8001)
set "PORT=8001"
for /f "tokens=2 delims==" %%A in ('findstr /b "DASHBOARD_PORT=" .env 2^>nul') do set "PORT=%%A"

echo ============================================================
echo  Binomo Signal Bot
echo  Dashboard: http://localhost:%PORT%
echo ============================================================
echo.

REM Open dashboard in default browser after 3s (non-blocking)
start "" /b cmd /c "timeout /t 3 /nobreak >nul & start http://localhost:%PORT%"

"%PY%" main.py

echo.
echo Bot stopped. Press any key to close.
pause >nul
