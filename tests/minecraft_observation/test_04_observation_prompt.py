import asyncio
import json
import logging
import multiprocessing
import queue
import threading
import traceback
import os
import time
import unittest
from contextlib import AsyncExitStack
from pathlib import Path
from tempfile import gettempdir
from types import SimpleNamespace

from mcp import Client
from mcp.server import MCPServer

from modules.agent import Agent
from modules.config_dataclass import MinecraftWorldConfig
from modules.observation_action_mcp import ObservationActionConnections, _build_observation_action_mcp_server
from modules.observation_source import MinecraftObservationConnection, _ObservationConnectionState
from modules.belief import WorldObservationLoader, load_from_template
from modules.mcp_client_request import call_mcp_tool, extract_json_object_from_resource_result, extract_json_object_from_tool_result
from modules.belief_store import BeliefStore
from modules.belief import render_belief_snapshot


ENFORCE_TARGET = os.getenv("EASYLLM_ENFORCE_PROMPT_TARGET") == "1"
PROMPT = (Path(__file__).resolve().parents[2] / "src/prompts/coding_llm_human_prompt_template_jp.txt").read_text(encoding="utf-8")
TARGET_MS = 100.0
TRIALS = 5


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def initial_state(names, small=False):
    status = {
        name: {"visible": {"position": {"__Vec3__": [index, -60, 0]}, "equipment": [None] * 6}, "hidden": {"inventory": {}}}
        for index, name in enumerate(names)
    }
    blocks = [{"position": [0, -60, 0], "name": "crafting_table"}] if small else [
        {"position": [x, y, z], "name": "crafting_table" if (x, y, z) == (0, -60, 0) else "stone" if y == -61 else "air"}
        for x in range(-16, 17) for y in range(-61, -54) for z in range(-16, 17)
    ]
    return {"globalTick": 0, "status": status, "blocks": {"__Vec3Map__": blocks}, "containers": {"__Vec3Map__": []}, "events": {}}


def make_store(name, state, history_ticks):
    store = BeliefStore(initial_state=state, agent_names=[name])
    for tick in range(1, history_ticks + 1):
        events = [{"eventName": "chat", "visible": {"agentName": name, "msg": "prompt-performance-marker"}, "hidden": None}] if tick == history_ticks else []
        store.register_observation(name, {"globalTick": tick, "objective": {"status": state["status"], "events": events, "blocksToUpdate": []},
                                    "player_visibility": {other: True for other in state["status"] if other != name}})
    return store


def make_server(store, name, server_timings):
    server = MCPServer("Prompt performance")

    @server.tool()
    def render_prompt(template: str, variables: dict):
        started = time.perf_counter()
        snapshot = store.get_prompt_snapshot(name)
        snapshot_done = time.perf_counter()
        prompt = load_from_template(WorldObservationLoader(**snapshot), template, variables=variables, extra_filters=[], allow_filter_override=True)
        server_timings.append({"snapshot_ms": (snapshot_done - started) * 1000, "render_ms": (time.perf_counter() - snapshot_done) * 1000})
        return {"text": prompt}

    @server.resource("belief://snapshot", mime_type="application/json")
    def belief_snapshot():
        return store.get_belief_snapshot(name)
    @server.tool()
    def ping():
        return {"ok": True}

    return server


async def render_prompt(client, name):
    started = time.perf_counter()
    result = await call_mcp_tool(client, "render_prompt", {"template": PROMPT, "variables": {"self_name": name, "other_beliefs": {}}})
    transport_done = time.perf_counter()
    prompt = extract_json_object_from_tool_result(result, "render_prompt")["text"]
    read_done = time.perf_counter()
    return prompt, {
        "read_ms": (read_done - started) * 1000,
        "transport_ms": (transport_done - started) * 1000,
        "decode_ms": (read_done - transport_done) * 1000,
        "total_ms": (read_done - started) * 1000,
        "prompt_bytes": len(prompt.encode("utf-8")),
    }


class ObservationPromptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    async def run_case(self, agent_count, history_ticks, *, small=False, full_case=True):
        names = [f"Prompt{i:02d}" for i in range(1, agent_count + 1)]
        state = initial_state(names, small=small)
        async with AsyncExitStack() as stack:
            clients, stores, server_timings = {}, {}, {}
            for name in names:
                stores[name] = make_store(name, state, history_ticks)
                server_timings[name] = []
                clients[name] = await stack.enter_async_context(Client(make_server(stores[name], name, server_timings[name])))
            warmup = await asyncio.gather(*(render_prompt(clients[name], name) for name in names))
            for name, (prompt, _) in zip(names, warmup):
                full_snapshot = stores[name].get_belief_snapshot(name)
                full_prompt = render_belief_snapshot(full_snapshot, PROMPT, {"self_name": name, "other_beliefs": {}})
                self.assertEqual(prompt, full_prompt)
            ping_batches = []
            for _ in range(TRIALS if full_case else 1):
                started = time.perf_counter()
                await asyncio.gather(*(call_mcp_tool(clients[name], "ping", {}) for name in names))
                ping_batches.append((time.perf_counter() - started) * 1000)
            for values in server_timings.values():
                values.clear()
            measurements = []
            for _ in range(TRIALS if full_case else 1):
                results = await asyncio.gather(*(render_prompt(clients[name], name) for name in names))
                for name, (prompt, timing) in zip(names, results):
                    self.assertIn("crafting_table:", prompt)
                    if history_ticks:
                        self.assertIn("prompt-performance-marker", prompt)
                    measurements.append({"agent": name, **timing})
        totals = [row["total_ms"] for row in measurements]
        report = {
            "case": "prompt_only", "agents": agent_count, "history_ticks": history_ticks, "trials": TRIALS if full_case else 1,
            "target_ms": TARGET_MS, "total_p95_ms": round(percentile(totals, .95), 2), "total_max_ms": round(max(totals), 2),
            "over_target_count": sum(value > TARGET_MS for value in totals), "within_target": max(totals) <= TARGET_MS,
            "read_p95_ms": round(percentile([row["read_ms"] for row in measurements], .95), 2),
            "transport_p95_ms": round(percentile([row["transport_ms"] for row in measurements], .95), 2),
            "decode_p95_ms": round(percentile([row["decode_ms"] for row in measurements], .95), 2),
            "prompt_bytes_max": max(row["prompt_bytes"] for row in measurements),
            "ping_batch_max_ms": round(max(ping_batches), 2),
            "server_snapshot_p95_ms": round(percentile([row["snapshot_ms"] for values in server_timings.values() for row in values], .95), 2),
            "server_render_p95_ms": round(percentile([row["render_ms"] for values in server_timings.values() for row in values], .95), 2),
        }
        print(json.dumps(report, ensure_ascii=False), flush=True)
        if full_case and ENFORCE_TARGET:
            self.assertLessEqual(max(totals), TARGET_MS, report)

    async def test_prompt_04_agent_generates_prompt_from_its_own_belief(self):
        class FakeLLM:
            def format_prompts_for_llm(self, prompts):
                return prompts

            async def generate(self, prompts, **kwargs):
                self.rendered_prompt = prompts[1][1]
                return "[]", "0"

        name = "Prompt01"
        store = make_store(name, initial_state([name], small=True), 1)
        model = FakeLLM()
        agent = Agent.__new__(Agent)
        agent.config = SimpleNamespace(name=name, prompts={"system": {"primitive": "system"}, "human": {"generate_action": PROMPT}, "other_belief": "{{ events() }}"})
        agent.modules = SimpleNamespace(belief_store=store, llm=model, logger=logging.getLogger(__name__))
        agent.state = SimpleNamespace(state_lock=threading.Lock(), closed=False)
        actions = await agent.generate_action_js()
        self.assertEqual(actions, ["[]"])
        self.assertEqual(store.get_belief_snapshot(name)["state_dict"]["globalTick"], 1)
        full_snapshot = store.get_belief_snapshot(name)
        expected = render_belief_snapshot(full_snapshot, PROMPT + "\n{{ conversation_context }}", {"self_name": name, "other_beliefs": {}, "conversation_context": ""})
        self.assertEqual(model.rendered_prompt, expected)

    async def test_prompt_01_template_selects_displayed_blocks(self):
        name = "Prompt01"
        state = initial_state([name], small=True)
        state["blocks"]["__Vec3Map__"].append({"position": [1, -60, 0], "name": "diamond_ore", "properties": {"custom": "visible"}})
        store = make_store(name, state, 0)
        template = (
            '{{ blocks_and_visibilities(["diamond_ore", "unknown_block"]) }}\n'
            '{{ blocks(["crafting_table"]) }}\n{{ block_property("diamond_ore") }}\n{{ latest_state_json() }}'
        )
        expected = render_belief_snapshot(store.get_belief_snapshot(name), template, {"self_name": name, "other_beliefs": {}})
        async with Client(make_server(store, name, [])) as client:
            result = await call_mcp_tool(client, "render_prompt", {"template": template, "variables": {"self_name": name, "other_beliefs": {}}})
            prompt = extract_json_object_from_tool_result(result, "render_prompt")["text"]
        self.assertEqual(prompt, expected)
        self.assertIn("diamond_ore: [", prompt)
        self.assertIn("unknown_block: Not observed", prompt)
        self.assertIn('"custom": "visible"', prompt)
        snapshot = store.get_prompt_snapshot(name)
        store.register_observation(name, {
            "globalTick": 1, "objective": {"status": state["status"], "events": [],
                                           "blocksToUpdate": [{"position": {"__Vec3__": [1, -60, 0]}, "name": "stone"}]},
            "player_visibility": {name: True},
        })
        prior_prompt = load_from_template(WorldObservationLoader(**snapshot), template, variables={"self_name": name, "other_beliefs": {}})
        self.assertEqual(prior_prompt, expected)

    async def test_prompt_02_generates_prompt_from_short_history(self):
        await self.run_case(1, 1, small=True, full_case=False)

    async def test_prompt_03_retrieves_latest_observations_after_processing_completes(self):
        env_box = [[-1, -61, -1], [1, -59, 1]]
        initial_state = {
            "globalTick": 10,
            "status": {"Sally": {"visible": {"position": {"__Vec3__": [0, -60, 0]}, "equipment": [None] * 6}, "hidden": {"inventory": {}}}},
            "blocks": {"__Vec3Map__": [{"position": [0, -60, 0], "name": "air"}]},
            "containers": {"__Vec3Map__": []}, "events": {},
        }
        player = {"name": "Sally", "visible": {"position": [0, -60, 0], "velocity": [0, 0, 0], "pitch": 0, "yaw": 0}, "hidden": {"inventory": {}}}
        tick = lambda number: {"type": "players_tick", "tick": number, "data": {"players": {"Sally": player}}}
        connection = MinecraftObservationConnection(log_dir=gettempdir(), host="test", port=7891, env_box=env_box)
        connection.state = _ObservationConnectionState(websocket=object())
        original_receive = connection.receive
        def slow_receive(timeout=0.1):
            time.sleep(0.005)
            return original_receive(timeout)
        connection.receive = slow_receive
        connection.connect = lambda **kwargs: initial_state
        connection.close = lambda: None
        server = _build_observation_action_mcp_server(
            connections=ObservationActionConnections(FakeMineflayer(), connection),
            minecraft_world_config=MinecraftWorldConfig(env_box), primitives=[], observation_enabled=True, should_enqueue_observations=True,
        )

        async with Client(server) as client:
            await call_mcp_tool(client, "setup_observation_action", {"agent_name": "Sally"})
            chat_before_tick = {"type": "chat", "data": {"player": {"name": "Sally"}, "message": "chat-before-last-tick"}}
            chat_after_tick = {"type": "chat", "data": {"player": {"name": "Sally"}, "message": "chat-after-last-tick"}}
            connection.incoming_queue.put(json.dumps({"type": "event_batch", "items": [
                *[tick(number) for number in range(11, 30)], chat_before_tick, tick(30), chat_after_tick,
            ]}))
            self.assertGreater(connection.pending_message_count, 0)
            await asyncio.wait_for(call_mcp_tool(client, "stop_observation_processing", {}), 5)
            uri = "minecraft-observation://latest-observation"
            latest = extract_json_object_from_resource_result(await client.read_resource(uri), uri)
            self.assertEqual(latest["globalTick"], 30)
            self.assertIn("chat-before-last-tick", json.dumps(latest))
            self.assertNotIn("chat-after-last-tick", json.dumps(latest))
            result = await call_mcp_tool(client, "render_latest_observation_prompt", {})
            prompt = extract_json_object_from_tool_result(result, "render_latest_observation_prompt")["prompt"]
            self.assertIn("\"globalTick\": 30", prompt)

            await call_mcp_tool(client, "start_observation_processing", {})
            result = await call_mcp_tool(client, "take_queued_observations", {"timeout_sec": 0.1})
            queued = extract_json_object_from_tool_result(result, "take_queued_observations")["observations"]
            self.assertEqual([observation["globalTick"] for observation in queued], list(range(11, 31)))
            connection.incoming_queue.put(json.dumps(tick(31)))
            await asyncio.wait_for(call_mcp_tool(client, "stop_observation_processing", {}), 5)
            latest = extract_json_object_from_resource_result(await client.read_resource(uri), uri)
            self.assertEqual(latest["globalTick"], 31)
            self.assertIn("chat-after-last-tick", json.dumps(latest))
            result = await call_mcp_tool(client, "render_latest_observation_prompt", {})
            prompt = extract_json_object_from_tool_result(result, "render_latest_observation_prompt")["prompt"]
            self.assertIn("\"globalTick\": 31", prompt)
            result = await call_mcp_tool(client, "take_queued_observations", {"timeout_sec": 0.1})
            queued = extract_json_object_from_tool_result(result, "take_queued_observations")["observations"]
            self.assertEqual([observation["globalTick"] for observation in queued], [31])
            result = await call_mcp_tool(client, "take_queued_observations", {"timeout_sec": 0.01})
            empty = extract_json_object_from_tool_result(result, "take_queued_observations")["observations"]
            self.assertEqual(empty, [])

            await call_mcp_tool(client, "start_observation_processing", {})
            await asyncio.wait_for(call_mcp_tool(client, "stop_observation_processing", {"discard_unprocessed_updates": True}), 5)

    async def test_prompt_05_measures_generation_time_for_one_agent_without_history(self):
        await self.run_case(1, 0)

    async def test_prompt_06_measures_generation_time_for_one_agent_with_600_ticks(self):
        await self.run_case(1, 600)

    async def test_prompt_07_measures_concurrent_generation_time_for_three_agents_with_600_ticks(self):
        await self.run_case(3, 600)

    async def test_prompt_08_measures_concurrent_generation_time_for_ten_agents_with_600_ticks(self):
        await self.run_case(10, 600)


