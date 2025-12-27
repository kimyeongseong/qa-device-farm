@echo off
echo Starting QA Device Farm...

:: Both servers are launched from this script's own directory, so the repo can
:: live anywhere. Python and Node are taken from PATH.
cd /d "%~dp0"

where python >nul 2>&1 || (echo [ERROR] python not found on PATH & pause & exit /b 1)
where npm    >nul 2>&1 || (echo [ERROR] npm not found on PATH & pause & exit /b 1)

:: Access token. Leave unset on a trusted network; set it before exposing the
:: farm and every API call then needs it. See "접근 토큰" in the README.
:: set "DEVICE_FARM_TOKEN=put-a-long-random-string-here"

:: The stream port is defined once, in ws-scrcpy.config.json. ws-scrcpy reads it
:: through this variable; server.py reads the same file to tell the dashboard.
set "WS_SCRCPY_CONFIG=%~dp0ws-scrcpy.config.json"
if not exist "%WS_SCRCPY_CONFIG%" (
    echo [WARN] ws-scrcpy.config.json missing - ws-scrcpy will fall back to port 8000,
    echo        which collides with common dev tooling. See the README.
)

:: ws-scrcpy shells out to `adb` from PATH (it spawns the process itself rather
:: than speaking the protocol), so a bundled copy that only server.py knows about
:: leaves the stream server unable to push scrcpy onto the device -- it fails with
:: "spawn adb ENOENT" while /api/health still reports adb as fine. Put the bundled
:: directory on PATH for both children so the two agree on which adb exists.
if exist "%~dp0scrcpy_bin\adb.exe" set "PATH=%~dp0scrcpy_bin;%PATH%"

:: Start Backend (Python FastAPI)
start "Backend Server (8001)" cmd /k "python server.py"

:: Start Frontend (Node.js / ws-scrcpy)
start "Stream Server" /d "%~dp0ws-scrcpy" cmd /k npm start

echo Servers are starting...
echo ===================================================
echo [MAIN DASHBOARD]: http://localhost:8001/
echo [API DOCS]:       http://localhost:8001/docs
echo [Stream Server]:  see ws-scrcpy.config.json (default 8010, internal)
echo ===================================================
echo Don't close this window.
