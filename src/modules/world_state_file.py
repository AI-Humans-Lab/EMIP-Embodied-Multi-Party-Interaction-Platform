from __future__ import annotations

import json
from copy import deepcopy
from numbers import Integral, Real
from pathlib import Path

import yaml

from modules.belief import WorldState


WORLD_STATE_FORMAT_VERSION = 1
SUPPORTED_KINDS = {"initial_belief", "world_snapshot"}
AGENT_VISIBLE_FIELDS = {"position", "velocity", "yaw", "pitch", "equipment"}


class _FlowSequence(list):
    pass


class _WorldStateDumper(yaml.SafeDumper):
    pass


def _represent_flow_sequence(dumper: yaml.SafeDumper, data: _FlowSequence):
    """座標などの短い配列を、YAML内で角括弧付きの1行表記として出力する。"""
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True)


_WorldStateDumper.add_representer(_FlowSequence, _represent_flow_sequence)


def load_world_state(path: str | Path) -> dict:
    """ワールド状態のYAMLファイルを読み込み、形式を検証してデータを返す。"""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"WorldState file not found: {path}")
    with path.open("r", encoding="utf-8") as stream:
        world_state_data = yaml.safe_load(stream)
    _validate_world_state_data(world_state_data)
    return deepcopy(world_state_data)


def save_world_state(path: str | Path, world_state_data: dict) -> None:
    """ワールド状態の形式を検証し、座標の表記を整えてUTF-8のYAMLへ保存する。

    必要なら保存先フォルダーも作る。
    """
    _validate_world_state_data(world_state_data)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml_data = _build_yaml_data(world_state_data)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        yaml.dump(yaml_data, stream, Dumper=_WorldStateDumper, allow_unicode=True, sort_keys=False)


def save_world_state_object(
    path: str | Path, world_state: WorldState, *, env_box: list[list[int]], snapshot_is_complete: bool, game_version: str | None = None,
) -> None:
    """内部のWorldStateを保存用データに変換し、指定ファイルへYAMLとして保存する。"""
    save_world_state(path, build_world_snapshot_data(
        world_state.to_dict(), env_box=env_box, snapshot_is_complete=snapshot_is_complete, game_version=game_version,
    ))


def build_initial_belief_state(
    world_state_data: dict, *, agent_name: str, current_tick: int, env_box: list[list[int]], reserved_agent_names: set[str],
) -> dict:
    """初期知識ファイルを検証し、範囲内のブロック・コンテナ・人物を内部の記憶形式へ変換する。

    予約された人物名の使用は拒否する。
    """
    _validate_world_state_data(world_state_data)
    if world_state_data["kind"] != "initial_belief":
        raise ValueError("Initial Belief file must have kind: initial_belief")
    subject = world_state_data.get("subject")
    if subject != agent_name:
        raise ValueError(f"Initial Belief subject must be '{agent_name}', got {subject!r}")

    normalized_env_box = _normalize_env_box(env_box)
    world = world_state_data["world"]
    blocks = _build_internal_blocks(world.get("blocks"), normalized_env_box)
    containers = _build_internal_containers(world.get("containers"), normalized_env_box)
    status = _build_internal_agents(world.get("agents"))
    contained_reserved_agent_names = reserved_agent_names.intersection(status)
    if contained_reserved_agent_names:
        raise ValueError(f"Initial Belief contains reserved agents: {sorted(contained_reserved_agent_names)}")
    return {
        "globalTick": int(current_tick),
        "status": status,
        "blocks": {"__Vec3Map__": blocks},
        "containers": {"__Vec3Map__": containers},
    }