async def run_process_worker(name, names, ready_event, barrier, reports):
    asyncio.get_running_loop().set_debug(False)
    state = initial_state(names)
    store = make_store(name, state, 600)
    server_timings = []
    async with Client(make_server(store, name, server_timings)) as client:
        prompt, _ = await render_prompt(client, name)
        full_snapshot = store.get_belief_snapshot(name)
        full_prompt = render_belief_snapshot(full_snapshot, PROMPT, {"self_name": name, "other_beliefs": {}})
        if prompt != full_prompt:
            raise AssertionError(f"Prompt differs from full snapshot for {name}")
        reports.put({"ready": name})
        await asyncio.to_thread(ready_event.wait)
        durations = []
        for _ in range(TRIALS):
            await asyncio.to_thread(barrier.wait, 60)
            prompt, timing = await render_prompt(client, name)
            if "prompt-performance-marker" not in prompt:
                raise AssertionError(f"Chat is missing for {name}")
            durations.append(timing["total_ms"])
        reports.put({"agent": name, "durations_ms": durations})


def process_worker(name, names, ready_event, barrier, reports):
    try:
        asyncio.run(run_process_worker(name, names, ready_event, barrier, reports))
    except BaseException:
        reports.put({"agent": name, "error": traceback.format_exc()})


