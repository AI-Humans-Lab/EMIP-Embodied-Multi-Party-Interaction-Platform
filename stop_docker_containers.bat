@echo off

docker container inspect emip-sally >nul 2>&1
if not errorlevel 1 (
    docker container stop emip-sally
    if errorlevel 1 goto error
)

docker compose -f "%~dp0docker-compose.yml" stop mineflayer minecraft
if errorlevel 1 goto error

docker container inspect voicevox_engine >nul 2>&1
if not errorlevel 1 (
    docker container stop voicevox_engine
    if errorlevel 1 goto error
)

echo Containers stopped. Press any key to continue...
pause >nul
exit /b 0

:error
echo Failed to stop containers. Press any key to continue...
pause >nul
exit /b 1
