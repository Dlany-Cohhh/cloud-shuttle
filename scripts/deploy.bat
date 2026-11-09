@echo off
chcp 65001 >nul
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1" %*
set "code=%ERRORLEVEL%"
echo.
echo (Press any key to close this window)
pause >nul
exit /b %code%
