from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

import numpy as np

from .belief import Vec3Map, WorldState, _summarize_events
from .visibility import BlockSpatialIndex, Vec3BoolMap, build_block_arrays, build_visibility_data, get_player_visibility, get_block_visibility


def _filter_visible_status(status: dict, observer_name: str, player_visibility: dict[str, bool]) -> dict:
    """観測者自身の状態と、見えている他者の公開状態だけを取り出す。

    他者の非公開情報と過去の目撃情報は含めない。
    """
    filtered_status = {}
    for agent_name, agent_status in status.items():
        if agent_name == observer_name:
            filtered_status[agent_name] = deepcopy(agent_status)
        elif player_visibility.get(agent_name):
            visible_status = deepcopy(agent_status)
            visible_status.pop("hidden", None)
            visible_status.pop("last_seen", None)
            filtered_status[agent_name] = visible_status
    return filtered_status


def _prepare_status_for_belief(
    status: dict, observer_name: str, player_visibility: dict[str, bool], remembered_state: WorldState, current_tick: int,
) -> dict:
    """現在見える人物の状態に、見えなくなった人物の最後の目撃情報を組み合わせる。

    過去の位置を現在位置として扱わないための記憶用データを作る。
    """
    filtered_status = _filter_visible_status(status, observer_name, player_visibility)
    for agent_name, agent_status in filtered_status.items():
        if agent_name == observer_name:
            continue
        current_visible = agent_status.get("visible")
        if current_visible is not None:
            agent_status["last_seen"] = {"tick": current_tick, "visible": deepcopy(current_visible)}

    previous_tick = int(remembered_state.global_tick)
    for agent_name, agent_status in remembered_state.status.items():
        if agent_name == observer_name or agent_name in filtered_status:
            continue
        current_visible = agent_status.get("visible")
        last_seen = agent_status.get("last_seen")
        if current_visible is None and last_seen is None:
            continue
        filtered_status[agent_name] = {}
        if last_seen is not None:
            filtered_status[agent_name]["last_seen"] = deepcopy(last_seen)
        else:
            filtered_status[agent_name]["last_seen"] = {
                "tick": previous_tick if previous_tick >= 0 else current_tick,
                "visible": deepcopy(current_visible),
            }
    return filtered_status


def _filter_events(
    events: list[dict],
    see_agent_name: str,
    player_visibility_from_agent: dict[str, bool],
    block_visibility_from_agent: Vec3BoolMap | None,
) -> list[dict]:
    """人物やブロックの可視性に応じて観測できる出来事だけを選ぶ。

    他者の非公開情報は取り除き、元データは変更しない。
    """
    filtered_events = []

    for event in events:
        filtered = event

        if "blockPos" in event and event["blockPos"] is not None and block_visibility_from_agent is not None:
            if not block_visibility_from_agent.has(np.array(event["blockPos"]["__Vec3__"])):
                continue

        if "agentName" in event and event["agentName"] is not None:
            if see_agent_name == event["agentName"]:
                pass
            elif player_visibility_from_agent.get(event["agentName"]):
                filtered = deepcopy(filtered)
                filtered.pop("hidden", None)
            else:
                continue

        filtered_events.append(deepcopy(filtered))

    return filtered_events


