"""Measure ten observation agents with one Python process per agent."""

import asyncio
from functools import partial
import json
import logging
import multiprocessing
import os
import statistics
import threading
import time
import traceback
from contextlib import AsyncExitStack
from pathlib import Path
from tempfile import gettempdir

from mcp import Client

from modules.config_dataclass import ObservationActionConnectionConfig, MinecraftObservationConnectionConfig, MinecraftWorldConfig, MineflayerConnectionConfig
from modules.mcp_client_request import call_mcp_tool, extract_json_object_from_resource_result, extract_json_object_from_tool_result
from modules.observation_action_mcp import OBSERVATION_RUNTIME_LOGGER, build_observation_action_mcp_server
from modules.belief_mcp import build_belief_mcp_server
from modules.belief_inference import BeliefInference
from modules.belief_store import BeliefStore
from modules.mineflayerconnection import AdminMineflayerConnection, build_mineflayer_variables
from modules.transfer_observations_to_belief import continuously_transfer_observations_to_belief


MINEFLAYER_HOST = os.getenv("EASYLLM_TEST_MINEFLAYER_HOST", "mineflayer")
OBSERVATION_HOST = os.getenv("EASYLLM_TEST_OBSERVATION_HOST", "minecraft")
ENV_BOX = [[-16, -61, -16], [16, -55, 16]]
DURATION = float(os.getenv("EASYLLM_TEST_OBSERVATION_DURATION", "20"))
AGENT_COUNT = int(os.getenv("EASYLLM_TEST_AGENT_COUNT", "10"))
LOG_DIR = Path(gettempdir()) / "easyllm-observation-multiprocess"


