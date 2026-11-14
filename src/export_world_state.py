import argparse
import asyncio
import queue
import time
from datetime import datetime
from pathlib import Path

from modules.config_dataclass import MinecraftObservationConnectionConfig, MinecraftWorldConfig
from modules.world_state_file import build_world_snapshot_data, save_world_state


MINECRAFT_WORLD_CFG = MinecraftWorldConfig(
        env_box=[[-20, -61, -20],[20, -55, 20]]
    )

MINECRAFT_OBSERVATION_CONNECTION_CFG = MinecraftObservationConnectionConfig(
        host="minecraft",
        port=7891
    )

def build_default_output_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return Path(__file__).resolve().parent / "world_states" / f"world_snapshot_{timestamp}.yml"


async def export_world_state(*, output_path: str | Path, observation_timeout: float, admin_mc_name: str | None = None) -> Path:
    from modules.belief import WorldState
    from modules.minecraft_observation import MinecraftObservationProcessor
    from modules.observation_source import MinecraftObservationConnection

    output_path = Path(output_path).resolve()
    env_box = MINECRAFT_WORLD_CFG.env_box
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    log_dir = output_path.parent / ".export_logs" / timestamp
    observation_connection = MinecraftObservationConnection(
        log_dir=str(log_dir),
        host=MINECRAFT_OBSERVATION_CONNECTION_CFG.host,
        port=MINECRAFT_OBSERVATION_CONNECTION_CFG.port,
        env_box=env_box,
        logger_name=f"world_state_export.{timestamp}.observation",
    )

    try:
        initial_state, snapshot_info = await asyncio.to_thread(
            observation_connection.connect, include_snapshot_info=True,
        )
        observation_processor = MinecraftObservationProcessor(initial_tick=initial_state["globalTick"], admin_mc_name=admin_mc_name)
        queued_tick_observations: queue.Queue[dict] = queue.Queue()
        world_state = WorldState(initial_state)
        deadline = time.monotonic() + observation_timeout
        while time.monotonic() < deadline:
            if queued_tick_observations.empty():
                message_from_minecraft = await asyncio.to_thread(
                    observation_connection.receive, timeout=min(0.1, max(0.0, deadline - time.monotonic())),
                )
                if message_from_minecraft is None:
                    continue
                tick_observations = observation_processor.process(message_from_minecraft)
                if not tick_observations:
                    continue
                tick_observation = tick_observations[0]
                for remaining_tick_observation in tick_observations[1:]:
                    queued_tick_observations.put(remaining_tick_observation)
            else:
                tick_observation = queued_tick_observations.get_nowait()
            world_state.update(tick_observation)
            if tick_observation["objective"]["status"]:
                break

        snapshot_is_complete = snapshot_info["actual_count"] == snapshot_info["expected_count"]
        world_state_data = build_world_snapshot_data(
            world_state.to_dict(),
            env_box=env_box,
            snapshot_is_complete=snapshot_is_complete,
        )
        save_world_state(output_path, world_state_data)
    finally:
        await asyncio.to_thread(observation_connection.close)
    return output_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export the observed Minecraft world as an editable WorldState YAML file.")
    parser.add_argument("--output", default=None)
    parser.add_argument("--observation-timeout", type=float, default=30.0)
    parser.add_argument("--admin-mc-name", default="admin")
    args = parser.parse_args()
    if args.observation_timeout <= 0:
        parser.error("--observation-timeout must be positive")

    output_path = args.output
    if output_path is None: output_path = build_default_output_path()

    asyncio.run(export_world_state(output_path=output_path, observation_timeout=args.observation_timeout, admin_mc_name=args.admin_mc_name))
    print("WorldState export completed.")
