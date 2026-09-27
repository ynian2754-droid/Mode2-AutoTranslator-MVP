@echo off
setlocal
cd /d "%~dp0"

set "APP_PORT=%~1"
if "%APP_PORT%"=="" set "APP_PORT=4873"
set "APP_URL=http://127.0.0.1:%APP_PORT%/"

start "" /b powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$url='%APP_URL%'; for($i=0;$i -lt 30;$i++){ try { $response=Invoke-WebRequest -Uri ($url+'api/health') -UseBasicParsing -TimeoutSec 1; if($response.StatusCode -eq 200){ Start-Process $url; exit 0 } } catch {}; Start-Sleep -Seconds 1 }; Start-Process $url"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" -Port %APP_PORT%
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo Mode2 AutoTranslator failed to start.
    echo Exit code: %EXIT_CODE%
    pause
)
exit /b %EXIT_CODE%