def build_world_snapshot_data(
    internal_state: dict,
    *,
    env_box: list[list[int]],
    snapshot_is_complete: bool,
    game_version: str | None = None,
) -> dict:
    """WorldStateを、形式のバージョン・観測範囲・tickを含む保存用スナップショットへ変換する。

    設定に応じて人物やコンテナも含める。
    """
    normalized_env_box = _normalize_env_box(env_box)
    blocks = _build_external_blocks(internal_state["blocks"], normalized_env_box, snapshot_is_complete)
    containers = _build_external_containers(internal_state["containers"])
    agents = _build_external_agents(internal_state["status"])
    environment = {
        "game": "minecraft",
        "env_box": {
            "min": list(normalized_env_box[0]),
            "max": list(normalized_env_box[1]),
        },
        "coordinate_frame": "environment",
    }
    if game_version is not None:
        environment["game_version"] = str(game_version)

    world = {
        "tick": int(internal_state["globalTick"]),
        "blocks": blocks,
    }
    if containers:
        world["containers"] = containers
    if agents:
        world["agents"] = agents
    return {
        "format_version": WORLD_STATE_FORMAT_VERSION,
        "kind": "world_snapshot",
        "environment": environment,
        "world": world,
    }


def _build_yaml_data(world_state_data: dict) -> dict:
    """保存データをコピーし、範囲や位置の座標をYAMLで1行表示する配列へ置き換える。"""
    yaml_data = deepcopy(world_state_data)
    environment = yaml_data.get("environment")
    if isinstance(environment, dict):
        env_box = environment.get("env_box")
        _set_flow_position(env_box, "min")
        _set_flow_position(env_box, "max")

    world = yaml_data["world"]
    blocks = world.get("blocks")
    if isinstance(blocks, dict):
        for region in blocks.get("regions", []):
            _set_flow_position(region, "min")
            _set_flow_position(region, "max")
        for assignment in blocks.get("assignments", []):
            positions = assignment.get("positions")
            if isinstance(positions, list):
                assignment["positions"] = [_FlowSequence(position) for position in positions]

    for container in world.get("containers", []):
        _set_flow_position(container, "position")
    agents = world.get("agents")
    if isinstance(agents, dict):
        for agent in agents.values():
            _set_flow_position(agent, "position")
            _set_flow_position(agent, "velocity")
    return yaml_data


def _set_flow_position(mapping, key: str) -> None:
    """指定項目の座標が配列なら、YAMLで1行表示する専用の配列形式へ置き換える。"""
    if isinstance(mapping, dict) and isinstance(mapping.get(key), (list, tuple)):
        mapping[key] = _FlowSequence(mapping[key])


def _validate_world_state_data(world_state_data: dict) -> None:
    """ワールドファイルの辞書構造、形式のバージョン、種類、主体名など、読み書きに必要な基本項目を検証する。"""
    if not isinstance(world_state_data, dict):
        raise ValueError("WorldState data must be a mapping")
    if world_state_data.get("format_version") != WORLD_STATE_FORMAT_VERSION:
        raise ValueError(f"WorldState format_version must be {WORLD_STATE_FORMAT_VERSION}")
    kind = world_state_data.get("kind")
    if kind not in SUPPORTED_KINDS:
        raise ValueError(f"WorldState kind must be one of {sorted(SUPPORTED_KINDS)}, got {kind!r}")
    if not isinstance(world_state_data.get("world"), dict):
        raise ValueError("WorldState data must contain a world mapping")
    if kind == "initial_belief" and not isinstance(world_state_data.get("subject"), str):
        raise ValueError("Initial Belief data must contain a subject string")


def _normalize_env_box(env_box: list[list[int]]) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """観測範囲の最小・最大座標が整数のXYZで、大小関係も正しいことを確認して形式をそろえる。"""
    if not isinstance(env_box, list) or len(env_box) != 2:
        raise ValueError("env_box must contain min and max positions")
    minimum = _parse_position(env_box[0], "env_box[0]", integer=True)
    maximum = _parse_position(env_box[1], "env_box[1]", integer=True)
    if any(minimum[index] > maximum[index] for index in range(3)):
        raise ValueError("env_box min must not exceed max")
    return minimum, maximum