class PromptThreadPerformanceTests(unittest.TestCase):
    def run_case(self, agent_count):
        names = [f"Thread{i:02d}" for i in range(1, agent_count + 1)]
        ready_event, barrier, reports = threading.Event(), threading.Barrier(agent_count), queue.Queue()
        threads = [threading.Thread(target=process_worker, args=(name, names, ready_event, barrier, reports)) for name in names]
        try:
            for thread in threads:
                thread.start()
            for _ in names:
                message = reports.get(timeout=120)
                self.assertNotIn("error", message, message)
                self.assertIn("ready", message, message)
            ready_event.set()
            results = []
            for _ in names:
                message = reports.get(timeout=120)
                self.assertNotIn("error", message, message)
                self.assertIn("durations_ms", message, message)
                results.extend(message["durations_ms"])
            report = {"case": "prompt_separate_threads", "agents": agent_count, "history_ticks": 600, "trials": TRIALS, "target_ms": TARGET_MS,
                      "total_p95_ms": round(percentile(results, .95), 2), "total_max_ms": round(max(results), 2),
                      "over_target_count": sum(value > TARGET_MS for value in results), "within_target": max(results) <= TARGET_MS}
            print(json.dumps(report, ensure_ascii=False), flush=True)
            if ENFORCE_TARGET:
                self.assertLessEqual(max(results), TARGET_MS, report)
        finally:
            ready_event.set()
            for thread in threads:
                thread.join(timeout=5)

    def test_prompt_09_measures_concurrent_generation_time_for_three_agents_in_separate_threads(self):
        self.run_case(3)

    def test_prompt_10_measures_concurrent_generation_time_for_ten_agents_in_separate_threads(self):
        self.run_case(10)