def _get_visible_blocks_to_update(
    reported_blocks: Vec3Map,
    source_state: WorldState,
    block_visibility_from_agent: Vec3BoolMap | None,
    previous_block_visibility: Vec3BoolMap | None = None,
    changed_blocks: list[dict] | None = None,
) -> list[dict]:
    """新しく見えた場所と、見えている範囲の変更箇所を調べ、前回までに報告した内容と異なるブロック情報を返す。"""
    if block_visibility_from_agent is None:
        return []

    updated_blocks = []
    world_blocks = source_state.blocks.data
    known_blocks = reported_blocks.data
    origin = block_visibility_from_agent.range[0]
    sx, sy = int(block_visibility_from_agent.size[0]), int(block_visibility_from_agent.size[1])
    indices = block_visibility_from_agent.new_indices(previous_block_visibility)
    if previous_block_visibility is not None and changed_blocks:
        candidates = set(int(index) for index in indices)
        for block in changed_blocks:
            pos = np.asarray(block["position"]["__Vec3__"])
            if block_visibility_from_agent.has(pos):
                offset = pos - origin
                candidates.add(int(offset[0] + sx * (offset[1] + sy * offset[2])))
        indices = sorted(candidates)
    for index in indices:
        index = int(index)
        pos = (int(origin[0]) + index % sx, int(origin[1]) + (index // sx) % sy, int(origin[2]) + index // (sx * sy))
        world_block = world_blocks.get(pos)
        if world_block is None or known_blocks.get(pos) == world_block:
            continue

        block = {
            "position": {"__Vec3__": list(pos)},
            "name": world_block["name"],
        }
        if "stateId" in world_block:
            block["stateId"] = world_block["stateId"]
        if "properties" in world_block:
            block["properties"] = deepcopy(world_block["properties"])
        updated_blocks.append(block)

    return updated_blocks


def build_agent_observation(
    world_observation: dict,
    observer_name: str,
    source_state: WorldState,
    reported_blocks: Vec3Map,
    env_box,
    visibility_distance: float = 20.0,
    block_index: BlockSpatialIndex | None = None,
    previous_block_visibility: Vec3BoolMap | None = None,
) -> tuple[dict, Vec3BoolMap]:
    """ワールド全体の状態をエージェントから見える観測へ変換する。

    人物・出来事・ブロックの更新と可視性をまとめた観測、および今回のブロック可視範囲を返す。
    """
    objective = world_observation["objective"]
    agent_observation = {key: deepcopy(value) for key, value in world_observation.items() if key != "objective"}
    agent_observation["objective"] = {key: deepcopy(value) for key, value in objective.items() if key not in ("status", "events", "blocksToUpdate")}

    player_positions = {}
    for agent_name, agent_status in source_state.status.items():
        pos = agent_status.get("visible", {}).get("position", {}).get("__Vec3__")
        player_positions[agent_name] = deepcopy(pos) if pos is not None else None

    position = player_positions.get(observer_name)
    block_arrays = block_index.near(position, env_box, max_dist=visibility_distance) if block_index is not None and position is not None else build_block_arrays(source_state.blocks, ())
    player_vis, block_vis = _calculate_visibility(source_state.blocks, player_positions, observer_name, env_box,
                                                  visibility_distance, block_arrays=block_arrays)

    agent_observation["objective"]["status"] = _filter_visible_status(objective["status"], observer_name, player_vis)
    agent_observation["objective"]["events"] = _filter_events(
        objective["events"],
        observer_name,
        player_vis,
        block_vis,
    )
    agent_observation["objective"]["blocksToUpdate"] = _get_visible_blocks_to_update(
        reported_blocks,
        source_state,
        block_vis,
        previous_block_visibility,
        objective["blocksToUpdate"],
    )
    agent_observation["player_visibility"] = {
        agent_name: agent_name == observer_name or bool(player_vis.get(agent_name))
        for agent_name in agent_observation["objective"]["status"]
    }
    agent_observation["block_visibility"] = {
        "min": block_vis.range[0].tolist(), "max": block_vis.range[1].tolist(), "bits": block_vis.to_base64(),
    }
    agent_observation["event_summary"] = _summarize_events(agent_observation)

    return agent_observation, block_vis


def prepare_observation_for_belief(observation: dict, observer_name: str, remembered_state: WorldState) -> dict:
    """受信観測を記憶へ登録する形に整える。

    見えない他者の最後の目撃情報を保持し、すでに記憶している内容と同じブロック更新を省く。
    """
    result = {key: deepcopy(value) for key, value in observation.items() if key != "objective"}
    objective = {key: deepcopy(value) for key, value in observation["objective"].items() if key not in ("status", "blocksToUpdate")}
    result["objective"] = objective
    objective["status"] = _prepare_status_for_belief(
        observation["objective"]["status"], observer_name, result["player_visibility"], remembered_state, int(result["globalTick"]),
    )
    updated_blocks = []
    for block in observation["objective"]["blocksToUpdate"]:
        position = block["position"]["__Vec3__"]
        value = {key: block[key] for key in ("name", "stateId", "properties") if key in block and block[key] is not None}
        if not remembered_state.blocks.has(position) or remembered_state.blocks.get(position) != value:
            updated_blocks.append(deepcopy(block))
    objective["blocksToUpdate"] = updated_blocks
    return result

def infer_other_observation(observer_state: WorldState, observer_observation: dict, target_name: str, env_box,
                            visibility_distance: float = 20.0, *, previous_inferred_block_visibility: dict | None = None,
                            block_arrays: tuple | None = None) -> dict:
    """観測者が知っている情報から、指定した他者に見えるはずの観測を推定する。

    他者が見えない場合はチャットだけを扱い、他者の実際の記憶は参照しない。
    """
    tick = int(observer_observation["globalTick"])
    if observer_state.global_tick != tick:
        raise ValueError("Observer state and observation must have the same tick")

    # 1. If the target is not visible, return an observation with no visual information and only chat events.
    events = observer_observation["objective"]["events"]
    if not observer_observation["player_visibility"].get(target_name, False):
        return {"globalTick": tick, "player_visibility": {}, "block_visibility": None,
                "objective": {"status": {}, "events": _infer_other_events(events, target_name, {}, None), "blocksToUpdate": []}}

    # 2. Estimate which people and blocks currently visible to the observer the target can see, using known terrain for occlusion.
    visible_status, player_visibility, block_visibility = _infer_visual_observation(observer_state.blocks, observer_observation, target_name, env_box,
                                                                                  visibility_distance, block_arrays=block_arrays)

    # 3. Select events using the estimated visibility, excluding thoughts and private information.
    inferred_events = _infer_other_events(events, target_name, player_visibility, block_visibility)

    # 4. Collect visible block updates; if previous visibility is available, include only newly visible or changed blocks.
    updates = _collect_inferred_block_updates(observer_state, block_visibility, previous_inferred_block_visibility, observer_observation["objective"]["blocksToUpdate"])

    # 5. Return the estimated information and visibility as an observation that can support subsequent inference.
    return {"globalTick": tick, "player_visibility": {name: bool(player_visibility[name]) for name in visible_status},
            "block_visibility": {"min": block_visibility.range[0].tolist(), "max": block_visibility.range[1].tolist(), "bits": block_visibility.to_base64()},
            "objective": {"status": visible_status, "events": inferred_events, "blocksToUpdate": updates}}


def _infer_visual_observation(known_blocks: Vec3Map, observer_observation: dict, target_name: str, env_box,
                              visibility_distance: float, *, block_arrays: tuple | None = None) -> tuple[dict, dict[str, bool], Vec3BoolMap]:
    """観測者の既知の地形を遮蔽物として使い、対象者に見える人物とブロックを推定する。

    非公開情報や過去の目撃情報は渡さない。
    """
    # Current perception supplies candidates; remembered blocks also supply occluders.
    encoded = observer_observation["block_visibility"]
    if encoded is None:
        raise ValueError("A visible target requires the observer's block visibility")
    observer_visible_blocks = Vec3BoolMap((encoded["min"], encoded["max"]))
    observer_visible_blocks.from_base64(encoded["bits"])
    indices = np.flatnonzero(observer_visible_blocks.data)
    sx, sy = int(observer_visible_blocks.size[0]), int(observer_visible_blocks.size[1])
    candidate_block_positions = np.column_stack((indices % sx, (indices // sx) % sy, indices // (sx * sy))) + observer_visible_blocks.range[0]
    observer_player_visibility = observer_observation["player_visibility"]
    candidate_status = {name: info for name, info in observer_observation["objective"]["status"].items() if observer_player_visibility.get(name, False)}
    player_positions = {name: info["visible"]["position"]["__Vec3__"] for name, info in candidate_status.items()}

    player_visibility, block_visibility = _calculate_visibility(known_blocks, player_positions, target_name, env_box, visibility_distance,
                                                               candidate_block_positions=candidate_block_positions, block_arrays=block_arrays)

    # The observer's private information and last-seen memories are not the target's observations.
    visible_status = _filter_visible_status(candidate_status, target_name, player_visibility)
    for info in visible_status.values():
        info.pop("hidden", None)
        info.pop("last_seen", None)
    return visible_status, player_visibility, block_visibility


def _calculate_visibility(blocks: Vec3Map, player_positions: dict, observer_name: str, env_box, visibility_distance: float = 20.0,
                          *, candidate_block_positions: list | np.ndarray | None = None, block_arrays: tuple | None = None) -> tuple[dict[str, bool], Vec3BoolMap]:
    """指定した人物の位置・視認距離と地形から、見える人物とブロックを計算して返す。"""
    if block_arrays is None:
        block_arrays = build_block_arrays(blocks, ())
    position = player_positions[observer_name]
    visibility_data = build_visibility_data(env_box, position, block_arrays, max_dist=visibility_distance) if position is not None and block_arrays[0].size else None
    player_visibility = get_player_visibility(env_box, player_positions, blocks, observer_name, block_arrays=block_arrays,
                                              visibility_data=visibility_data, max_dist=visibility_distance)
    block_visibility = get_block_visibility(env_box, player_positions, blocks, observer_name, block_arrays=block_arrays,
                                            visibility_data=visibility_data, max_dist=visibility_distance, target_positions=candidate_block_positions)
    return player_visibility, block_visibility


def _infer_other_events(events: list[dict], target_name: str, player_visibility: dict[str, bool], block_visibility: Vec3BoolMap | None) -> list[dict]:
    """推定対象者に伝わる出来事を選ぶ。

    対象者が見えない場合はチャットに限定し、思考や非公開情報は除く。
    """
    if block_visibility is None:
        inferred_events = [deepcopy(event) for event in events if event["eventName"] == "chat"]
    else:
        inferred_events = _filter_events([event for event in events if event["eventName"] != "think"], target_name, player_visibility, block_visibility)
    for event in inferred_events:
        event.pop("hidden", None)
    return inferred_events


def _collect_inferred_block_updates(observer_state: WorldState, block_visibility: Vec3BoolMap,
                                   previous_inferred_block_visibility: dict | None, changed_blocks: list[dict]) -> list[dict]:
    """推定対象者の前回と今回の可視範囲を比較し、新しく見えるブロックと変更されたブロックの情報を集める。"""
    previous_blocks = None
    if previous_inferred_block_visibility is not None:
        previous_blocks = Vec3BoolMap((previous_inferred_block_visibility["min"], previous_inferred_block_visibility["max"]))
        previous_blocks.from_base64(previous_inferred_block_visibility["bits"])
    # An empty reported map leaves target-knowledge comparison to Belief.register_observation.
    return _get_visible_blocks_to_update(Vec3Map([]), observer_state, block_visibility, previous_blocks, changed_blocks)
