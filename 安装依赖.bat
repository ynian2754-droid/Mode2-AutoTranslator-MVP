@echo off
setlocal
cd /d "%~dp0"

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_dependencies.ps1"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
    echo All project dependencies are ready.
) else (
    echo Dependency installation failed. Review the message above.
)
pause
exit /b %EXIT_CODE%