class PromptWorkerProcessPerformanceTests(unittest.TestCase):
    def run_case(self, agent_count):
        context = multiprocessing.get_context("spawn")
        names = [f"Process{i:02d}" for i in range(1, agent_count + 1)]
        ready_event, barrier, reports = context.Event(), context.Barrier(agent_count), context.Queue()
        processes = [context.Process(target=process_worker, args=(name, names, ready_event, barrier, reports)) for name in names]
        try:
            for process in processes:
                process.start()
            for _ in names:
                message = reports.get(timeout=120)
                self.assertNotIn("error", message, message)
                self.assertIn("ready", message, message)
            ready_event.set()
            results = []
            for _ in names:
                message = reports.get(timeout=120)
                self.assertNotIn("error", message, message)
                self.assertIn("durations_ms", message, message)
                results.extend(message["durations_ms"])
            report = {"case": "prompt_separate_processes", "agents": agent_count, "history_ticks": 600, "trials": TRIALS, "target_ms": TARGET_MS,
                      "total_p95_ms": round(percentile(results, .95), 2), "total_max_ms": round(max(results), 2),
                      "over_target_count": sum(value > TARGET_MS for value in results), "within_target": max(results) <= TARGET_MS}
            print(json.dumps(report, ensure_ascii=False), flush=True)
            if ENFORCE_TARGET:
                self.assertLessEqual(max(results), TARGET_MS, report)
        finally:
            ready_event.set()
            for process in processes:
                if process.pid is not None:
                    process.join(timeout=2)
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=5)
            reports.close()

    def test_prompt_11_measures_concurrent_generation_time_for_three_agents_in_separate_processes(self):
        self.run_case(3)

    def test_prompt_12_measures_concurrent_generation_time_for_ten_agents_in_separate_processes(self):
        self.run_case(10)

class FakeMineflayer:
    async def connect(self):
        pass

    async def join(self, name, variables):
        return "Admin"

    async def close(self):
        pass


if __name__ == "__main__":
    unittest.main()
