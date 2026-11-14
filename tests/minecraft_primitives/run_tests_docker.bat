@echo off
setlocal
cd /d "%~dp0..\.."
set "SKIP_CONFIRMATION="
set "EASYLLM_WATCH_MINECRAFT_PRIMITIVES=1"
for %%A in (%*) do (
    if /I "%%~A"=="--yes" set "SKIP_CONFIRMATION=1"
    if /I "%%~A"=="-Batch" set "EASYLLM_WATCH_MINECRAFT_PRIMITIVES=0"
)

echo Start Docker Desktop.
echo Start Minecraft on port 25565 and Mineflayer, and wait until both are ready.
echo You can start both by double-clicking individual_launchers/start_minecraft_environment.bat.
echo.
echo Use a world you are willing to modify or discard.
echo If you use the BAT above and want to preserve its world, stop both services
echo and move minecraft_server_on_docker/flat/25565/data aside before starting.
echo.
if not defined SKIP_CONFIRMATION set /p "CONFIRMATION=Press Enter to continue..."

docker compose run --build --rm --no-deps -w /app -v "%CD%\tests:/app/tests:ro" ^
  -e PYTHONPATH=/app/src -e EASYLLM_RUN_MINECRAFT_PRIMITIVES=1 ^
  -e EASYLLM_WATCH_MINECRAFT_PRIMITIVES=%EASYLLM_WATCH_MINECRAFT_PRIMITIVES% ^
  -e EASYLLM_TEST_MINEFLAYER_HOST=mineflayer -e EASYLLM_TEST_OBSERVATION_HOST=minecraft ^
  python_runtime python -m unittest discover -s tests/minecraft_primitives -v
if not defined SKIP_CONFIRMATION pause