class ProcessingRecords(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.records = []
        self.records_lock = threading.Lock()

    def emit(self, record):
        records, records_lock = self.records, self.records_lock
        values = getattr(record, "observation_runtime", None)
        if isinstance(values, dict) and values.get("event") == "processed":
            with records_lock:
                records.append((time.perf_counter(), values.copy()))

    def between(self, start, end):
        records, records_lock = self.records, self.records_lock
        with records_lock:
            return [values for at, values in records if start <= at <= end]


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


async def run_worker(name, commands, output):
    config = ObservationActionConnectionConfig(
        mineflayer=MineflayerConnectionConfig(MINEFLAYER_HOST, 3000),
        observation=MinecraftObservationConnectionConfig(OBSERVATION_HOST, 7891),
    )
    records = ProcessingRecords()
    OBSERVATION_RUNTIME_LOGGER.disabled = False
    OBSERVATION_RUNTIME_LOGGER.setLevel(logging.INFO)
    OBSERVATION_RUNTIME_LOGGER.addHandler(records)
    async with AsyncExitStack() as stack:
        server = build_observation_action_mcp_server(
            connection_config=config, minecraft_world_config=MinecraftWorldConfig(ENV_BOX, visibility_distance=20),
            primitives=[], log_dir=str(LOG_DIR), should_enqueue_observations=True,
        )
        minecraft_client = await stack.enter_async_context(Client(server))
        await call_mcp_tool(minecraft_client, "setup_observation_action", {"agent_name": name})

        async def status():
            result = await minecraft_client.read_resource("minecraft-observation://status")
            return extract_json_object_from_resource_result(result, "minecraft-observation://status")

        async def position():
            result = await minecraft_client.read_resource("minecraft-observation://latest-observation")
            observation = extract_json_object_from_resource_result(result, "minecraft-observation://latest-observation")
            return observation["objective"]["status"][name]["visible"]["position"]["__Vec3__"]

        initial_status = await status()
        belief = BeliefStore(initial_state={"globalTick": initial_status["initial_tick"], "status": {}, "blocks": {"__Vec3Map__": []},
                                        "containers": {"__Vec3Map__": []}}, agent_names=[name])
        belief_client = await stack.enter_async_context(Client(build_belief_mcp_server(get_store=lambda: belief, observer_name=name, infer_other_belief=partial(asyncio.to_thread, BeliefInference(belief, name, ENV_BOX, 20).infer_other_belief))))
        transfer_task = asyncio.create_task(continuously_transfer_observations_to_belief(minecraft_client, belief, name))
        output.put({"name": name, "event": "ready"})
        try:
            while True:
                command = await asyncio.to_thread(commands.get)
                if command["event"] == "stop":
                    break
                walking = command["walking"]
                if walking:
                    code = """
if (bot._obsPerfTimer) clearInterval(bot._obsPerfTimer);
let forward = true;
bot.setControlState('forward', true);
bot._obsPerfTimer = setInterval(() => {
    forward = !forward;
    bot.setControlState('forward', forward);
    bot.setControlState('back', !forward);
}, 2000);
"""
                else:
                    code = "if (bot._obsPerfTimer) clearInterval(bot._obsPerfTimer); bot._obsPerfTimer = null; bot.clearControlStates();"
                execution_result = await call_mcp_tool(minecraft_client, "exec_js", {"code": code})
                execution = extract_json_object_from_tool_result(execution_result, "exec_js")
                if not execution["success"]:
                    raise RuntimeError(execution.get("errorMsg"))
                await asyncio.sleep(1)
                first = await status()
                start_position = await position() if walking else None
                started = time.perf_counter()
                if walking:
                    await asyncio.sleep(3)
                    walking_position = await position()
                    movement = round(sum((walking_position[index] - start_position[index]) ** 2 for index in (0, 2)) ** .5, 2)
                else:
                    movement = None
                await asyncio.sleep(max(0, started + DURATION - time.perf_counter()))
                ended = time.perf_counter()
                last = await status()
                belief_result = await belief_client.read_resource("belief://status")
                belief_status = extract_json_object_from_resource_result(belief_result, "belief://status")
                rows = records.between(started, ended)
                durations = [row["processing_seconds"] * 1000 for row in rows]
                result = {
                    "name": name, "event": "measured", "case": command["case"], "observations": len(rows),
                    "processing_p95_ms": round(percentile(durations, .95), 3) if durations else None,
                    "processing_mean_ms": round(statistics.mean(durations), 3) if durations else None,
                    "achieved_tps": round((last["latest_received_tick"] - first["latest_received_tick"]) / (ended - started), 3),
                    "pending_end": last["pending_messages"], "pending_transfer_end": last["pending_observations"],
                    "belief_lag_ticks": last["latest_received_tick"] - belief_status["current_state_tick"],
                    "movement_after_three_seconds_blocks": movement,
                    "transfer_done": transfer_task.done(),
                }
                output.put(result)
        finally:
            transfer_task.cancel()
            await asyncio.gather(transfer_task, return_exceptions=True)
            code = "if (bot._obsPerfTimer) clearInterval(bot._obsPerfTimer); bot._obsPerfTimer = null; bot.clearControlStates();"
            await call_mcp_tool(minecraft_client, "exec_js", {"code": code})


def worker(name, commands, output):
    try:
        asyncio.run(run_worker(name, commands, output))
    except BaseException:
        output.put({"name": name, "event": "error", "traceback": traceback.format_exc()})


async def receive_results(output, count, expected):
    results = []
    while len(results) < count:
        row = await asyncio.to_thread(output.get, True, 120)
        if row["event"] == "error":
            raise RuntimeError(row["traceback"])
        if row["event"] != expected:
            raise RuntimeError(f"Expected {expected}, got {row}")
        results.append(row)
    return results


async def main():
    if DURATION < 3:
        raise ValueError("EASYLLM_TEST_OBSERVATION_DURATION must be at least 3 seconds")
    ctx = multiprocessing.get_context("spawn")
    names = [f"ObsMP_{index:02d}" for index in range(1, AGENT_COUNT + 1)]
    commands = {name: ctx.Queue() for name in names}
    output = ctx.Queue()
    processes = []
    admin = AdminMineflayerConnection(MINEFLAYER_HOST, 3000, logging.getLogger(__name__))
    try:
        await admin.connect()
        await admin.acquire(build_mineflayer_variables(ENV_BOX))
        await admin.set_speed(1.0)
        await admin.exec_mc(["/obsTick 1", "/weather clear", "/time set day"])
        await admin.exec_mc(["/fill -16 -60 -16 16 -55 16 air", "/fill -16 -61 -16 16 -61 16 stone"])
        for name in names:
            process = ctx.Process(target=worker, args=(name, commands[name], output), name=name)
            process.start()
            processes.append(process)
        await receive_results(output, len(names), "ready")
        for index, name in enumerate(names):
            await admin.teleport(name, [-8 + 5 * (index % 4), -60, -6 + 5 * (index // 4)], 0, 0, teleport_offset=[0, 0, 0])
        for case, walking in ((f"{len(names)}_still_multiprocess", False), (f"{len(names)}_walking_multiprocess", True)):
            for name in names:
                commands[name].put({"event": "measure", "case": case, "walking": walking})
            results = await receive_results(output, len(names), "measured")
            p95 = [row["processing_p95_ms"] for row in results]
            print(json.dumps({
                "case": case, "processes": len(names), "duration_sec": DURATION,
                "agent_p95_ms_min": min(p95), "agent_p95_ms_median": statistics.median(p95), "agent_p95_ms_max": max(p95),
                "achieved_tps_min": min(row["achieved_tps"] for row in results),
                "pending_end_max": max(row["pending_end"] for row in results),
                "pending_transfer_end_max": max(row["pending_transfer_end"] for row in results),
                "belief_lag_ticks_max": max(row["belief_lag_ticks"] for row in results),
                "movement_after_three_seconds_blocks_min": min(row["movement_after_three_seconds_blocks"] for row in results) if walking else None,
                "transfer_done_count": sum(row["transfer_done"] for row in results), "agents": results,
            }, ensure_ascii=False), flush=True)
    finally:
        for name in names:
            commands[name].put({"event": "stop"})
        for process in processes:
            await asyncio.to_thread(process.join, 20)
            if process.is_alive():
                process.terminate()
                await asyncio.to_thread(process.join, 5)
        await admin.set_speed(1.0)
        await admin.exec_mc(["/obsTick 1"])
        await admin.close()


if __name__ == "__main__":
    asyncio.run(main())
