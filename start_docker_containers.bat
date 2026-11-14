@echo off

cd /d "%~dp0"

docker network inspect bnnet >nul 2>&1 || docker network create bnnet >nul
if errorlevel 1 goto error

call "%~dp0individual_launchers\start_VOICEVOX.bat"
if errorlevel 1 goto error

docker compose up -d --build --force-recreate --wait --wait-timeout 600 minecraft mineflayer
if errorlevel 1 goto error

start "minecraft" "%ComSpec%" /d /c docker attach mc_server_flat_25565 ^|^| pause
start "sally" "%~dp0individual_launchers\start_sally_agent.bat"
exit /b 0

:error
pause
exit /b 1
