@echo off
cd /d "%~dp0.."
docker container inspect emip-sally >nul 2>&1
if not errorlevel 1 (
    docker container stop emip-sally >nul
    docker container rm emip-sally >nul
)
docker compose run --build --rm --name emip-sally --no-deps -p 127.0.0.1:8000:8000 python_runtime python -u main.py --config-module sally_config
if errorlevel 1 pause
