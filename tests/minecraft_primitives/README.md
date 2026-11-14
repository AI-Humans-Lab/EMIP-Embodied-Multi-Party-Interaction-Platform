# Primitive behavior tests in Minecraft

There are two ways to run these tests. Minecraft and Mineflayer run in Docker either way.

- **Docker Python:** Double-click the BAT to run tests in a temporary container; host Python is not required.
- **Host Python:** Install Python locally and use commands to select all or specific tests.

Host Python connects to Mineflayer at `127.0.0.1:3000` and Minecraft observation at `127.0.0.1:7891`. Docker Python connects using the Compose service names `mineflayer` and `minecraft`. A Minecraft client connects to `localhost:25565`.

The suite covers `craftItem`, `goToPosition`, `mineBlock`, `placeItem`, `smeltItem`, chest operations, lever operations, `giveItemToOther`, `receiveItemFromOther`, and `think`. It uses a second Mineflayer bot for item-transfer scenarios.

**Use a world that you are willing to modify or discard.** Each scenario resets blocks in the test area, agent positions and inventories, and removes all dropped items in the world. Docker Compose always mounts `minecraft_server_on_docker/flat/25565/data`. If you want to preserve that world, stop Minecraft and Mineflayer and move the `data` folder aside before starting. A new world will be created on startup. Stop both services before restoring any data you moved aside. The scripts do not swap world data.

## Run the tests

Start Docker Desktop and, if needed, move the data aside. Then double-click `individual_launchers/start_minecraft_environment.bat` to start Minecraft and Mineflayer. It does not start the Agent. The Minecraft server uses the Fabric 1.21 and easy-llm Mod configuration in `docker-compose.yml`; Mineflayer uses the same Compose setup.

### Docker Python

Once Minecraft and Mineflayer are running, double-click `tests/minecraft_primitives/run_tests_docker.bat`. By default, it displays test names and results in Minecraft, waits 30 seconds before the first scenario, and pauses between scenarios. The results window waits for a keypress when testing finishes.

The BAT displays prerequisites and waits for Enter. Confirm that the environment is ready before proceeding.

### Host Python (optional)

Only host execution requires Python 3.11 and the test packages. Run all PowerShell commands below from the repository root (the folder containing `docker-compose.yml`).

```powershell
python -m pip install "numpy>=1.26" "websockets>=12" websocket-client pyyaml
```

Once Minecraft and Mineflayer are running, open another PowerShell terminal and configure imports and the real-Minecraft test flag:

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
$env:EASYLLM_RUN_MINECRAFT_PRIMITIVES = "1"
```

Run all tests:

```powershell
python -m unittest discover -s tests/minecraft_primitives -v
```

Run one primitive's tests (example: `craftItem`):

```powershell
python -m unittest discover -s tests/minecraft_primitives -v -k test_craftItem_
```

Run one test (example: `goToPosition` test 07):

```powershell
python -m unittest discover -s tests/minecraft_primitives -v -k test_goToPosition_07_
```

`-k` matches part of a test name; if several names match, all of them run. To find names, use `Select-String -Path tests/minecraft_primitives/test_primitives_in_minecraft.py -Pattern 'async def test_'`. To watch test names and results in Minecraft, also set `$env:EASYLLM_WATCH_MINECRAFT_PRIMITIVES = "1"` before running. Watch mode waits 30 seconds before the first scenario and pauses between scenarios.

Normal test discovery skips these real-Minecraft tests unless the run flag or watch mode is enabled. Clear any host variables with `Remove-Item Env:\EASYLLM_RUN_MINECRAFT_PRIMITIVES, Env:\EASYLLM_WATCH_MINECRAFT_PRIMITIVES -ErrorAction SilentlyContinue`.

Neither method stops Minecraft/Mineflayer or swaps world data. If you moved data aside, stop both containers before restoring it after testing. Python observation logs are stored under `easyllm-minecraft-primitives` in the temporary directory (and disappear with the temporary Docker container).

## Optional: skip input prompts

In Command Prompt, run `run_tests_docker.bat --yes` from the BAT's folder to skip input prompts. Use `run_tests_docker.bat -Batch --yes` to also skip watch-mode waits.
