from __future__ import annotations

import math
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

@dataclass(frozen=True)
class ParsedPlayersTickData:
    server_tick: int | None
    players: dict


@dataclass(frozen=True)
class ParsedIncrementalEvent:
    event_type: str
    mc_name: str | None
    payload: dict


def _normalize_player_data(player_data: dict) -> dict:
    """Minecraftの人物情報を内部の観測形式へ変換する。

    位置・角度・装備・所持品を、記憶処理が扱う形式にそろえる。
    """
    source_visible = player_data["visible"]
    visible = dict(source_visible)
    hidden = dict(player_data.get("hidden", {}))

    visible["position"] = {"__Vec3__": list(source_visible["position"])}
    visible["velocity"] = {"__Vec3__": list(source_visible["velocity"])}
    visible["pitch"] = -float(source_visible["pitch"]) / 180.0 * math.pi
    visible["yaw"] = (-float(source_visible["yaw"]) + 180.0) / 180.0 * math.pi

    equipment = source_visible.get("equipment", {})
    if isinstance(equipment, list):
        visible["equipment"] = equipment
    else:
        visible["equipment"] = [
            equipment.get("head"), equipment.get("chest"), equipment.get("legs"),
            equipment.get("feet"), equipment.get("mainhand"), equipment.get("offhand"),
        ]

    inventory = hidden.get("inventory", {})
    if isinstance(inventory, list):
        inventory_counts: dict[str, int] = {}
        for slot in inventory:
            item_name = slot["item"]
            inventory_counts[item_name] = inventory_counts.get(item_name, 0) + int(slot["count"])
        hidden["inventory"] = inventory_counts
    elif isinstance(inventory, dict):
        hidden["inventory"] = dict(inventory)
    else:
        hidden["inventory"] = {}

    return {"name": player_data["name"], "visible": visible, "hidden": hidden}


def _parse_minecraft_record(record_from_minecraft: dict, admin_mc_name: str | None) -> ParsedPlayersTickData | ParsedIncrementalEvent | None:
    """Minecraftの受信記録を、人物のtick情報または出来事として読み解く。

    管理者の記録など観測対象外の内容は除く。
    """
    event_type = record_from_minecraft["type"]
    data = record_from_minecraft["data"]

    if event_type == "players_tick":
        players = {}
        for player_data in data["players"].values():
            if admin_mc_name is not None and player_data["name"] == admin_mc_name:
                continue
            players[player_data["name"]] = _normalize_player_data(player_data)
        return ParsedPlayersTickData(server_tick=record_from_minecraft.get("tick"), players=players)

    if event_type == "container_close" and data.get("containerBlock") != "chest":
        return None

    mc_name = None
    if event_type in ["swing_hand", "craft_item", "container_close"]:
        mc_name = data["playerName"]
    elif event_type in ["player_move_start", "player_move_end"]:
        mc_name = data["player"]["name"]
    elif event_type == "chat":
        source_mc_name = data["player"]["name"]
        message = data["message"]
        if admin_mc_name is not None and source_mc_name == admin_mc_name:
            match = re.match(r"^([^:]+) said: (.*)$", message)
            if not match:
                raise ValueError(f"Invalid admin chat message: {message}")
            mc_name = match.group(1)
            data = dict(data)
            data["message"] = match.group(2)
        else:
            mc_name = source_mc_name
    elif event_type == "tell":
        mc_name = data["player"]["name"]
    elif "playerName" in data:
        mc_name = data["playerName"]

    if admin_mc_name is not None and mc_name == admin_mc_name:
        return None
    return ParsedIncrementalEvent(event_type=event_type, mc_name=mc_name, payload=data)


def _unpack_minecraft_message(message_from_minecraft: dict) -> list[dict]:
    """受信メッセージにまとめられたイベントを、順序を保って個々の記録へ展開する。"""
    records_from_minecraft = []
    stack = [message_from_minecraft]
    while stack:
        record_from_minecraft = stack.pop()
        is_world_event = "eventName" in record_from_minecraft and "visible" in record_from_minecraft
        if "objective" in record_from_minecraft or is_world_event:
            records_from_minecraft.append(record_from_minecraft)
        elif record_from_minecraft.get("type") == "event_batch":
            stack.extend(reversed(record_from_minecraft.get("items", [])))
        else:
            records_from_minecraft.append(record_from_minecraft)
    return records_from_minecraft