def _parse_position(value, field: str, *, integer: bool) -> tuple:
    """座標が数値3要素であることを確認して変換する。

    整数必須の指定時は整数だけを許可し、真偽値は数値として扱わない。
    """
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{field} must contain exactly three values")
    expected_type = Integral if integer else Real
    if any(isinstance(part, bool) or not isinstance(part, expected_type) for part in value):
        type_name = "integers" if integer else "numbers"
        raise ValueError(f"{field} must contain three {type_name}")
    converter = int if integer else float
    return tuple(converter(part) for part in value)


def _ensure_in_env_box(position: tuple[int, int, int], env_box, field: str) -> None:
    """指定座標が最小・最大を含む観測範囲内にあることを確認し、範囲外ならエラーにする。"""
    minimum, maximum = env_box
    if any(position[index] < minimum[index] or position[index] > maximum[index] for index in range(3)):
        raise ValueError(f"{field} position {list(position)} is outside env_box")


def _normalize_external_block(value: dict, field: str) -> dict:
    """保存用ブロック情報の名前・状態ID・属性を検証し、内部で使うstateIdなどの形式へ変換する。"""
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a mapping")
    unknown_fields = set(value) - {"name", "state_id", "properties"}
    if unknown_fields:
        raise ValueError(f"{field} contains unknown fields: {sorted(unknown_fields)}")
    name = value.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"{field}.name must be a non-empty string")

    block = {"name": name}
    if "state_id" in value:
        state_id = value["state_id"]
        if isinstance(state_id, bool) or not isinstance(state_id, Integral):
            raise ValueError(f"{field}.state_id must be an integer")
        block["stateId"] = int(state_id)
    if "properties" in value:
        properties = value["properties"]
        if not isinstance(properties, dict):
            raise ValueError(f"{field}.properties must be a mapping")
        block["properties"] = deepcopy(properties)
    return block


def _build_internal_blocks(blocks: dict | None, env_box) -> list[dict]:
    """範囲指定のブロックを各座標へ展開し、個別指定で上書きして内部の一覧を作る。

    範囲の重なりや個別指定の重複、範囲外の座標は拒否する。
    """
    if blocks is None:
        return []
    if not isinstance(blocks, dict):
        raise ValueError("world.blocks must be a mapping")
    unknown_fields = set(blocks) - {"regions", "assignments"}
    if unknown_fields:
        raise ValueError(f"world.blocks contains unknown fields: {sorted(unknown_fields)}")

    block_map: dict[tuple[int, int, int], dict] = {}
    region_positions: set[tuple[int, int, int]] = set()
    regions = blocks.get("regions", [])
    if not isinstance(regions, list):
        raise ValueError("world.blocks.regions must be a list")
    for region_index, region in enumerate(regions):
        field = f"world.blocks.regions[{region_index}]"
        if not isinstance(region, dict) or set(region) != {"min", "max", "block"}:
            raise ValueError(f"{field} must contain exactly min, max, and block")
        minimum = _parse_position(region["min"], f"{field}.min", integer=True)
        maximum = _parse_position(region["max"], f"{field}.max", integer=True)
        if any(minimum[index] > maximum[index] for index in range(3)):
            raise ValueError(f"{field}.min must not exceed max")
        _ensure_in_env_box(minimum, env_box, f"{field}.min")
        _ensure_in_env_box(maximum, env_box, f"{field}.max")
        block = _normalize_external_block(region["block"], f"{field}.block")
        for x in range(minimum[0], maximum[0] + 1):
            for y in range(minimum[1], maximum[1] + 1):
                for z in range(minimum[2], maximum[2] + 1):
                    position = (x, y, z)
                    if position in region_positions:
                        raise ValueError(f"world.blocks.regions overlap at {list(position)}")
                    region_positions.add(position)
                    block_map[position] = deepcopy(block)

    assignment_positions: set[tuple[int, int, int]] = set()
    assignments = blocks.get("assignments", [])
    if not isinstance(assignments, list):
        raise ValueError("world.blocks.assignments must be a list")
    for assignment_index, assignment in enumerate(assignments):
        field = f"world.blocks.assignments[{assignment_index}]"
        if not isinstance(assignment, dict) or set(assignment) != {"block", "positions"}:
            raise ValueError(f"{field} must contain exactly block and positions")
        block = _normalize_external_block(assignment["block"], f"{field}.block")
        positions = assignment["positions"]
        if not isinstance(positions, list) or not positions:
            raise ValueError(f"{field}.positions must be a non-empty list")
        for position_index, raw_position in enumerate(positions):
            position_field = f"{field}.positions[{position_index}]"
            position = _parse_position(raw_position, position_field, integer=True)
            _ensure_in_env_box(position, env_box, position_field)
            if position in assignment_positions:
                raise ValueError(f"world.blocks.assignments repeat position {list(position)}")
            assignment_positions.add(position)
            block_map[position] = deepcopy(block)

    result = []
    for position in sorted(block_map):
        entry = {"position": list(position)}
        entry.update(block_map[position])
        result.append(entry)
    return result


