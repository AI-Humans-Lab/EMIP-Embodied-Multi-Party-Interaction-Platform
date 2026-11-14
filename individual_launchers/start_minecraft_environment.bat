@echo off
cd /d "%~dp0.."
docker network inspect bnnet >nul 2>&1 || docker network create bnnet >nul
call "%~dp0start_VOICEVOX.bat"
docker compose up -d --build --force-recreate --wait --wait-timeout 600 minecraft mineflayer && docker attach mc_server_flat_25565
if errorlevel 1 pause
