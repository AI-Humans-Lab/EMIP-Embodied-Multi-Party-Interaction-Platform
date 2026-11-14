@echo off
setlocal
cd /d "%~dp0.."

docker network inspect bnnet >nul 2>&1 || docker network create bnnet >nul
if errorlevel 1 goto error

docker compose up -d --build --wait minecraft mineflayer
if errorlevel 1 goto error

docker compose run --build --rm --name easyllm_world_state_exporter --no-deps python_runtime python -u export_world_state.py %*
if errorlevel 1 goto error

echo.
for %%I in ("%~dp0..\src\world_states") do echo Output directory: %%~fI
pause
exit /b 0

:error
echo.
echo World state export failed.
pause
exit /b 1