def _normalize_items(items, field: str) -> dict[str, int]:
    """アイテム名が空でなく、個数が正の整数であることを確認して所持品の形式をそろえる。"""
    if not isinstance(items, dict):
        raise ValueError(f"{field} must be a mapping")
    normalized = {}
    for name, count in items.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"{field} item names must be non-empty strings")
        if isinstance(count, bool) or not isinstance(count, Integral) or count < 1:
            raise ValueError(f"{field}.{name} must be a positive integer")
        normalized[name] = int(count)
    return normalized


def _build_internal_containers(containers: list | None, env_box) -> list[dict]:
    """保存用コンテナ一覧の座標・重複・範囲・中身を検証し、内部の座標別データへ変換する。"""
    if containers is None:
        return []
    if not isinstance(containers, list):
        raise ValueError("world.containers must be a list")
    result = []
    seen_positions = set()
    for index, container in enumerate(containers):
        field = f"world.containers[{index}]"
        if not isinstance(container, dict) or set(container) != {"position", "items"}:
            raise ValueError(f"{field} must contain exactly position and items")
        position = _parse_position(container["position"], f"{field}.position", integer=True)
        _ensure_in_env_box(position, env_box, f"{field}.position")
        if position in seen_positions:
            raise ValueError(f"world.containers repeat position {list(position)}")
        seen_positions.add(position)
        entry = {"position": list(position)}
        entry.update(_normalize_items(container["items"], f"{field}.items"))
        result.append(entry)
    return result


def _build_internal_agents(agents: dict | None) -> dict:
    """保存用の人物情報から、公開される位置・向き・装備と、非公開の所持品を検証して内部状態を作る。"""
    if agents is None:
        return {}
    if not isinstance(agents, dict):
        raise ValueError("world.agents must be a mapping")
    result = {}
    for agent_name, agent in agents.items():
        field = f"world.agents.{agent_name}"
        if not isinstance(agent_name, str) or not agent_name:
            raise ValueError("world.agents names must be non-empty strings")
        if not isinstance(agent, dict):
            raise ValueError(f"{field} must be a mapping")
        unknown_fields = set(agent) - AGENT_VISIBLE_FIELDS - {"inventory"}
        if unknown_fields:
            raise ValueError(f"{field} contains unknown fields: {sorted(unknown_fields)}")

        visible = {}
        for name in AGENT_VISIBLE_FIELDS:
            if name not in agent:
                continue
            value = agent[name]
            if name in {"position", "velocity"}:
                visible[name] = {"__Vec3__": list(_parse_position(value, f"{field}.{name}", integer=False))}
            elif name in {"yaw", "pitch"}:
                if isinstance(value, bool) or not isinstance(value, Real):
                    raise ValueError(f"{field}.{name} must be a number")
                visible[name] = float(value)
            else:
                if not isinstance(value, (list, dict)):
                    raise ValueError(f"{field}.equipment must be a list or mapping")
                visible[name] = deepcopy(value)

        status = {}
        if visible:
            status["visible"] = visible
        if "inventory" in agent:
            status["hidden"] = {"inventory": _normalize_items(agent["inventory"], f"{field}.inventory")}
        if not status:
            raise ValueError(f"{field} must contain at least one known field")
        result[agent_name] = status
    return result