def _advance_global_tick(latest_global_tick: int, latest_server_tick: int | None, server_tick: int | None) -> tuple[int, int | None]:
    """サーバーtickの差分から観測用の通しtickを進める。

    サーバーtickがない場合は1進め、逆行や重複はエラーにする。
    """
    if server_tick is None:
        return latest_global_tick + 1, latest_server_tick

    server_tick = int(server_tick)
    tick_diff = 1 if latest_server_tick is None else server_tick - latest_server_tick
    if tick_diff <= 0:
        raise ValueError(f"server_tick={server_tick}, received_latest_server_tick={latest_server_tick}")
    return latest_global_tick + tick_diff, server_tick


def _serialize_vec3(vec: Any, *, data_type=int) -> dict:
    """文字列または座標の並びを、内部で使う__Vec3__付きのXYZ形式へ変換する。"""
    if isinstance(vec, str):
        position = [data_type(part) for part in vec.split(",")]
    else:
        position = [data_type(value) for value in vec]
    return {"__Vec3__": position}


def _extract_updated_blocks(event_list: list[dict]) -> list[dict]:
    """ブロック更新イベントから、更新座標・種類・状態ID・属性を取り出して一覧にする。"""
    updated_blocks = []
    for event in event_list:
        if event["eventName"] != "blockUpdate":
            continue

        block = {"position": deepcopy(event["blockPos"]), "name": event["visible"]["name"]}
        if "stateId" in event["visible"]:
            block["stateId"] = event["visible"]["stateId"]
        if "properties" in event["visible"]:
            properties = event["visible"]["properties"]
            block["properties"] = {key.lower(): str(value).lower() for key, value in properties.items()}
        updated_blocks.append(block)
    return updated_blocks


def _build_world_event(parsed_event: ParsedIncrementalEvent) -> dict | None:
    """Minecraftの出来事を、公開情報と非公開情報を分けた内部イベントへ変換する。

    未対応の出来事はNoneを返す。
    """
    event_type = parsed_event.event_type
    data = parsed_event.payload
    agent_name = parsed_event.mc_name

    if event_type == "block_update":
        block = {"name": data["new"]["blockName"], "stateId": data["new"]["stateId"]}
        if data["new"].get("properties"):
            block["properties"] = data["new"]["properties"]
        return {
            "eventName": "blockUpdate",
            "blockPos": _serialize_vec3(data["pos"]),
            "visible": block,
            "hidden": {},
        }

    if event_type == "swing_hand":
        return {"eventName": "swingHand", "agentName": agent_name, "visible": {}, "hidden": {}}

    if event_type == "craft_item":
        return {
            "eventName": "craftItem",
            "agentName": agent_name,
            "visible": {
                "itemName": data["item"],
                "producedCount": 1,
                "consumedItems": dict(data["consumed"]),
                "craftingTablePos": _serialize_vec3(data["tablePos"]) if data.get("tablePos") is not None else None,
            },
            "hidden": {},
        }

    if event_type == "player_move_start":
        return {
            "eventName": "moveStart",
            "agentName": agent_name,
            "visible": {
                "startPos": _serialize_vec3(data["from"], data_type=float),
                "goalPos": {"__Vec3__": ["unknown", "unknown", "unknown"]},
            },
            "hidden": None,
        }

    if event_type == "player_move_end":
        return {
            "eventName": "moveEnd",
            "agentName": agent_name,
            "visible": {
                "startPos": _serialize_vec3(data["from"], data_type=float),
                "goalPos": _serialize_vec3(data["to"], data_type=float),
            },
            "hidden": None,
        }

    if event_type == "chat":
        return {
            "eventName": "chat",
            "visible": {"agentName": agent_name, "msg": data["message"]},
            "hidden": None,
        }

    if event_type == "tell":
        sender_name = data["player"]["name"]
        recipient_name = data["recipient"]["name"]
        if sender_name != recipient_name:
            return None
        return {
            "eventName": "think",
            "agentName": agent_name,
            "visible": {},
            "hidden": {"msg": data["message"]},
        }

    return None


