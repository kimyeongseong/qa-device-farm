@echo off
echo Starting QA Device Farm...

:: Both servers are launched from this script's own directory, so the repo can
:: live anywhere. Python and Node are taken from PATH.
cd /d "%~dp0"

where python >nul 2>&1 || (echo [ERROR] python not found on PATH & pause & exit /b 1)
where npm    >nul 2>&1 || (echo [ERROR] npm not found on PATH & pause & exit /b 1)

:: Start Backend (Python FastAPI)
start "Backend Server (8001)" cmd /k "python server.py"

:: Start Frontend (Node.js / ws-scrcpy)
start "Frontend Server (8000)" cmd /k "cd /d "%~dp0ws-scrcpy" && npm start"

echo Servers are starting...
echo ===================================================
echo [MAIN DASHBOARD]: http://localhost:8001/
echo [API DOCS]:       http://localhost:8001/docs
echo [Stream Server]:  http://localhost:8000 (Internal)
echo ===================================================
echo Don't close this window.
