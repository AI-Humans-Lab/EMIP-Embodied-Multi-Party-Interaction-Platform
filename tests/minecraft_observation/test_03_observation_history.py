"""Historical state correctness and diagnostic timings, including an optional simulated 24-hour history.

The capacity case generates 1,728,000 ticks in a loop without waiting 24 hours or starting Minecraft.
"""

import asyncio
import json
import os
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path

from mcp import Client
from mcp.server import MCPServer

from modules.belief import WorldObservationLoader, load_from_template
from modules.mcp_client_request import call_mcp_tool, extract_json_object_from_tool_result
from modules.belief_store import BeliefStore
from modules.belief import render_belief_snapshot


PROMPT = (Path(__file__).resolve().parents[2] / "src/prompts/coding_llm_human_prompt_template_jp.txt").read_text(encoding="utf-8")
PROMPT_VARIABLES = {"self_name": "Agent1", "other_beliefs": {}}
TRIALS = 5


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def initial_state(names, large_world=False):
    blocks = [{"position": [0, -60, 0], "name": "crafting_table"}]
    if large_world:
        blocks = [
            {"position": [x, y, z], "name": "crafting_table" if (x, y, z) == (0, -60, 0) else "stone" if y == -61 else "air"}
            for x in range(-16, 17) for y in range(-61, -54) for z in range(-16, 17)
        ]
    return {
        "globalTick": 0,
        "status": {name: {"visible": {"position": {"__Vec3__": [index, -60, 0]}, "equipment": [None] * 6}, "hidden": {"inventory": {}}}
                   for index, name in enumerate(names)},
        "blocks": {"__Vec3Map__": blocks},
        "containers": {"__Vec3Map__": []},
        "events": {},
    }


def observation(tick, names, *, chat=False, block_name=None, other_visible=True):
    status = {
        name: {"visible": {"position": {"__Vec3__": [tick % 5 + index, -60, 0]}, "equipment": [None] * 6},
               "hidden": {"inventory": {"stone": tick % 3}}}
        for index, name in enumerate(names)
    }
    events = [{"eventName": "chat", "visible": {"agentName": names[0], "msg": f"history-marker-{tick}"}, "hidden": None}] if chat else []
    blocks = [{"position": {"__Vec3__": [0, -60, 0]}, "name": block_name}] if block_name is not None else []
    return {
        "globalTick": tick,
        "objective": {"status": status, "events": events, "blocksToUpdate": blocks},
        "player_visibility": {name: other_visible for name in names[1:]},
    }