def _build_container_events(data: dict, agent_name: str | None) -> list[dict]:
    """チェスト内のアイテム増減を、取り出し・格納・使用のイベントへ変換する。

    中身の確定値は非公開情報に含める。
    """
    got_items = {}
    deposited_items = {}
    for name, count in data["delta"].items():
        if count < 0:
            got_items[name] = -count
        elif count > 0:
            deposited_items[name] = count

    event_list = []
    hidden_items = dict(data["finalCounts"])
    chest_pos = _serialize_vec3(data["pos"])
    if got_items:
        get_event = {
            "eventName": "getItemFromChest",
            "agentName": agent_name,
            "visible": {"chestPos": chest_pos, "gotItems": got_items},
            "hidden": {"chestItems": hidden_items},
        }
        event_list.append(get_event)
    if deposited_items:
        deposit_event = {
            "eventName": "depositItemIntoChest",
            "agentName": agent_name,
            "visible": {"chestPos": chest_pos, "depositedItems": deposited_items},
            "hidden": {"chestItems": hidden_items},
        }
        event_list.append(deposit_event)
    if got_items or deposited_items:
        use_event = {
            "eventName": "useChest",
            "agentName": agent_name,
            "visible": {"chestPos": chest_pos, "gotItems": got_items, "depositedItems": deposited_items},
            "hidden": {"chestItems": hidden_items},
        }
        event_list.append(use_event)
    return event_list


def _build_tick_observation(parsed_tick_data: ParsedPlayersTickData, pending_events: list[dict], latest_global_tick: int, latest_server_tick: int | None) -> tuple[dict, int, int | None]:
    """人物のtick情報と、それまでに届いた出来事をまとめ、ブロック更新を含む1回分のワールド観測と更新後のtickを返す。"""
    status = deepcopy(parsed_tick_data.players)

    global_tick, latest_server_tick = _advance_global_tick(latest_global_tick, latest_server_tick, parsed_tick_data.server_tick)
    event_list = deepcopy(pending_events)
    tick_observation = {
        "globalTick": global_tick,
        "objective": {
            "status": status,
            "events": event_list,
            "blocksToUpdate": _extract_updated_blocks(event_list),
        },
    }
    return tick_observation, global_tick, latest_server_tick


class MinecraftObservationProcessor:
    def __init__(self, *, initial_tick: int, admin_mc_name: str | None = None) -> None:
        """人物のtick到着まで出来事を貯める領域と、サーバーtick・観測用tick・管理者名を初期化する。"""
        self.admin_mc_name = admin_mc_name
        self.pending_events: list[dict] = []
        self.latest_global_tick = int(initial_tick)
        self.latest_server_tick: int | None = None

    def process(self, message_from_minecraft: dict) -> list[dict]:
        """受信メッセージを読み解き、人物のtick情報が来るたびに蓄積した出来事を観測として出力する。

        変換が成功した場合だけ内部のtickと待機中イベントを更新する。
        """
        admin_mc_name = self.admin_mc_name
        pending_events = deepcopy(self.pending_events)
        latest_global_tick = self.latest_global_tick
        latest_server_tick = self.latest_server_tick

        tick_observations = []
        records_from_minecraft = _unpack_minecraft_message(message_from_minecraft)
        for record_from_minecraft in records_from_minecraft:
            if "objective" in record_from_minecraft:
                raise ValueError()
            if "eventName" in record_from_minecraft and "visible" in record_from_minecraft:
                pending_events.append(deepcopy(record_from_minecraft))
                continue

            parsed_record = _parse_minecraft_record(record_from_minecraft, admin_mc_name)
            if parsed_record is None:
                continue
            if isinstance(parsed_record, ParsedIncrementalEvent):
                if parsed_record.event_type == "container_close":
                    pending_events.extend(_build_container_events(parsed_record.payload, parsed_record.mc_name))
                else:
                    event = _build_world_event(parsed_record)
                    if event is not None:
                        pending_events.append(event)
            elif isinstance(parsed_record, ParsedPlayersTickData):
                tick_observation, latest_global_tick, latest_server_tick = _build_tick_observation(parsed_record, pending_events, latest_global_tick, latest_server_tick)
                tick_observations.append(tick_observation)
                pending_events = []
            else:
                raise TypeError(f"Unexpected parsed Minecraft record type: {type(parsed_record).__name__}")

        self.pending_events = pending_events
        self.latest_global_tick = latest_global_tick
        self.latest_server_tick = latest_server_tick
        return tick_observations


