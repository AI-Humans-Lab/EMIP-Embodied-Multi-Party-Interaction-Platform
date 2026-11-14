@echo off
cd /d "%~dp0.."
docker container inspect easyllm_agent_anne >nul 2>&1
if not errorlevel 1 (
    docker container stop easyllm_agent_anne >nul
    docker container rm easyllm_agent_anne >nul
)
docker compose run --build --rm --name easyllm_agent_anne --no-deps python_runtime python -u main.py --config-module anne_config
if errorlevel 1 pause
