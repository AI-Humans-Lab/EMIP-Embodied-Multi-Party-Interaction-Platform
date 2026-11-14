import asyncio
from functools import partial
import json
import logging
import os
import statistics
import threading
import time
import unittest
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import gettempdir
from uuid import uuid4

from mcp import Client

from modules.config_dataclass import ObservationActionConnectionConfig, MinecraftObservationConnectionConfig, MinecraftWorldConfig, MineflayerConnectionConfig
from modules.mcp_client_request import call_mcp_tool, extract_json_object_from_resource_result, extract_json_object_from_tool_result
from modules.observation_action_mcp import OBSERVATION_RUNTIME_LOGGER, build_observation_action_mcp_server
from modules.belief_mcp import build_belief_mcp_server
from modules.belief_inference import BeliefInference
from modules.belief_store import BeliefStore
from modules.transfer_observations_to_belief import continuously_transfer_observations_to_belief
from modules.minecraft_observation import MinecraftObservationProcessor, _unpack_minecraft_message
from modules.mineflayerconnection import AdminMineflayerConnection, build_mineflayer_variables
from modules.observation_source import MinecraftObservationConnection


MINEFLAYER_HOST = os.getenv("EASYLLM_TEST_MINEFLAYER_HOST", "127.0.0.1")
OBSERVATION_HOST = os.getenv("EASYLLM_TEST_OBSERVATION_HOST", "127.0.0.1")
ENV_BOX = json.loads(os.getenv("EASYLLM_TEST_ENV_BOX", "[[-16,-61,-16],[16,-55,16]]"))
ADMIN_ENV_BOX = json.loads(os.getenv("EASYLLM_TEST_ADMIN_ENV_BOX", "[[-16,-61,-16],[16,-55,16]]"))
ENV_BOXES = json.loads(os.getenv("EASYLLM_TEST_ENV_BOXES", "[[[-16,-61,-16],[16,-55,16]],[[-32,-61,-32],[32,-55,32]]]"))
VISIBILITY_DISTANCES = json.loads(os.getenv("EASYLLM_TEST_VISIBILITY_DISTANCES", "[10,20,40]"))
VISIBILITY_ENV_BOX = json.loads(os.getenv("EASYLLM_TEST_VISIBILITY_ENV_BOX", "[[-48,-61,-48],[48,-55,48]]"))
DURATION = float(os.getenv("EASYLLM_TEST_OBSERVATION_DURATION", "30"))
CHAT_TRIALS = int(os.getenv("EASYLLM_TEST_CHAT_TRIALS", "5"))
LOG_DIR = Path(gettempdir()) / "easyllm-observation-performance"
PROMPT = (Path(__file__).resolve().parents[2] / "src/prompts/coding_llm_human_prompt_template_jp.txt").read_text(encoding="utf-8")
STATUS_URI = "minecraft-observation://status"


