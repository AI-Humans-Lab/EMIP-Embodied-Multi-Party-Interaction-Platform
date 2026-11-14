@echo off
setlocal
cd /d "%~dp0..\.."
set "SKIP_CONFIRMATION="
for %%A in (%*) do if /I "%%~A"=="--yes" set "SKIP_CONFIRMATION=1"

echo Start Docker Desktop.
echo Start Minecraft on port 25565 and Mineflayer, and wait until both are ready.
echo You can start both by double-clicking individual_launchers/start_minecraft_environment.bat.
echo.
echo Use a world you are willing to modify or discard.
echo If you use the BAT above and want to preserve its world, stop both services
echo and move minecraft_server_on_docker/flat/25565/data aside before starting.
echo.
if not defined SKIP_CONFIRMATION set /p "CONFIRMATION=Press Enter to continue..."

if not defined EASYLLM_TEST_ENV_BOX set "EASYLLM_TEST_ENV_BOX=[[-16,-61,-16],[16,-55,16]]"
if not defined EASYLLM_TEST_ADMIN_ENV_BOX set "EASYLLM_TEST_ADMIN_ENV_BOX=[[-16,-61,-16],[16,-55,16]]"
if not defined EASYLLM_TEST_ENV_BOXES set "EASYLLM_TEST_ENV_BOXES=[[[-16,-61,-16],[16,-55,16]],[[-32,-61,-32],[32,-55,32]]]"
if not defined EASYLLM_TEST_VISIBILITY_DISTANCES set "EASYLLM_TEST_VISIBILITY_DISTANCES=[10,20,40]"
if not defined EASYLLM_TEST_VISIBILITY_ENV_BOX set "EASYLLM_TEST_VISIBILITY_ENV_BOX=[[-48,-61,-48],[48,-55,48]]"
if not defined EASYLLM_TEST_OBSERVATION_DURATION set "EASYLLM_TEST_OBSERVATION_DURATION=30"
if not defined EASYLLM_TEST_CHAT_TRIALS set "EASYLLM_TEST_CHAT_TRIALS=5"
echo Observation tests take several minutes; measurement windows alone total 6 min 30 sec.
docker compose run --build --rm --no-deps -w /app -v "%CD%\tests:/app/tests:ro" ^
  -e PYTHONPATH=/app/src ^
  -e EASYLLM_TEST_MINEFLAYER_HOST=mineflayer -e EASYLLM_TEST_OBSERVATION_HOST=minecraft ^
  -e EASYLLM_TEST_ENV_BOX -e EASYLLM_TEST_ADMIN_ENV_BOX -e EASYLLM_TEST_ENV_BOXES ^
  -e EASYLLM_TEST_VISIBILITY_DISTANCES -e EASYLLM_TEST_VISIBILITY_ENV_BOX ^
  -e EASYLLM_TEST_OBSERVATION_DURATION -e EASYLLM_TEST_CHAT_TRIALS -e EASYLLM_ENFORCE_PROMPT_TARGET ^
  python_runtime python -m unittest discover -s tests/minecraft_observation -v
if not defined SKIP_CONFIRMATION pause
