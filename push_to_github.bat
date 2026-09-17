@echo off
set "GIT_EXE=%LOCALAPPDATA%\Programs\Git\cmd\git.exe"

if not exist "%GIT_EXE%" (
    echo [ERROR] Git not found at %GIT_EXE%
    pause
    exit /b 1
)

cd /d "%~dp0"
echo ============================================================
echo  Pushing Spotify Downloader to GitHub...
echo ============================================================
echo.

"%GIT_EXE%" push -u origin main

echo.
echo Done!
pause