class ProcessingRecords(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.records = []
        self.records_lock = threading.Lock()

    def emit(self, record):
        records, records_lock = self.records, self.records_lock
        values = getattr(record, "observation_runtime", None)
        if not isinstance(values, dict) or values["event"] != "processed":
            return
        with records_lock:
            records.append((time.perf_counter(), values.copy()))

    def between(self, start, end, names):
        records, records_lock = self.records, self.records_lock
        with records_lock:
            return [{**values, "recorded_at": at} for at, values in records if start <= at <= end and values["agent_name"] in names]


@dataclass
class TestConnections:
    clients: dict[str, Client]
    names: list[str]
    stack: AsyncExitStack
    control: AdminMineflayerConnection
    admin_ready: bool
    belief_clients: dict[str, Client] = field(default_factory=dict)
    transfer_tasks: dict[str, asyncio.Task] = field(default_factory=dict)


@dataclass
class TestMeasurements:
    failures: list[str]
    records: ProcessingRecords
    previous_logger_state: tuple[bool, int]


class ChatOracle:
    def __init__(self, connection):
        self.connection = connection
        self.stop = threading.Event()
        self.seen = {}
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._consume, daemon=True)

    def _consume(self):
        connection, stop, seen, lock = self.connection, self.stop, self.seen, self.lock
        try:
            while not stop.is_set():
                message_from_minecraft = connection.receive(0.2)
                if message_from_minecraft is None:
                    continue
                for record_from_minecraft in _unpack_minecraft_message(message_from_minecraft):
                    if record_from_minecraft.get("type") == "chat":
                        message = record_from_minecraft["data"]["message"]
                        with lock:
                            seen[message] = time.perf_counter()
        except Exception as error:
            with lock:
                seen[None] = error

    async def wait(self, marker, timeout=20):
        seen, lock = self.seen, self.lock
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            with lock:
                observed_at = next((at for message, at in seen.items() if message is not None and marker in message), None)
                error = seen.get(None)
            if error is not None:
                raise RuntimeError("Independent chat observation failed") from error
            if observed_at is not None:
                return observed_at
            await asyncio.sleep(0.01)
        raise TimeoutError(f"Independent observation connection did not see chat {marker}")

    async def close(self):
        connection, stop, thread = self.connection, self.stop, self.thread
        stop.set()
        await asyncio.to_thread(connection.close)
        if thread.ident is not None:
            await asyncio.to_thread(thread.join)


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