def make_store(names, history_ticks, *, large_world=False, chat_every=None):
    state = initial_state(names, large_world=large_world)
    store = BeliefStore(initial_state=state, agent_names=["Agent1"])
    for tick in range(1, history_ticks + 1):
        chat = tick == history_ticks or (chat_every is not None and tick % chat_every == 0)
        block_name = ("stone" if tick // 100 % 2 else "crafting_table") if tick % 100 == 0 else None
        item = observation(tick, names, chat=chat, block_name=block_name)
        store.register_observation("Agent1", item)
    return store


def render_prompt(store):
    snapshot = store.get_prompt_snapshot("Agent1")
    prompt = load_from_template(WorldObservationLoader(**snapshot), PROMPT, variables=PROMPT_VARIABLES)
    return prompt


class HistoryBasicTests(unittest.TestCase):
    def test_history_01_returns_previous_state_when_no_observation_exists_at_requested_tick(self):
        names = ["Agent1", "Agent2"]
        state = initial_state(names)
        store = BeliefStore(initial_state=state, agent_names=["Agent1"])
        store.register_observation("Agent1", observation(2, names, block_name="stone"))
        store.register_observation("Agent1", observation(4, names, chat=True, block_name="crafting_table", other_visible=False))

        initial = store.get_state_at("Agent1", 0)
        at_two = store.get_state_at("Agent1", 2)
        at_three = store.get_state_at("Agent1", 3)
        at_four = store.get_state_at("Agent1", 4)
        self.assertEqual([initial["tick"], at_two["tick"], at_three["tick"], at_four["tick"]], [0, 2, 2, 4])
        self.assertEqual([initial["requested_tick"], at_three["requested_tick"]], [0, 3])
        self.assertEqual([item["blocks"]["__Vec3Map__"][0]["name"] for item in (initial, at_two, at_three, at_four)],
                         ["crafting_table", "stone", "stone", "crafting_table"])
        self.assertEqual(at_two["agents"]["Agent1"]["private_status"]["inventory"], {"stone": 2})
        self.assertEqual(at_four["agents"]["Agent1"]["private_status"]["inventory"], {"stone": 1})
        self.assertEqual([at_two["agents"]["Agent2"]["visibility"], at_four["agents"]["Agent2"]["visibility"]],
                         ["visible", "not_visible"])
        self.assertNotIn("events", initial)
        self.assertNotIn("events", at_three)
        self.assertNotIn("events", at_four)
        self.assertEqual(store.get_events("Agent1", from_tick=4)["events"][0]["tick"], 4)
        self.assertEqual(set(store.get_state_at("Agent1", 2, include=["agents"])), {"tick", "requested_tick", "agents"})

        at_two["agents"]["Agent1"]["status"]["visible"]["position"]["__Vec3__"][0] = 999
        self.assertNotEqual(store.get_state_at("Agent1", 2)["agents"]["Agent1"]["status"]["visible"]["position"]["__Vec3__"][0], 999)

    def test_history_04_produces_same_prompt_from_compacted_history(self):
        store = make_store(["Agent1", "Agent2"], 4)
        compact = render_prompt(store)
        full = render_belief_snapshot(store.get_belief_snapshot("Agent1"), PROMPT, PROMPT_VARIABLES)
        self.assertEqual(compact, full)
        self.assertIn("history-marker-4", compact)

    def test_history_02_restores_past_chests_and_blocks_without_mutating_stored_observations(self):
        names = ["Agent1"]
        state = initial_state(names)
        store = BeliefStore(initial_state=state, agent_names=["Agent1"])
        first = observation(2, names, block_name="chest")
        first["objective"]["events"] = [{"eventName": "depositItemIntoChest", "agentName": "Agent1",
                                           "visible": {"chestPos": {"__Vec3__": [0, -60, 0]}, "depositedItems": {"stone": 3}}}]
        store.register_observation("Agent1", first)
        store.register_observation("Agent1", observation(4, names, block_name="stone"))
        saved_before = store.get_observation_history("Agent1")

        at_three = store.get_state_at("Agent1", 3)
        at_four = store.get_state_at("Agent1", 4)
        self.assertEqual(at_three["blocks"]["__Vec3Map__"][0]["name"], "chest")
        self.assertEqual(at_three["containers"]["__Vec3Map__"][0]["stone"], 3)
        self.assertEqual(at_four["blocks"]["__Vec3Map__"][0]["name"], "stone")
        self.assertEqual(at_four["containers"], {"__Vec3Map__": []})
        self.assertEqual(store.get_observation_history("Agent1"), saved_before)

        at_three["containers"]["__Vec3Map__"][0]["stone"] = 999
        self.assertEqual(store.get_state_at("Agent1", 3)["containers"]["__Vec3Map__"][0]["stone"], 3)

    def test_history_03_mutating_returned_block_attributes_does_not_change_history(self):
        names = ["Agent1"]
        state = initial_state(names)
        store = BeliefStore(initial_state=state, agent_names=["Agent1"])
        item = observation(2, names, block_name="oak_stairs")
        item["objective"]["blocksToUpdate"][0]["properties"] = {"facing": "north"}
        store.register_observation("Agent1", item)

        result = store.get_state_at("Agent1", 2)
        result["blocks"]["__Vec3Map__"][0]["properties"]["facing"] = "south"
        self.assertEqual(store.get_state_at("Agent1", 2)["blocks"]["__Vec3Map__"][0]["properties"]["facing"], "north")
        saved = store.get_observation_history("Agent1", from_tick=2, to_tick=2)["observations"][0]
        self.assertEqual(saved["block_observations"][0]["properties"]["facing"], "north")


class HistoryPerformanceTests(unittest.TestCase):
    def run_state_case(self, history_ticks, *, large_world=False):
        names = ["Agent1", "Agent2", "Agent3"]
        started = time.perf_counter()
        store = make_store(names, history_ticks, large_world=large_world)
        preparation_ms = (time.perf_counter() - started) * 1000
        for tick in (history_ticks, history_ticks // 2, max(1, history_ticks // 10)):
            started = time.perf_counter()
            result = store.get_state_at("Agent1", tick)
            first_ms = (time.perf_counter() - started) * 1000
            repeats = []
            for _ in range(TRIALS):
                started = time.perf_counter()
                repeated = store.get_state_at("Agent1", tick)
                repeats.append((time.perf_counter() - started) * 1000)
                self.assertEqual(repeated, result)
            started = time.perf_counter()
            payload_bytes = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
            serialization_ms = (time.perf_counter() - started) * 1000
            print(json.dumps({"case": "historical_state", "agents": len(names), "history_ticks": history_ticks,
                              "target_tick": tick, "large_world": large_world, "world_blocks": len(store.get_current_state("Agent1", include=["blocks"])["blocks"]["__Vec3Map__"]),
                              "preparation_ms": round(preparation_ms, 2), "first_ms": round(first_ms, 2),
                              "repeat_p95_ms": round(percentile(repeats, .95), 2), "repeat_max_ms": round(max(repeats), 2),
                              "serialization_ms": round(serialization_ms, 2), "result_bytes": payload_bytes}, ensure_ascii=False), flush=True)
            self.assertEqual(result["tick"], tick)

    def run_prompt_case(self, history_ticks, *, chat_every=None):
        names = ["Agent1", "Agent2", "Agent3"]
        store = make_store(names, history_ticks, chat_every=chat_every)
        snapshot_times, render_times, prompt_bytes = [], [], []
        render_prompt(store)
        for _ in range(TRIALS):
            started = time.perf_counter()
            snapshot = store.get_prompt_snapshot("Agent1")
            snapshot_done = time.perf_counter()
            prompt = load_from_template(WorldObservationLoader(**snapshot), PROMPT, variables=PROMPT_VARIABLES)
            render_done = time.perf_counter()
            snapshot_times.append((snapshot_done - started) * 1000)
            render_times.append((render_done - snapshot_done) * 1000)
            prompt_bytes.append(len(prompt.encode("utf-8")))
            if history_ticks:
                self.assertIn(f"history-marker-{history_ticks}", prompt)
        totals = [a + b for a, b in zip(snapshot_times, render_times)]
        print(json.dumps({"case": "long_history_prompt", "agents": len(names), "history_ticks": history_ticks,
                          "chat_every": chat_every, "world_blocks": len(store.get_current_state("Agent1", include=["blocks"])["blocks"]["__Vec3Map__"]),
                          "snapshot_p95_ms": round(percentile(snapshot_times, .95), 2), "render_p95_ms": round(percentile(render_times, .95), 2),
                          "total_p95_ms": round(percentile(totals, .95), 2), "total_max_ms": round(max(totals), 2),
                          "prompt_bytes": max(prompt_bytes), "trials": TRIALS}, ensure_ascii=False), flush=True)

    def test_history_07_measures_past_state_lookup_with_600_ticks(self):
        self.run_state_case(600)

    def test_history_08_measures_past_state_lookup_with_6000_ticks(self):
        self.run_state_case(6000)

    def test_history_10_measures_past_state_lookup_with_36000_ticks(self):
        self.run_state_case(36000)

    def test_history_09_measures_past_state_lookup_with_6000_ticks_and_normal_bounds(self):
        self.run_state_case(6000, large_world=True)

    def test_history_13_measures_prompt_generation_time_by_history_length(self):
        for history_ticks in (0, 600, 6000, 36000):
            with self.subTest(history_ticks=history_ticks):
                self.run_prompt_case(history_ticks)

    def test_history_14_measures_prompt_generation_time_by_event_frequency(self):
        self.run_prompt_case(6000)
        self.run_prompt_case(6000, chat_every=20)

    def test_history_12_measures_past_state_lookup_during_observation_registration(self):
        names = ["Agent1", "Agent2", "Agent3"]
        store = make_store(names, 6000)
        barrier = threading.Barrier(2)
        query_intervals, errors = [], []

        def query_history():
            try:
                barrier.wait(timeout=10)
                for _ in range(3):
                    started = time.perf_counter()
                    result = store.get_state_at("Agent1", 5400)
                    query_intervals.append((started, time.perf_counter()))
                    if result["tick"] != 5400:
                        raise AssertionError(result["tick"])
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=query_history, daemon=True)
        thread.start()
        barrier.wait(timeout=10)
        register_intervals = []
        for tick in range(6001, 6051):
            started = time.perf_counter()
            store.register_observation("Agent1", observation(tick, names))
            register_intervals.append((started, time.perf_counter()))
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive(), "historical query did not finish")
        self.assertFalse(errors, errors)
        self.assertEqual(store.get_observation_status("Agent1")["current_state_tick"], 6050)
        overlaps = sum(any(query_start < register_end and register_start < query_end for query_start, query_end in query_intervals)
                       for register_start, register_end in register_intervals)
        register_ms = [(end - start) * 1000 for start, end in register_intervals]
        query_ms = [(end - start) * 1000 for start, end in query_intervals]
        print(json.dumps({"case": "historical_query_during_registration", "agents": len(names), "history_ticks_start": 6000,
                          "registrations": len(register_intervals), "overlapping_registrations": overlaps,
                          "registration_p95_ms": round(percentile(register_ms, .95), 2), "registration_max_ms": round(max(register_ms), 2),
                          "query_max_ms": round(max(query_ms), 2)}, ensure_ascii=False), flush=True)

    def test_history_11_measures_lookup_time_with_36000_ticks_and_normal_bounds(self):
        names = ["Agent1", "Agent2", "Agent3"]
        store = make_store(names, 36000, large_world=True)
        durations = []
        for _ in range(3):
            started = time.perf_counter()
            result = store.get_state_at("Agent1", 36000)
            durations.append((time.perf_counter() - started) * 1000)
            self.assertEqual(result["tick"], 36000)
            self.assertEqual(len(result["blocks"]["__Vec3Map__"]), 7623)
            self.assertNotIn("events", result)
        print(json.dumps({"case": "historical_state_target", "agents": len(names), "history_ticks": 36000,
                          "world_blocks": 7623, "target_ms": 2000, "first_ms": round(durations[0], 2),
                          "repeat_max_ms": round(max(durations[1:]), 2), "within_target": max(durations) <= 2000}, ensure_ascii=False), flush=True)