def _external_block_from_internal(entry: dict) -> dict:
    """内部のブロック情報から名前と属性をコピーし、保存用の表記へ変換する。

    内部の状態IDは出力しない。
    """
    block = {"name": entry["name"]}
    if "properties" in entry:
        block["properties"] = deepcopy(entry["properties"])
    return block


def _vec3map_entries(value: dict | list, field: str) -> list[dict]:
    """__Vec3Map__形式または座標項目のリストから各項目を取り出す。

    それ以外の形式はエラーにする。
    """
    if isinstance(value, dict):
        value = value.get("__Vec3Map__")
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list or contain __Vec3Map__")
    return value


def _build_external_blocks(blocks: dict | list, env_box, snapshot_is_complete: bool) -> dict:
    """内部のブロックを名前・属性ごとの保存用配置へ変換する。

    完全なスナップショットでは範囲全体を空気で初期化する指定を作り、空気の個別指定を省く。
    """
    grouped_positions: dict[str, tuple[dict, list[list[int]]]] = {}
    for index, entry in enumerate(_vec3map_entries(blocks, "internal_state.blocks")):
        position = list(_parse_position(entry["position"], f"internal_state.blocks[{index}].position", integer=True))
        block = _external_block_from_internal(entry)
        if snapshot_is_complete and block["name"] == "air":
            continue
        key = json.dumps(block, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        grouped_positions.setdefault(key, (block, []))[1].append(position)

    result = {}
    if snapshot_is_complete:
        result["regions"] = [{
            "min": list(env_box[0]),
            "max": list(env_box[1]),
            "block": {"name": "air"},
        }]
    assignments = []
    for key in sorted(grouped_positions):
        block, positions = grouped_positions[key]
        assignments.append({"block": block, "positions": sorted(positions)})
    if assignments:
        result["assignments"] = assignments
    return result


def _build_external_containers(containers: dict | list) -> list[dict]:
    """内部のコンテナ情報から、中身があるものの座標とアイテム一覧を保存用に取り出す。"""
    result = []
    for index, entry in enumerate(_vec3map_entries(containers, "internal_state.containers")):
        position = list(_parse_position(entry["position"], f"internal_state.containers[{index}].position", integer=True))
        items = {name: int(count) for name, count in entry.items() if name != "position"}
        if items:
            result.append({"position": position, "items": items})
    return result


def _build_external_agents(status: dict) -> dict:
    """人物名順に、保存対象の公開状態と所持品を取り出す。

    最後の目撃情報などの対象外項目や、保存項目がない人物は含めない。
    """
    if not isinstance(status, dict):
        raise ValueError("internal_state.status must be a mapping")
    result = {}
    for agent_name in sorted(status):
        internal_agent = status[agent_name]
        if not isinstance(internal_agent, dict):
            continue
        external_agent = {}
        visible = internal_agent.get("visible", {})
        if isinstance(visible, dict):
            for name in AGENT_VISIBLE_FIELDS:
                if name not in visible:
                    continue
                value = visible[name]
                if name in {"position", "velocity"} and isinstance(value, dict):
                    value = value.get("__Vec3__")
                external_agent[name] = deepcopy(value)
        hidden = internal_agent.get("hidden", {})
        if isinstance(hidden, dict) and isinstance(hidden.get("inventory"), dict):
            external_agent["inventory"] = deepcopy(hidden["inventory"])
        if external_agent:
            result[agent_name] = external_agent
    return result