class MinecraftObservationPerformance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        test_name = self._testMethodName
        asyncio.get_running_loop().set_debug(False)
        control = AdminMineflayerConnection(MINEFLAYER_HOST, 3000, logging.getLogger(__name__))
        records = ProcessingRecords()
        connections = TestConnections({}, [f"Obs{test_name.split('_')[2]}_{i:02d}" for i in range(1, 11)], AsyncExitStack(), control, False)
        self.connections = connections
        self.env_box = ENV_BOX
        self.visibility_distance = 20.0
        self.measurements = TestMeasurements([], records, (OBSERVATION_RUNTIME_LOGGER.disabled, OBSERVATION_RUNTIME_LOGGER.level))
        OBSERVATION_RUNTIME_LOGGER.disabled = False
        OBSERVATION_RUNTIME_LOGGER.setLevel(logging.INFO)
        OBSERVATION_RUNTIME_LOGGER.addHandler(records)
        self.addAsyncCleanup(self._cleanup)
        await control.connect()
        await control.acquire(build_mineflayer_variables(ADMIN_ENV_BOX))
        connections.admin_ready = True
        await control.set_speed(1.0)
        await control.exec_mc(["/obsTick 1", "/weather clear", "/time set day"])
        await control.exec_mc(["/fill -16 -60 -16 16 -55 16 air", "/fill -16 -61 -16 16 -61 16 stone"])

    async def _cleanup(self):
        connections, measurements = self.connections, self.measurements
        control = connections.control
        try:
            if connections.admin_ready and control.ws is not None:
                await control.set_speed(1.0)
                await control.exec_mc(["/obsTick 1"])
        finally:
            await control.close()
            OBSERVATION_RUNTIME_LOGGER.removeHandler(measurements.records)
            OBSERVATION_RUNTIME_LOGGER.disabled, OBSERVATION_RUNTIME_LOGGER.level = measurements.previous_logger_state

    async def _close_agents(self):
        clients, stack = self.connections.clients, self.connections.stack
        try:
            for name in clients:
                try:
                    await self._execute(name, "if (bot._obsPerfTimer) clearInterval(bot._obsPerfTimer); bot._obsPerfTimer = null; bot.clearControlStates();")
                except Exception:
                    pass
        finally:
            for task in self.connections.transfer_tasks.values():
                task.cancel()
            await asyncio.gather(*self.connections.transfer_tasks.values(), return_exceptions=True)
            self.connections.transfer_tasks.clear()
            try:
                await stack.aclose()
            finally:
                clients.clear()
                self.connections.belief_clients.clear()
                self.connections.stack = AsyncExitStack()

    async def _execute(self, name, code):
        client = self.connections.clients[name]
        result = await call_mcp_tool(client, "exec_js", {"code": code})
        execution = extract_json_object_from_tool_result(result, "exec_js")
        self.assertTrue(execution["success"], execution.get("errorMsg"))

    async def _add_agents(self, count):
        clients, names, stack, control, env_box = self.connections.clients, self.connections.names, self.connections.stack, self.connections.control, self.env_box
        belief_clients, transfer_tasks = self.connections.belief_clients, self.connections.transfer_tasks
        config = ObservationActionConnectionConfig(
            mineflayer=MineflayerConnectionConfig(MINEFLAYER_HOST, 3000),
            observation=MinecraftObservationConnectionConfig(OBSERVATION_HOST, 7891),
        )
        for name in names[len(clients):count]:
            server = build_observation_action_mcp_server(
                connection_config=config, minecraft_world_config=MinecraftWorldConfig(env_box, visibility_distance=self.visibility_distance),
                primitives=[], log_dir=str(LOG_DIR), should_enqueue_observations=True,
            )
            clients[name] = await stack.enter_async_context(Client(server))
            await call_mcp_tool(clients[name], "setup_observation_action", {"agent_name": name})
            status = await self._resource(name, STATUS_URI)
            belief = BeliefStore(initial_state={"globalTick": status["initial_tick"], "status": {}, "blocks": {"__Vec3Map__": []},
                                        "containers": {"__Vec3Map__": []}}, agent_names=[name])
            belief_clients[name] = await stack.enter_async_context(Client(build_belief_mcp_server(get_store=lambda belief=belief: belief, observer_name=name, infer_other_belief=partial(asyncio.to_thread, BeliefInference(belief, name, env_box, self.visibility_distance).infer_other_belief))))
            transfer_tasks[name] = asyncio.create_task(continuously_transfer_observations_to_belief(clients[name], belief, name))
            index = names.index(name)
            x, z = -8 + 5 * (index % 4), -6 + 5 * (index // 4)
            await control.teleport(name, [x, -60, z], 0, 0, teleport_offset=[0, 0, 0])
        await asyncio.sleep(1)

    async def _set_walking(self, names, walking):
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
        await asyncio.gather(*(self._execute(name, code) for name in names))

    async def _resource(self, name, uri):
        clients = self.connections.clients
        result = await clients[name].read_resource(uri)
        return extract_json_object_from_resource_result(result, uri)

    async def _prompt(self, name):
        result = await call_mcp_tool(self.connections.belief_clients[name], "render_prompt", {"template": PROMPT, "variables": {"self_name": name, "other_beliefs": {}}})
        return extract_json_object_from_tool_result(result, "render_prompt")["text"]

    async def _positions(self, names):
        observations = await asyncio.gather(*(self._resource(name, "minecraft-observation://latest-observation") for name in names))
        return [obs["objective"]["status"][name]["visible"]["position"]["__Vec3__"] for name, obs in zip(names, observations)]

    async def _chat_check(self, names, oracle):
        failures = self.measurements.failures
        marker = f"obs-perf-{uuid4().hex[:12]}"
        await self._execute(names[0], f"bot.chat({json.dumps(marker)});")
        observed_at = await oracle.wait(marker)
        await asyncio.sleep(max(0, 0.1 - (time.perf_counter() - observed_at)))
        enter_at = time.perf_counter()
        prompts = await asyncio.gather(*(self._prompt(name) for name in names))
        ready_at = time.perf_counter()
        missing = [name for name, prompt in zip(names, prompts) if marker not in prompt or names[0] not in prompt]
        result = {"oracle_to_enter_ms": round((enter_at - observed_at) * 1000, 2), "enter_to_prompt_ms": round((ready_at - enter_at) * 1000, 2), "missing": missing}
        if result["oracle_to_enter_ms"] < 100 or missing:
            failures.append(f"chat for {len(names)} agents: {result}")
        return result

    async def _chat_checks(self, names):
        failures = self.measurements.failures
        connection = MinecraftObservationConnection(log_dir=str(LOG_DIR), host=OBSERVATION_HOST, port=7891, env_box=self.env_box)
        oracle = ChatOracle(connection)
        try:
            await asyncio.to_thread(connection.connect)
            oracle.thread.start()
            results = []
            for _ in range(CHAT_TRIALS):
                try:
                    results.append(await self._chat_check(names, oracle))
                except TimeoutError as error:
                    failures.append(str(error))
                    results.append({"oracle_timeout": str(error)})
                    break
                await asyncio.sleep(1)
            return results
        finally:
            await oracle.close()

    async def _block_check(self):
        control, name, failures, env_box = self.connections.control, self.connections.names[0], self.measurements.failures, self.env_box
        await control.teleport(name, [0, -60, -2], 0, 0, teleport_offset=[0, 0, 0])
        await control.exec_mc(["/setblock 0 -60 0 air"])
        await asyncio.sleep(0.2)
        oracle = MinecraftObservationConnection(log_dir=str(LOG_DIR), host=OBSERVATION_HOST, port=7891, env_box=env_box)
        try:
            initial_state = await asyncio.to_thread(oracle.connect)
            processor = MinecraftObservationProcessor(initial_tick=initial_state["globalTick"])
            await control.exec_mc(["/setblock 0 -60 0 crafting_table"])
            deadline = time.perf_counter() + 10
            while time.perf_counter() < deadline:
                message = await asyncio.to_thread(oracle.receive, 0.2)
                observations = processor.process(message) if message is not None else []
                if any(block.get("name") == "crafting_table" and block.get("position", {}).get("__Vec3__") == [0, -60, 0]
                       for obs in observations for block in obs["objective"]["blocksToUpdate"]):
                    observed_at = time.perf_counter()
                    break
            else:
                self.fail("The independent observation connection did not see the test block update")
            await asyncio.sleep(max(0, 0.12 - (time.perf_counter() - observed_at)))
            enter_at = time.perf_counter()
            prompt = await self._prompt(name)
            result = {"oracle_to_enter_ms": round((enter_at - observed_at) * 1000, 2), "enter_to_prompt_ms": round((time.perf_counter() - enter_at) * 1000, 2)}
            block_line = next((line for line in prompt.splitlines() if line.startswith("crafting_table:")), "")
            result["block_in_prompt"] = "(0, -60, 0)" in block_line
            if not result["block_in_prompt"]:
                failures.append(f"visible block: {result}")
            print(json.dumps({"case": "visible_block_prompt", **result}, ensure_ascii=False), flush=True)
        finally:
            await asyncio.to_thread(oracle.close)

    async def _measure(self, label, names, tick_rate, walking, check_chat=False):
        control, records, failures, env_box = self.connections.control, self.measurements.records, self.measurements.failures, self.env_box
        await control.set_speed(tick_rate / 20)
        await control.exec_mc(["/obsTick 1"])
        await self._set_walking(names, walking)
        await asyncio.sleep(1)
        first = await asyncio.gather(*(self._resource(name, STATUS_URI) for name in names))
        start_positions = await self._positions(names) if walking else None
        started_at = time.perf_counter()
        try:
            if walking:
                await asyncio.sleep(3)
                walking_positions = await self._positions(names)
            else:
                walking_positions = None
            await asyncio.sleep(max(0, started_at + DURATION / 2 - time.perf_counter()))
            chat = await self._chat_checks(names) if check_chat else None
            await asyncio.sleep(max(0, started_at + DURATION - time.perf_counter()))
            ended_at = time.perf_counter()
            last = await asyncio.gather(*(self._resource(name, STATUS_URI) for name in names))
        finally:
            await self._set_walking(names, False)
            await control.set_speed(1.0)
        rows = records.between(started_at, ended_at, names)
        self.assertTrue(rows, f"No processing records for {label}")
        durations = [row["processing_seconds"] * 1000 for row in rows]
        elapsed = ended_at - started_at
        tick_rates = [(b["latest_received_tick"] - a["latest_received_tick"]) / elapsed for a, b in zip(first, last)]
        movement = None if not walking else [round(sum((b[i] - a[i]) ** 2 for i in (0, 2)) ** .5, 2) for a, b in zip(start_positions, walking_positions)]
        result = {
            "case": label, "agents": len(names), "env_box": env_box, "visibility_distance": self.visibility_distance, "target_tps": tick_rate, "achieved_tps_min": round(min(tick_rates), 2),
            "elapsed_seconds": round(elapsed, 2), "observations": len(rows), "processing_mean_ms": round(statistics.mean(durations), 3),
            "processing_p95_ms": round(percentile(durations, .95), 3), "processing_max_ms": round(max(durations), 3),
            "per_agent_tick_budget_ms": round(1000 / tick_rate, 2), "p95_budget_ratio": round((1000 / tick_rate) / percentile(durations, .95), 2),
            "aggregate_work_seconds_per_second": round(sum(durations) / (1000 * elapsed), 3),
            "pending_start_max": max(row["pending_messages"] for row in rows[:len(names)]),
            "pending_end_max": max(row["pending_messages"] for row in rows[-len(names):]),
            "pending_peak": max(row["pending_messages"] for row in rows), "chat": chat,
            "movement_after_three_seconds_blocks": movement,
        }
        first_pending = [row["pending_messages"] for row in rows if row["recorded_at"] < started_at + min(5, elapsed / 4)]
        last_pending = [row["pending_messages"] for row in rows if row["recorded_at"] > ended_at - min(5, elapsed / 4)]
        result["pending_first_window_mean"] = round(statistics.mean(first_pending), 2) if first_pending else None
        result["pending_last_window_mean"] = round(statistics.mean(last_pending), 2) if last_pending else None
        if first_pending and last_pending:
            result["pending_mean_growth"] = round(statistics.mean(last_pending) - statistics.mean(first_pending), 2)
        if check_chat:
            before_chat = [row["processing_seconds"] * 1000 for row in rows if row["recorded_at"] < started_at + DURATION / 2]
            result["walking_before_chat_p95_ms"] = round(percentile(before_chat, .95), 3) if before_chat else None
        for field in ("conversion_seconds", "world_seconds", "perspective_seconds", "belief_seconds"):
            values = [row[field] * 1000 for row in rows if field in row]
            result[field.replace("seconds", "p95_ms")] = round(percentile(values, .95), 3) if values else None
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if min(tick_rates) < tick_rate * .98:
            failures.append(f"tick rate for {label}: {result['achieved_tps_min']} < {tick_rate * .98}")
        if result.get("pending_mean_growth", 0) > 5:
            failures.append(f"observation backlog grew in {label}: {result['pending_mean_growth']} messages")
        if movement is not None and any(distance < 0.5 for distance in movement):
            failures.append(f"walkers did not move in {label}: {movement}")
        return result

    async def _case(self, label, count, tick_rate, walking, check_chat=False):
        names, failures = self.connections.names, self.measurements.failures
        self.assertGreaterEqual(DURATION, 10, "EASYLLM_TEST_OBSERVATION_DURATION must be at least 10 seconds")
        self.assertGreaterEqual(CHAT_TRIALS, 1, "EASYLLM_TEST_CHAT_TRIALS must be at least 1")
        try:
            await self._add_agents(count)
            await self._measure(label, names[:count], tick_rate, walking, check_chat=check_chat)
        finally:
            await self._close_agents()
        self.assertFalse(failures, "\n".join(failures))

    async def test_observation_01_observes_one_idle_agent_at_normal_speed(self):
        await self._case("one_still_normal", 1, 20, False)

    async def test_observation_11_observes_one_idle_agent_at_ten_times_speed(self):
        await self._case("one_still_ten_times", 1, 200, False)

    async def test_observation_03_observes_three_idle_agents_at_normal_speed(self):
        await self._case("three_still_normal", 3, 20, False)

    async def test_observation_04_observes_three_walking_agents_at_normal_speed(self):
        await self._case("three_walking_normal", 3, 20, True)

    async def test_observation_05_reflects_chat_from_three_walking_agents_in_prompts(self):
        await self._case("three_walking_chat", 3, 20, True, check_chat=True)

    async def test_observation_06_observes_ten_idle_agents_at_normal_speed(self):
        await self._case("ten_still_normal", 10, 20, False)

    async def test_observation_07_observes_ten_walking_agents_at_normal_speed(self):
        await self._case("ten_walking_normal", 10, 20, True)

    async def test_observation_08_reflects_chat_from_ten_walking_agents_in_prompts(self):
        await self._case("ten_walking_chat", 10, 20, True, check_chat=True)

    async def test_observation_02_reflects_visible_block_updates_in_prompts(self):
        failures = self.measurements.failures
        try:
            await self._add_agents(1)
            await self._block_check()
        finally:
            await self._close_agents()
        self.assertFalse(failures, "\n".join(failures))

    async def test_observation_09_compares_processing_time_by_observation_bounds(self):
        self.assertEqual(len(ENV_BOXES), 2, "EASYLLM_TEST_ENV_BOXES must contain exactly two boxes")
        self.assertNotEqual(ENV_BOXES[0], ENV_BOXES[1], "The comparison needs different EnvBoxes")
        self.assertGreaterEqual(DURATION, 10, "EASYLLM_TEST_OBSERVATION_DURATION must be at least 10 seconds")
        names, failures = self.connections.names[:3], self.measurements.failures
        results = []
        for index, env_box in enumerate(ENV_BOXES):
            self.env_box = env_box
            try:
                await self._add_agents(3)
                results.append(await self._measure(f"env_box_{index + 1}_three_walking_normal", names, 20, True))
            finally:
                await self._close_agents()
        first, second = results
        print(json.dumps({"case": "env_box_comparison", "measurements": results,
                          "processing_p95_ratio": round(second["processing_p95_ms"] / first["processing_p95_ms"], 2),
                          "aggregate_work_ratio": round(second["aggregate_work_seconds_per_second"] / first["aggregate_work_seconds_per_second"], 2)},
                         ensure_ascii=False), flush=True)
        self.assertFalse(failures, "\n".join(failures))

    async def test_observation_10_compares_processing_time_by_view_distance(self):
        self.assertGreaterEqual(DURATION, 10, "EASYLLM_TEST_OBSERVATION_DURATION must be at least 10 seconds")
        self.assertGreaterEqual(len(VISIBILITY_DISTANCES), 2, "EASYLLM_TEST_VISIBILITY_DISTANCES needs at least two values")
        self.assertTrue(all(float(distance) > 0 for distance in VISIBILITY_DISTANCES), "Visibility distances must be positive")
        self.env_box = VISIBILITY_ENV_BOX
        names, failures = self.connections.names[:3], self.measurements.failures
        results = []
        for distance in VISIBILITY_DISTANCES:
            self.visibility_distance = float(distance)
            try:
                await self._add_agents(3)
                results.append(await self._measure(f"visibility_{distance}_three_walking_normal", names, 20, True))
            finally:
                await self._close_agents()
        baseline = results[0]
        print(json.dumps({"case": "visibility_distance_comparison", "measurements": results,
                          "processing_p95_ratios": [round(row["processing_p95_ms"] / baseline["processing_p95_ms"], 2) for row in results],
                          "perspective_p95_ratios": [round(row["perspective_p95_ms"] / baseline["perspective_p95_ms"], 2) for row in results],
                          "diagnostic_failures": failures}, ensure_ascii=False), flush=True)

if __name__ == "__main__":
    unittest.main()
