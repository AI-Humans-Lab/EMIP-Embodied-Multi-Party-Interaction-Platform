from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from numbers import Real
from typing import Any

from modules.belief import WorldState
from modules.mineflayerconnection import AdminMineflayerConnection


EQUIPMENT_SLOTS = ("head", "chest", "legs", "feet", "mainhand", "offhand")


@dataclass(frozen=True)
class PreparedMinecraftWorldState:
    world_state: WorldState
    blocks: list[dict]
    containers: list[dict]
    agent_updates: list[dict]


def _vec3map_entries(value: Any, field: str) -> list[dict[str, Any]]:
    """座標別データの保存形式を検証し、各座標の項目をコピーして取り出す。"""
    if not isinstance(value, dict) or "__Vec3Map__" not in value:
        raise ValueError(f"{field} must contain __Vec3Map__")
    entries = value["__Vec3Map__"]
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError(f"{field}.__Vec3Map__ must be a list of mappings")
    return deepcopy(entries)


def _position(value: Any, field: str) -> list[float]:
    """__Vec3__形式の座標が、真偽値を除く数値3要素であることを検証して取り出す。"""
    if not isinstance(value, dict) or "__Vec3__" not in value:
        raise ValueError(f"{field} must contain __Vec3__")
    position = value["__Vec3__"]
    if not isinstance(position, list) or len(position) != 3 or any(isinstance(part, bool) or not isinstance(part, Real) for part in position):
        raise ValueError(f"{field}.__Vec3__ must contain three numbers")
    return list(position)


def prepare_agent_updates(status: dict, agent_names: list[str]) -> list[dict[str, Any]]:
    """人物の状態から、移動先・向き・所持品・装備の操作データを作る。

    現在位置がなければ最後の目撃位置を利用する。
    """
    updates = []
    for agent_name in agent_names:
        if agent_name not in status:
            raise ValueError(f"world_state.status is missing agent '{agent_name}'")
        agent_state = status[agent_name]
        if not isinstance(agent_state, dict):
            raise ValueError(f"world_state.status.{agent_name} must be a mapping")
        visible = agent_state.get("visible")
        if visible is not None and not isinstance(visible, dict):
            raise ValueError(f"world_state.status.{agent_name}.visible must be a mapping")
        used_last_seen = visible is None or visible.get("position") is None
        if used_last_seen:
            last_seen = agent_state.get("last_seen")
            if not isinstance(last_seen, dict) or not isinstance(last_seen.get("visible"), dict):
                raise ValueError(f"Agent '{agent_name}' must have a visible or last_seen position")
            visible = last_seen["visible"]
        position_value = visible.get("position")
        if position_value is None:
            raise ValueError(f"Agent '{agent_name}' must have a visible or last_seen position")

        equipment = visible.get("equipment", [])
        if isinstance(equipment, dict):
            equipment = [equipment.get(slot) for slot in EQUIPMENT_SLOTS]
        if not isinstance(equipment, list):
            raise ValueError(f"world_state.status.{agent_name}.visible.equipment must be a list or mapping")

        pitch = visible.get("pitch", 0.0)
        yaw = visible.get("yaw", 0.0)
        if isinstance(pitch, bool) or not isinstance(pitch, Real) or isinstance(yaw, bool) or not isinstance(yaw, Real):
            raise ValueError(f"world_state.status.{agent_name}.visible pitch and yaw must be numbers")

        hidden = agent_state.get("hidden", {})
        if not isinstance(hidden, dict):
            raise ValueError(f"world_state.status.{agent_name}.hidden must be a mapping")
        inventory = hidden.get("inventory", {})
        if not isinstance(inventory, dict):
            raise ValueError(f"world_state.status.{agent_name}.hidden.inventory must be a mapping")

        updates.append({
            "agent_name": agent_name,
            "position": _position(position_value, f"world_state.status.{agent_name}.visible.position"),
            "pitch": float(pitch),
            "yaw": float(yaw),
            "equipment": deepcopy(equipment),
            "inventory": deepcopy(inventory),
            "used_last_seen": used_last_seen,
            "update_pose": True,
            "update_inventory_and_equipment": True,
        })

    return updates


def prepare_minecraft_world_state(world_state: WorldState, agent_names: list[str] | None = None) -> PreparedMinecraftWorldState:
    """WorldStateのブロック・コンテナ・対象人物を検証し、実ワールドへ反映するための更新データを準備する。"""
    if not isinstance(world_state, WorldState):
        raise ValueError("world_state must be a WorldState")
    status = world_state.status
    if agent_names is None:
        agent_names = list(status)
    blocks = _vec3map_entries(world_state.blocks.to_dict(), "world_state.blocks")
    containers = _vec3map_entries(world_state.containers.to_dict(), "world_state.containers")
    agent_updates = prepare_agent_updates(status, agent_names)
    return PreparedMinecraftWorldState(world_state.copy(), blocks, containers, agent_updates)


async def apply_minecraft_world_state(
    mineflayer_connection: AdminMineflayerConnection, prepared_state: PreparedMinecraftWorldState, *,
    clear: bool = True, wait_for_teleport: bool = True,
) -> dict[str, Any]:
    """準備済みの状態を、範囲消去・ブロック配置・コンテナ設定・人物更新の順に実ワールドへ適用し、処理件数を返す。

    途中で失敗した場合は一部が適用済みになり得る。
    """
    blocks, containers, agent_updates = prepared_state.blocks, prepared_state.containers, prepared_state.agent_updates
    if clear:
        await mineflayer_connection.clear_box()
    if blocks:
        await mineflayer_connection.set_blocks(blocks)
    if containers:
        await mineflayer_connection.set_containers(containers)
    for update in agent_updates:
        agent_name = update["agent_name"]
        if update["update_pose"]:
            teleport_options = {"teleport_offset": [0, 0, 0]}
            if not wait_for_teleport:
                teleport_options["wait_for_arrival"] = False
            await mineflayer_connection.teleport(agent_name, update["position"], update["pitch"], update["yaw"], **teleport_options)
        if update["update_inventory_and_equipment"]:
            await mineflayer_connection.set_inventory_and_equipment(agent_name, update["inventory"], update["equipment"])

    return {
        "blocks": len(blocks), "containers": len(containers), "agents": [update["agent_name"] for update in agent_updates],
        "last_seen_agents": [update["agent_name"] for update in agent_updates if update["used_last_seen"]],
    }