def _build_world_config_from_block_snapshots(snapshots: list[dict]) -> dict:
    """分割受信した初期ブロック一覧を順番に結合し、観測範囲と初期ワールド状態を組み立てる。"""
    snapshots = sorted(snapshots, key=lambda snapshot: snapshot["data"].get("sequence", 0))
    first = snapshots[0]
    bounds = first["data"]["bounds"]
    blocks = []
    containers = []

    for snapshot in snapshots:
        for item in snapshot["data"]["items"]:
            pos = list(item["pos"])
            block = item["block"]
            entry = {
                "position": pos,
                "name": block["blockName"],
                "stateId": block["stateId"],
            }

            if block.get("properties"):
                entry["properties"] = block["properties"]

            blocks.append(entry)
            if block["blockName"] == "chest":
                containers.append({"position": pos})

    return {
        "envBox": [
            [bounds["min"]["x"], bounds["min"]["y"], bounds["min"]["z"]],
            [bounds["max"]["x"], bounds["max"]["y"], bounds["max"]["z"]],
        ],
        "state": {
            "globalTick": first.get("tick", -1),
            "status": {},
            "blocks": {"__Vec3Map__": blocks},
            "containers": {"__Vec3Map__": containers},
        },
    }


def build_world_config_from_block_snapshot_buffer(message_from_minecraft: dict, block_snapshot_buffer: dict) -> tuple[dict, dict] | None:
    """初期ブロックデータを要求IDごとに蓄積する。

    完了したら初期状態と受信件数を返してバッファを消し、未完了ならNoneを返す。
    """
    if message_from_minecraft.get("type") == "block_snapshot":
        block_snapshot_items = [message_from_minecraft]
    else:
        block_snapshot_items = [item for item in message_from_minecraft.get("items", []) if item.get("type") == "block_snapshot"]

    if not block_snapshot_items:
        return None

    for item in block_snapshot_items:
        data = item["data"]
        request_id = data.get("requestId", "default")
        sequence = data.get("sequence", 0)
        complete = item.get("complete", data.get("complete", False))
        request_snapshots = block_snapshot_buffer.setdefault(request_id, {})
        request_snapshots[sequence] = item

        if not complete:
            continue

        sequences = sorted(request_snapshots)
        snapshots = [request_snapshots[snapshot_sequence] for snapshot_sequence in sequences]
        world_config = _build_world_config_from_block_snapshots(snapshots)
        world_state = world_config["state"]
        bounds = data["bounds"]
        min_bound = bounds["min"]
        max_bound = bounds["max"]
        x_size = max_bound["x"] - min_bound["x"] + 1
        y_size = max_bound["y"] - min_bound["y"] + 1
        z_size = max_bound["z"] - min_bound["z"] + 1
        expected_count = x_size * y_size * z_size
        snapshot_info = {
            "request_id": request_id,
            "sequences": sequences,
            "actual_count": len(world_state["blocks"]["__Vec3Map__"]),
            "expected_count": expected_count,
            "chest_count": len(world_state["containers"]["__Vec3Map__"]),
        }
        del block_snapshot_buffer[request_id]
        return world_config, snapshot_info

    return None