CAPACITY_AGENTS = ["Agent1", "Agent2", "Agent3"]
CAPACITY_TARGET_TICKS = 24 * 60 * 60 * 20
CAPACITY_TARGET_MS = 2000


def capacity_initial_state(radius):
    blocks = [
        {"position": [x, y, z], "name": "stone" if y == -61 else "air"}
        for x in range(-radius, radius + 1) for y in range(-61, -54) for z in range(-radius, radius + 1)
    ]
    blocks[0]["name"] = "diamond_block"
    return {
        "globalTick": 0,
        "status": {name: {"visible": {"position": {"__Vec3__": [index, -60, 0]}, "equipment": [None] * 6}, "hidden": {"inventory": {}}}
                   for index, name in enumerate(CAPACITY_AGENTS)},
        "blocks": {"__Vec3Map__": blocks}, "containers": {"__Vec3Map__": []}, "events": {},
    }


def capacity_observation(tick):
    return {
        "globalTick": tick,
        "objective": {
            "status": {name: {"visible": {"position": {"__Vec3__": [(tick + index) % 5, -60, 0]}, "equipment": [None] * 6},
                             "hidden": {"inventory": {}}} for index, name in enumerate(CAPACITY_AGENTS)},
            "events": [{"eventName": "chat", "visible": {"agentName": CAPACITY_AGENTS[1], "msg": f"marker-{tick}"}, "hidden": None}]
                      if tick % 1200 == 0 else [],
            "blocksToUpdate": [{"position": {"__Vec3__": [0, -60, 0]}, "name": "stone" if tick % 2400 else "air"}]
                              if tick % 1200 == 0 else [],
        },
        "player_visibility": {name: True for name in CAPACITY_AGENTS[1:]},
    }


def make_capacity_store(ticks, radius):
    state = capacity_initial_state(radius)
    beliefs = {name: BeliefStore(initial_state=state, agent_names=[name]) for name in CAPACITY_AGENTS}
    for tick in range(1, ticks + 1):
        item = capacity_observation(tick)
        for agent_name, belief in beliefs.items():
            item["player_visibility"] = {name: True for name in CAPACITY_AGENTS if name != agent_name}
            belief.register_observation(agent_name, item)
    return beliefs


def make_capacity_server(store, timings):
    server = MCPServer("Historical state capacity")

    @server.tool()
    def get_state_at(tick: int, center: list[int] | None = None, radius: int | None = None,
                     include: list[str] | None = None) -> dict:
        started = time.perf_counter()
        try:
            return store[CAPACITY_AGENTS[0]].get_state_at(CAPACITY_AGENTS[0], tick, center=center, radius=radius, include=include)
        finally:
            timings.append((time.perf_counter() - started) * 1000)

    @server.tool()
    def get_state_at_current(tick: int, include: list[str] | None = None) -> dict:
        started = time.perf_counter()
        try:
            return store[CAPACITY_AGENTS[0]].get_state_at(CAPACITY_AGENTS[0], tick, include=include)
        finally:
            timings.append((time.perf_counter() - started) * 1000)

    return server


async def query_capacity_state(client, tool, arguments, timings):
    started = time.perf_counter()
    response = await call_mcp_tool(client, tool, arguments)
    received = time.perf_counter()
    result = extract_json_object_from_tool_result(response, tool)
    finished = time.perf_counter()
    return result, {"server_ms": round(timings[-1], 2), "mcp_ms": round((received - started) * 1000, 2),
                    "decode_ms": round((finished - received) * 1000, 2), "total_ms": round((finished - started) * 1000, 2)}


class HistoryMcpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    async def test_history_05_retrieves_short_history_via_mcp(self):
        store = make_capacity_store(12, 1)
        timings = []
        async with Client(make_capacity_server(store, timings)) as client:
            result, duration = await query_capacity_state(client, "get_state_at_current", {"tick": 12, "include": ["agents"]}, timings)
        self.assertEqual(result["tick"], 12)
        self.assertEqual(set(result), {"tick", "requested_tick", "agents"})
        self.assertEqual(set(result["agents"]), set(CAPACITY_AGENTS))
        self.assertGreaterEqual(duration["total_ms"], duration["server_ms"])
        print(json.dumps({"case": "history_mcp_smoke", "agents_in_world": 3, "histories_populated": 3,
                          "history_ticks": 12, "world_blocks": 63, **duration}), flush=True)

    async def test_history_06_restores_block_updates_after_checkpoint(self):
        store = make_capacity_store(1201, 1)
        before = store[CAPACITY_AGENTS[0]].get_state_at(CAPACITY_AGENTS[0], 1199, center=[0, -60, 0], radius=0, include=["agents", "blocks"])
        after = store[CAPACITY_AGENTS[0]].get_state_at(CAPACITY_AGENTS[0], 1200, center=[0, -60, 0], radius=0, include=["agents", "blocks"])
        self.assertEqual((before["tick"], after["tick"]), (1199, 1200))
        self.assertEqual(before["blocks"]["__Vec3Map__"][0]["name"], "air")
        self.assertEqual(after["blocks"]["__Vec3Map__"][0]["name"], "stone")
        self.assertEqual(after["agents"][CAPACITY_AGENTS[0]]["status"]["visible"]["position"]["__Vec3__"], [0, -60, 0])


class HistoryScaleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    async def run_history_15_generates_24_hours_of_simulated_history_and_queries_within_two_seconds(self):
        # Check the region contract before allocating a 24-hour history.
        probe = make_capacity_store(2, 1)
        probe_timings = []
        async with Client(make_capacity_server(probe, probe_timings)) as client:
            local, _ = await query_capacity_state(client, "get_state_at", {"tick": 1, "radius": 0, "include": ["blocks"]}, probe_timings)
            distant, _ = await query_capacity_state(client, "get_state_at", {"tick": 2, "center": [-1, -61, -1], "radius": 0,
                                                              "include": ["blocks"]}, probe_timings)
        self.assertEqual(len(local["blocks"]["__Vec3Map__"]), 1)
        self.assertEqual(distant["blocks"]["__Vec3Map__"][0]["name"], "diamond_block")

        ticks = int(os.getenv("EASYLLM_SIMULATED_DAY_TICKS", str(CAPACITY_TARGET_TICKS)))
        self.assertGreater(ticks, 0)
        started = time.perf_counter()
        store = make_capacity_store(ticks, 32)
        preparation_s = time.perf_counter() - started
        timings = []
        async with Client(make_capacity_server(store, timings)) as client:
            results = []
            for target_tick, arguments in ((ticks, {"tick": ticks, "include": ["agents", "blocks", "containers"]}),
                                           (ticks // 2, {"tick": ticks // 2, "center": [-32, -61, -32], "radius": 0,
                                                         "include": ["blocks", "containers"]})):
                result, duration = await query_capacity_state(client, "get_state_at", arguments, timings)
                self.assertEqual(result["tick"], target_tick)
                self.assertLessEqual(len(result["blocks"]["__Vec3Map__"]), 65 * 7 * 65 if "center" not in arguments else 1)
                if "center" in arguments:
                    self.assertEqual(result["blocks"]["__Vec3Map__"][0]["name"], "diamond_block")
                results.append({"target_tick": target_tick, "result_bytes": len(json.dumps(result).encode("utf-8")), **duration})
        print(json.dumps({"case": "simulated_day_history_query", "agents_in_world": 3, "histories_populated": 3,
                          "history_ticks": ticks, "full_simulated_day": ticks == CAPACITY_TARGET_TICKS, "world_blocks": 65 * 7 * 65,
                          "preparation_s": round(preparation_s, 2),
                          "target_ms": CAPACITY_TARGET_MS, "queries": results,
                          "within_target": all(item["total_ms"] <= CAPACITY_TARGET_MS for item in results)}), flush=True)
        self.assertTrue(all(item["total_ms"] <= CAPACITY_TARGET_MS for item in results))


if __name__ == "__main__":
    unittest.main()
