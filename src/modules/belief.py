from __future__ import annotations

import bisect
import json
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from collections import defaultdict
from typing import Iterable, Optional

from jinja2 import Environment, StrictUndefined


current_loader = ContextVar("current_loader")
EXCLUDE_NAMES: list[str] = ["Camera", "admin"]


def _vec_to_key(vec: Iterable[int | float]) -> tuple[int, int, int]:
    """XYZ座標を、座標別データの辞書キーに使う整数3要素のタプルへ変換する。"""
    x, y, z = vec
    return int(x), int(y), int(z)


class Vec3Map:
    def __init__(self, vec3map: dict | list):
        """保存形式の座標一覧を読み込み、整数座標をキーに値を検索できる辞書を作る。"""
        if isinstance(vec3map, dict) and "__Vec3Map__" in vec3map:
            vec3map = vec3map["__Vec3Map__"]

        self.data: dict[tuple[int, int, int], dict] = {}
        for entry in vec3map:
            value = dict(entry)
            pos = value.pop("position")
            self.data[(int(pos[0]), int(pos[1]), int(pos[2]))] = value

    def set(self, vec: Iterable[int | float], value: dict) -> None:
        """指定座標の値を登録する。

        すでに同じ座標がある場合は上書きする。
        """
        self.data[_vec_to_key(vec)] = value

    def get(self, vec: Iterable[int | float]) -> dict:
        """指定座標に保存されている値を取得する。"""
        return self.data[_vec_to_key(vec)]

    def has(self, vec: Iterable[int | float]) -> bool:
        """指定座標の値が登録されているかを真偽値で返す。"""
        return _vec_to_key(vec) in self.data

    def delete(self, vec: Iterable[int | float]) -> None:
        """指定座標の値を辞書から削除する。"""
        del self.data[_vec_to_key(vec)]

    def to_dict(self) -> dict:
        """座標別データを保存・通信に使う__Vec3Map__形式へ変換し、値をコピーして返す。"""
        values = self.data
        data = []
        for (x, y, z), value in values.items():
            row = {"position": [x, y, z]}
            row.update(deepcopy(value))
            data.append(row)
        return {"__Vec3Map__": data}

    def keys(self) -> list[list[int]]:
        """登録されている座標をXYZの一覧として返す。"""
        return [[x, y, z] for (x, y, z) in self.data.keys()]


class WorldState:
    def __init__(self, state_dict: dict):
        """人物・ブロック・コンテナ・tickを初期データからコピーし、更新可能なワールド状態を作る。"""
        self.global_tick = int(state_dict.get("globalTick", -1))
        self.status = deepcopy(state_dict["status"])
        self.blocks = Vec3Map(deepcopy(state_dict["blocks"]))
        self.containers = Vec3Map(deepcopy(state_dict["containers"]))

    def to_dict(self) -> dict:
        """現在のワールド状態を、保存・通信できる辞書形式のコピーとして返す。"""
        tick, status, blocks, containers = self.global_tick, self.status, self.blocks, self.containers
        return {"globalTick": tick, "status": deepcopy(status), "blocks": blocks.to_dict(), "containers": containers.to_dict()}

    def copy(self) -> "WorldState":
        """このワールド状態を独立して更新できるよう、内部データをコピーしたWorldStateを返す。"""
        return WorldState(self.to_dict())

    def update(self, obs: dict) -> None:
        """1回分の観測を人物・ブロック・コンテナへ反映し、現在のtickを進める。

        更新内容を一時領域で組み立ててから状態へ反映する。
        """
        current_status, current_blocks, current_containers = self.status, self.blocks, self.containers
        global_tick = int(obs["globalTick"])
        status, events, updated_blocks = obs["objective"]["status"], obs["objective"]["events"], obs["objective"]["blocksToUpdate"]
        # Prepare only mutable status/container data and changed blocks before committing an observation.
        updated = WorldState({"globalTick": global_tick, "status": current_status, "blocks": {"__Vec3Map__": []}, "containers": current_containers.to_dict()})
        has_inventory_info = {name: "hidden" in info and "inventory" in info["hidden"] for name, info in status.items()}
        updated._update_status_state(status, events, has_inventory_info)
        apply_block_updates(updated.blocks, updated_blocks)
        updated._update_container_state(events, updated_blocks, current_blocks)
        self.status = updated.status
        current_blocks.data.update(updated.blocks.data)
        self.containers = updated.containers
        self.global_tick = global_tick

    def _update_status_state(self, status: dict, events: list, has_inventory_info: dict) -> None:
        """人物の公開・非公開状態と目撃情報を更新する。

        観測に所持品の確定値がない場合は、出来事から所持数の増減を反映する。
        """
        known_status = self.status
        for agent_name, agent_status in status.items():
            known_status.setdefault(agent_name, {"visible": {}, "hidden": {}})
            if "visible" in agent_status:
                known_status[agent_name]["visible"] = deepcopy(agent_status["visible"])
            else:
                known_status[agent_name].pop("visible", None)
            if "hidden" in agent_status:
                known_status[agent_name]["hidden"] = deepcopy(agent_status["hidden"])
            if "last_seen" in agent_status:
                known_status[agent_name]["last_seen"] = deepcopy(agent_status["last_seen"])
            else:
                known_status[agent_name].pop("last_seen", None)
            if "hidden" in known_status[agent_name]:
                known_status[agent_name]["hidden"].setdefault("inventory", {})

        def _add(agent_name: str, name: str, count: int) -> None:
            """所持品の確定値が今回の観測にない場合に限り、出来事で増えたアイテム数を加算する。"""
            if has_inventory_info.get(agent_name):
                return
            inv = known_status.setdefault(agent_name, {"visible": {}, "hidden": {}}).setdefault("hidden", {}).setdefault(
                "inventory", {}
            )
            inv[name] = inv.get(name, 0) + count

        def _remove(agent_name: str, name: str, count: int) -> None:
            """所持品の確定値が今回の観測にない場合に限り、出来事で減ったアイテム数を減算する。

            残数がなくなった項目は削除する。
            """
            if has_inventory_info.get(agent_name):
                return
            inv = known_status.setdefault(agent_name, {"visible": {}, "hidden": {}}).setdefault("hidden", {}).setdefault(
                "inventory", {}
            )
            if name not in inv:
                return
            inv[name] -= count
            if inv[name] <= 0:
                del inv[name]

        for event in events:
            event_name = event.get("eventName")
            if event_name == "mineBlock":
                _add(event["agentName"], event["visible"]["blockName"], 1)
            elif event_name == "craftItem":
                _add(event["agentName"], event["visible"]["itemName"], event["visible"]["producedCount"])
                for name, count in event["visible"].get("consumedItems", {}).items():
                    _remove(event["agentName"], name, count)
            elif event_name == "smeltItem":
                _add(
                    event["agentName"],
                    event["visible"]["producedItemName"],
                    event["visible"]["producedCount"],
                )
                for name, count in event["visible"].get("consumedItems", {}).items():
                    _remove(event["agentName"], name, count)
            elif event_name == "getItemFromChest":
                for name, count in event["visible"].get("gotItems", {}).items():
                    _add(event["agentName"], name, count)
            elif event_name == "depositItemIntoChest":
                for name, count in event["visible"].get("depositedItems", {}).items():
                    _remove(event["agentName"], name, count)
            elif event_name == "giveItemToOther":
                _remove(event["agentName"], event["visible"]["itemName"], event["visible"]["count"])
                _add(
                    event["visible"]["otherAgentName"],
                    event["visible"]["itemName"],
                    event["visible"]["count"],
                )
            elif event_name == "receiveItemFromOther":
                _add(event["agentName"], event["visible"]["itemName"], event["visible"]["count"])
                _remove(
                    event["visible"]["otherAgentName"],
                    event["visible"]["itemName"],
                    event["visible"]["count"],
                )

    def _update_container_state(self, events: list, updated_blocks: list, previous_blocks: Vec3Map) -> None:
        """ブロックの設置・撤去とチェスト操作をコンテナ状態へ反映する。

        中身の確定値があればそれを使い、なければ取り出し・格納の差分を反映する。
        """
        current_blocks, containers = self.blocks, self.containers
        for block in updated_blocks:
            pos = block["position"]["__Vec3__"]
            if block.get("name") == "chest" and not containers.has(pos):
                containers.set(pos, {})

        for pos in list(containers.keys()):
            blocks = current_blocks if current_blocks.has(pos) else previous_blocks
            if blocks.has(pos) and blocks.get(pos)["name"] != "chest":
                containers.delete(pos)

        for event in events:
            if event.get("eventName") not in ["getItemFromChest", "depositItemIntoChest"]:
                continue

            chest_pos = event.get("visible", {}).get("chestPos")
            if chest_pos is None:
                raise ValueError(f"chestPos is not defined. event={json.dumps(event, ensure_ascii=False)}")

            pos = chest_pos["__Vec3__"]
            if event.get("hidden", {}).get("chestItems") is not None:
                containers.set(pos, deepcopy(event["hidden"]["chestItems"]))
                continue

            chest_items = {}
            if containers.has(pos):
                chest_items = deepcopy(containers.get(pos))

            if event["eventName"] == "getItemFromChest":
                for item_name, count in event.get("visible", {}).get("gotItems", {}).items():
                    if item_name not in chest_items:
                        continue
                    if count >= chest_items[item_name]:
                        del chest_items[item_name]
                    else:
                        chest_items[item_name] -= count
            elif event["eventName"] == "depositItemIntoChest":
                for item_name, count in event.get("visible", {}).get("depositedItems", {}).items():
                    chest_items[item_name] = chest_items.get(item_name, 0) + count
            else:
                raise ValueError(f'Invalid event name "{event["eventName"]}"')

            containers.set(pos, chest_items)


def apply_block_updates(blocks: Vec3Map, updated_blocks: list[dict]) -> None:
    """ブロック更新一覧の座標ごとに、種類・状態ID・属性をブロック記憶へ保存する。"""
    for block in updated_blocks:
        value = {"name": block["name"]}
        if "stateId" in block:
            value["stateId"] = block["stateId"]
        if "properties" in block and block["properties"] is not None:
            value["properties"] = deepcopy(block["properties"])
        blocks.set(block["position"]["__Vec3__"], value)


def advance_state_one_observation(state: WorldState, observation_or_legacy: dict) -> None:
    """新旧の観測形式を受け付け、1回分の観測でワールド状態を更新する。"""
    if "objective" in observation_or_legacy:
        obs = deepcopy(observation_or_legacy)
    else:
        obs = {
            "globalTick": observation_or_legacy["globalTick"],
            "objective": {
                "status": deepcopy(observation_or_legacy["status"]),
                "events": deepcopy(observation_or_legacy["events"]),
                "blocksToUpdate": deepcopy(observation_or_legacy["blocksToUpdate"]),
            },
        }
    state.update(obs)


class ObservationHistory:
    def __init__(self) -> None:
        """観測履歴、tick一覧、ブロック更新履歴を空の状態で用意する。"""
        self.objective_list: list[dict] = []
        self._ticks: list[int] = []
        self.block_updates: list[dict] = []

    def add(self, obs: dict) -> None:
        """1回分の観測をコピーして履歴へ追加し、tickとブロック更新も検索用の一覧に記録する。"""
        objective_list, ticks, block_updates = self.objective_list, self._ticks, self.block_updates
        objective = {
            "globalTick": obs["globalTick"],
            "status": deepcopy(obs["objective"]["status"]),
            "events": deepcopy(obs["objective"]["events"]),
            "blocksToUpdate": deepcopy(obs["objective"]["blocksToUpdate"]),
        }
        if "player_visibility" in obs:
            objective["player_visibility"] = deepcopy(obs["player_visibility"])
        if "block_visibility" in obs:
            objective["block_visibility"] = deepcopy(obs["block_visibility"])
        objective_list.append(objective)
        ticks.append(int(obs["globalTick"]))
        if objective["blocksToUpdate"]:
            block_updates.append(objective)

    @property
    def latest_tick(self) -> int:
        """最後に記録した観測のtickを返す。

        履歴が空なら-1を返す。
        """
        if not self._ticks:
            return -1
        return self._ticks[-1]

    @property
    def ticks(self) -> list[int]:
        """記録済みのtick一覧を、元の一覧を変更できないコピーとして返す。"""
        return deepcopy(self._ticks)

    def contains(self, tick: int) -> bool:
        """指定したtickの観測が履歴に存在するかを二分探索で調べる。"""
        ticks = self._ticks
        index = bisect.bisect_left(ticks, tick)
        return index < len(ticks) and ticks[index] == tick

    def get_range(self, *, from_tick=None, to_tick=None) -> list[dict]:
        """開始tickから終了tickまで、両端を含む観測履歴をコピーして返す。"""
        ticks, frames = self._ticks, self.objective_list
        start = 0 if from_tick is None else bisect.bisect_left(ticks, from_tick)
        end = len(ticks) if to_tick is None else bisect.bisect_right(ticks, to_tick)
        return deepcopy(frames[start:end])

    def get_block_updates(self, *, to_tick: int) -> list[dict]:
        """指定tick以前のブロック更新履歴を、指定tickも含めて取得する。"""
        frames = self.block_updates
        end = bisect.bisect_right([frame["globalTick"] for frame in frames], to_tick)
        return deepcopy(frames[:end])

    def at_or_before(self, tick: int) -> dict | None:
        """指定tick以下で最も新しい観測をコピーして返す。

        該当する観測がなければNoneを返す。
        """
        ticks, frames = self._ticks, self.objective_list
        index = bisect.bisect_right(ticks, tick) - 1
        return deepcopy(frames[index]) if index >= 0 else None


    def remove_ticks(self, ticks_to_remove: set[int]) -> list[int]:
        """指定tickの観測を通常履歴・tick一覧・ブロック更新履歴から削除し、実際に削除したtickを返す。"""
        objective_list, ticks, block_updates = self.objective_list, self._ticks, self.block_updates
        removed_ticks = sorted(ticks_to_remove.intersection(ticks))
        if not removed_ticks:
            return []
        objective_list[:] = [observation for observation in objective_list if observation["globalTick"] not in ticks_to_remove]
        ticks[:] = [tick for tick in ticks if tick not in ticks_to_remove]
        block_updates[:] = [observation for observation in block_updates if observation["globalTick"] not in ticks_to_remove]
        return removed_ticks

    def _get_idx_of(self, tick: int, strict: bool = False) -> int:
        """指定tickを二分探索し、履歴内の位置を返す。

        厳密検索では未登録tickをエラーにし、それ以外では挿入位置を返す。
        """
        idx = bisect.bisect_left(self._ticks, tick)
        if strict and (idx >= len(self._ticks) or self._ticks[idx] != tick):
            raise KeyError(f"tick {tick} not found")
        return idx

    def get_objective(self, tick: int, strict: bool = True) -> dict:
        """指定tickの観測をコピーして返す。

        厳密検索を無効にした場合は、そのtick以上の観測を検索する。
        """
        idx = self._get_idx_of(tick, strict=strict)
        return deepcopy(self.objective_list[idx])

    def events_until(self, tick: int | None = None) -> dict[int, list[dict]]:
        """指定tick以前の出来事を、出来事が存在するtickごとにまとめて返す。"""
        ticks, objective_list = self._ticks, self.objective_list
        events = {}
        end = len(ticks) if tick is None else bisect.bisect_right(ticks, tick)
        for observation in objective_list[:end]:
            if observation["events"]:
                events.setdefault(int(observation["globalTick"]), []).extend(deepcopy(observation["events"]))
        return events


class Belief:
    def __init__(
        self,
        *,
        initial_state: WorldState,
        main_agent_name: str | None = None,
    ) -> None:
        """エージェントの初期知識をコピーし、現在の状態と観測履歴を管理する記憶を作る。"""
        self.initial_state = initial_state.copy()
        self.main_agent_name = main_agent_name
        self.state = initial_state.copy()
        self.history = ObservationHistory()

    @property
    def agent_name(self) -> str:
        """この記憶の主体となるエージェント名を返す。"""
        return self.main_agent_name

    def update_state(self, observation_or_legacy: dict) -> None:
        """1回分の観測を現在の記憶状態へ反映する。

        観測履歴への追加は行わない。
        """
        advance_state_one_observation(self.state, observation_or_legacy)

    def append_history(self, observation_or_legacy: dict) -> None:
        """新旧の観測形式を受け付け、観測履歴へ追加する。

        現在の記憶状態は更新しない。
        """
        if "objective" in observation_or_legacy:
            obs = observation_or_legacy
        else:
            obs = {
                "globalTick": observation_or_legacy["globalTick"],
                "objective": {
                    "status": observation_or_legacy["status"],
                    "events": observation_or_legacy["events"],
                    "blocksToUpdate": observation_or_legacy["blocksToUpdate"],
                },
            }
        self.history.add(obs)

    def add_observation(self, observation: dict) -> None:
        """1回分の観測を現在状態へ反映してから、同じ観測を履歴へ追加する。"""
        self.update_state(observation)
        self.append_history(observation)

    def to_loader(self) -> "WorldObservationLoader":
        """現在状態と観測履歴をコピーし、プロンプトのテンプレートから参照できる読み出し用データに変換する。"""
        state, history, main_agent_name = self.state, self.history, self.main_agent_name
        objective_list = deepcopy(history.objective_list)
        ticks = history.ticks
        state_dict = state.to_dict()
        return WorldObservationLoader(
            state_dict=state_dict,
            events=history.events_until(),
            objective_list=objective_list,
            ticks=ticks,
            main_agent_name=main_agent_name,
        )


@dataclass
class LoaderHistory:
    events: dict[int, list[dict]]
    objective_list: list[dict]
    ticks: list[int]


class WorldObservationLoader:
    def __init__(
        self,
        *,
        state_dict: dict,
        events: dict[int, list[dict]],
        objective_list: list[dict],
        ticks: list[int],
        main_agent_name: str | None = None,
        block_snapshot: dict[tuple[int, int, int], dict] | None = None,
    ):
        """プロンプト用の状態・履歴・出来事・主体名を保持し、テンプレートから参照できるようにする。"""
        self.state_dict = state_dict
        self.history = LoaderHistory(events=events, objective_list=objective_list, ticks=ticks)
        self.main_agent_name = main_agent_name
        self.block_snapshot = block_snapshot
        self.agent_list = sorted(self.state_dict["status"].keys())

    def iter_blocks(self):
        """別途保持されたブロック一覧、または状態に含まれる一覧から、座標とブロック情報を順に返す。"""
        block_snapshot, state_dict = self.block_snapshot, self.state_dict
        if block_snapshot is not None:
            yield from block_snapshot.items()
        else:
            for block in state_dict["blocks"]["__Vec3Map__"]:
                yield block["position"], block

    def get_latest_state(self) -> tuple[dict, int]:
        """最新状態とそのtickを返す。

        ブロックが別途保持されている場合は、それを状態へ組み込んだコピーを返す。
        """
        state_dict, block_snapshot = self.state_dict, self.block_snapshot
        state = deepcopy(state_dict)
        if block_snapshot is not None:
            state["blocks"] = {"__Vec3Map__": [{"position": list(position), **deepcopy(block)} for position, block in block_snapshot.items()]}
        return state, int(state["globalTick"])

    def get_latest_history(self) -> tuple[dict, int]:
        """最も新しい観測履歴とtickを返す。

        履歴がない場合はエラーにする。
        """
        history = self.history
        if not history.ticks:
            raise ValueError("No observation history has been recorded yet.")
        objective = deepcopy(history.objective_list[-1])
        return objective, int(objective["globalTick"])

    def get_history(self, tick: int) -> tuple[dict, int]:
        """指定tickと一致する観測履歴を検索し、そのコピーとtickを返す。

        見つからなければKeyErrorにする。
        """
        history = self.history
        idx = bisect.bisect_left(history.ticks, tick)
        if idx >= len(history.ticks) or history.ticks[idx] != tick:
            raise KeyError(f"tick {tick} not found")
        objective = deepcopy(history.objective_list[idx])
        return objective, int(tick)


def _iter_events(events: dict) -> list[tuple[int, dict]]:
    """tickごとに記録された出来事を、tick順に1件ずつ取り出す。"""
    rows = []
    for tick, events_at_tick in events.items():
        for event in events_at_tick:
            rows.append((int(tick), event))
    rows.sort(key=lambda row: row[0])
    return rows


def _get_event_agent_name(event: dict) -> str | None:
    """出来事の主体名を取得する。

    直接のagentNameを優先し、なければ公開情報内の名前を使う。
    """
    if "agentName" in event:
        return event["agentName"]
    return event.get("visible", {}).get("agentName")


def _summarize_events(obs: dict) -> dict:
    """出来事の有無・種類・関係する人物・チャット内容を、プロンプトで使う要約情報へまとめる。"""
    summary = {
        "has_any_event": False,
        "event_names": [],
        "events_by_type": {},
    }

    for event in obs["objective"]["events"]:
        event_name = event.get("eventName")
        if not event_name:
            continue

        summary["has_any_event"] = True

        if event_name not in summary["event_names"]:
            summary["event_names"].append(event_name)

        agent_name = _get_event_agent_name(event)
        entry = summary["events_by_type"].setdefault(
            event_name,
            {
                "occurred": False,
                "agents": [],
                "messages": [],
            },
        )
        entry["occurred"] = True

        if agent_name is not None and agent_name not in entry["agents"]:
            entry["agents"].append(agent_name)

        if event_name == "chat":
            msg = event.get("visible", {}).get("msg")
            if msg is not None:
                entry["messages"].append(
                    {
                        "agent": agent_name,
                        "msg": msg,
                    }
                )

    return summary


#############################################################################
# FILTER

def get_loader():
    """現在レンダリングしているテンプレートに対応する観測データの読み出し元を取得する。"""
    return current_loader.get()

def _resolve_agent_name(agent_name: Optional[str]) -> str:
    """明示された名前、記憶の主体、唯一の登録名の順に対象者を決める。

    一意に決まらなければエラーにする。
    """
    if agent_name is not None:
        return agent_name
    loader = get_loader()
    if getattr(loader, "main_agent_name", None):
        return loader.main_agent_name
    else:
        if len(loader.agent_list) == 1:
            return loader.agent_list[0]
        raise ValueError("agent_name is required when using the world branch with multiple agents.")

def position(agent_name: Optional[str] = None, ignore_last_seen: bool = True):
    """対象者の現在の公開位置をプロンプト用の文字列で返す。

    位置がなければデータなしと示す。
    ignore_last_seen引数は現在の処理では参照しない。
    """
    del ignore_last_seen
    loader = get_loader()
    latest_state = loader.state_dict
    agent_name = _resolve_agent_name(agent_name)
    try:
        return latest_state["status"][agent_name]["visible"]["position"]["__Vec3__"]
    except Exception:
        return "No data"

def thought() -> str:
    """対象者の非公開の思考イベントを時系列の文章に整える。

    記録がなければ思考なしと示す。
    """
    loader = get_loader()
    lines = []
    for tick, event in _iter_events(loader.history.events):
        if event.get("eventName") != "think":
            continue
        if "hidden" not in event:
            continue
        msg = json.dumps(event["hidden"]["msg"], ensure_ascii=False)
        lines.append(f't={tick}   {event["agentName"]} thought {msg}')
    return "\n".join(lines) if lines else "No thought"

def _decode_escaped_unicode_text(value: str) -> str:
    """文字列内のUnicodeや改行などのエスケープ表現を読みやすい文字へ戻す。

    変換できなければ元の文字列を返す。
    """
    if "\\u" not in value and "\\n" not in value and "\\t" not in value:
        return value
    try:
        return json.loads(f'"{value}"')
    except Exception:
        return value

def chat_log() -> str:
    """チャット履歴を時刻・発言者・内容の文章に整え、文字のエスケープも復元する。"""
    loader = get_loader()
    lines = []
    for tick, event in _iter_events(loader.history.events):
        if event.get("eventName") != "chat":
            continue
        msg = _decode_escaped_unicode_text(event["visible"]["msg"])
        lines.append(f't={tick}   {event["visible"]["agentName"]} said "{msg}"')
    return "\n".join(lines) if lines else "No chats"

def inventory(agent_name: Optional[str] = None) -> str:
    """対象者の非公開の所持品をプロンプト用の文章にする。

    空の所持品と、情報がない状態を区別する。
    """
    loader = get_loader()
    latest_state = loader.state_dict
    agent_name = _resolve_agent_name(agent_name)
    try:
        inv = latest_state["status"][agent_name]["hidden"]["inventory"]
        return str(inv) if inv else "Empty"
    except Exception:
        return "No data"

def equipment(agent_name: Optional[str] = None) -> str:
    """対象者の公開装備をプロンプト用の文章にする。

    装備が空の場合や情報がない場合も示す。
    """
    loader = get_loader()
    latest_state = loader.state_dict
    agent_name = _resolve_agent_name(agent_name)
    try:
        eq = latest_state["status"][agent_name]["visible"]["equipment"]
        return str(eq) if eq else "Empty"
    except Exception:
        return "No data"

def helditem(agent_name: Optional[str] = None) -> str:
    """対象者の装備情報から手持ちアイテムを取り出し、プロンプト用の文字列を返す。"""
    loader = get_loader()
    latest_state = loader.state_dict
    agent_name = _resolve_agent_name(agent_name)
    try:
        return str(latest_state["status"][agent_name]["visible"]["equipment"][4])
    except Exception:
        return "No data"

def _get_last_seen(status: dict) -> dict | None:
    """対象者の最後の目撃情報を取得する。

    記録がなければNoneを返す。
    """
    last_seen = status.get("last_seen")
    if not isinstance(last_seen, dict):
        return None
    return last_seen

def _get_last_seen_position(status: dict):
    """最後に目撃した位置をプロンプト用の文字列にし、記録がなければデータなしと示す。"""
    try:
        return status["last_seen"]["visible"]["position"]["__Vec3__"]
    except Exception:
        return "No data"

def _get_last_seen_helditem(status: dict):
    """最後に目撃した装備から手持ちアイテムを取り出す。

    記録がなければデータなしと示す。
    """
    try:
        return str(status["last_seen"]["visible"]["equipment"][4])
    except Exception:
        return "No data"

def chests() -> str:
    """記憶しているチェストの座標と中身を、プロンプト用の一覧にする。"""
    loader = get_loader()
    latest_state = loader.state_dict
    lines = []
    for row in latest_state["containers"]["__Vec3Map__"]:
        pos = tuple(row["position"])
        items = {key: value for key, value in row.items() if key != "position"}
        lines.append(f"{pos}: {items}")
    return "\n".join(lines) if lines else "No chest data"

def other_players(agent_name: Optional[str] = None) -> str: # del
    """自分と除外対象以外の人物について、現在の状態や最後の目撃情報をまとめたJSON文字列を返す。"""
    loader = get_loader()
    latest_state = loader.state_dict
    main_agent_name = _resolve_agent_name(agent_name)
    info = {}
    for agent_name in latest_state["status"]:
        if agent_name == main_agent_name:
            continue
        if any(agent_name.startswith(prefix) for prefix in EXCLUDE_NAMES): # Admin等関係のないエージェント情報を排除するため
            continue
        status = latest_state["status"][agent_name]
        last_seen = _get_last_seen(status)
        info[agent_name] = {
            "position": position(agent_name=agent_name),
            "helditem": helditem(agent_name=agent_name),
            "last_seen": (
                {
                    "tick": last_seen.get("tick"),
                    "position": _get_last_seen_position(status),
                    "helditem": _get_last_seen_helditem(status),
                }
                if last_seen is not None
                else "No data"
            ),
            "inventory": inventory(agent_name=agent_name),
        }
    return json.dumps(info, ensure_ascii=False, indent=2)


def blocks(block_names: Optional[list[str]] = None) -> str: # del
    """記憶しているブロックを種類ごとの座標一覧にまとめる。

    指定があればブロック名で絞り込む。
    """
    loader = get_loader()
    if isinstance(block_names, str):
        block_names = [block_names]
    selected_names = set(block_names) if block_names is not None else None
    positions_by_name = defaultdict(list)
    for position, block in loader.iter_blocks():
        name = block["name"]
        if selected_names is None or name in selected_names:
            positions_by_name[name].append(tuple(position))
    if block_names is None:
        block_names = sorted(positions_by_name)

    sections = []
    for name in block_names:
        positions = positions_by_name.get(name)
        body = "\n".join(map(str, positions)) if positions else "Not observed"
        sections.append(f"{name}:\n{body}")
    return "\n".join(sections)


def block_property(block_name: str) -> str:
    """指定した種類のブロックについて、座標と属性をまとめたJSON文字列を返す。"""
    loader = get_loader()
    lines = []
    for position, block in loader.iter_blocks():
        if block["name"] != block_name:
            continue
        props = block.get("properties", {})
        lines.append(f'{list(position)} : {json.dumps(props, ensure_ascii=False)}')
    return "\n".join(lines) if lines else "No data"


def _event_to_description(event: dict) -> str:
    """出来事の種類に応じて、行動や対象物などを説明する文章へ変換する。"""
    event_name = event.get("eventName")
    if event_name == "depositItemIntoChest":
        chest_pos = event["visible"]["chestPos"]["__Vec3__"]
        return f'chest:{tuple(chest_pos)} & items:{event["visible"]["depositedItems"]}'
    if event_name == "getItemFromChest":
        chest_pos = event["visible"]["chestPos"]["__Vec3__"]
        return f'chest:{tuple(chest_pos)} & items:{event["visible"]["gotItems"]}'
    if event_name == "chat":
        return f'said "{event["visible"]["msg"]}"'
    if event_name == "moveStart":
        start_pos = event["visible"]["startPos"]["__Vec3__"]
        goal_pos = event["visible"]["goalPos"]["__Vec3__"]
        return f"From {tuple(start_pos)} To {tuple(goal_pos)}"
    if event_name == "moveEnd":
        start_pos = event["visible"]["startPos"]["__Vec3__"]
        goal_pos = event["visible"]["goalPos"]["__Vec3__"]
        return f"From {tuple(start_pos)} To {tuple(goal_pos)}"
    if event_name == "craftItem":
        return f'Crafted {event["visible"]["producedCount"]} {event["visible"]["itemName"]}(s)'
    if event_name == "mineBlock":
        pos = event["visible"]["pos"]["__Vec3__"]
        return f'Mined 1 {event["visible"]["blockName"]} at {tuple(pos)}'
    if event_name == "smeltItem":
        return (
            f'Smelted {event["visible"]["materialName"]} into '
            f'{event["visible"]["producedCount"]} {event["visible"]["producedItemName"]}(s)'
        )
    if event_name == "think":
        return f'thought that "{event["hidden"]["msg"]}"'
    if event_name == "useLever":
        lever_pos = event["visible"]["leverPos"]["__Vec3__"]
        return f'{event["visible"]["type"]} the lever at {tuple(lever_pos)}'
    if event_name == "giveItemToOther":
        return (
            f'gave {event["visible"]["count"]} {event["visible"]["itemName"]} '
            f'to {event["visible"]["otherAgentName"]}'
        )
    if event_name == "receiveItemFromOther":
        return (
            f'received {event["visible"]["count"]} {event["visible"]["itemName"]} '
            f'from {event["visible"]["otherAgentName"]}'
        )
    visible = event.get("visible")
    if visible is not None:
        return json.dumps(visible, ensure_ascii=False)
    return ""

def events() -> str:
    """思考などの除外対象を省き、出来事を時刻・行動・主体・説明の一覧にしてプロンプトへ渡す。"""
    loader = get_loader()
    lines = ["time;action;agent_name;description"]
    for tick, event in _iter_events(loader.history.events):
        event_name = event.get("eventName")
        if event_name in ["think", "blockUpdate", "useChest", "swingHand"]:
            continue
        if event_name == "chat":
            agent_name = event["visible"]["agentName"]
        else:
            agent_name = event.get("agentName")
        if any(agent_name.startswith(prefix) for prefix in EXCLUDE_NAMES): # Admin等関係のないエージェント情報を排除するため
            continue
        lines.append(f"{tick};{event_name};{agent_name};{_event_to_description(event)}")
    return "\n".join(lines)

def latest_state_json() -> str:
    """最新のワールド状態全体をJSON文字列として返す。"""
    loader = get_loader()
    latest_state, _ = loader.get_latest_state()
    return json.dumps(latest_state, ensure_ascii=False, indent=2)

def latest_history_json() -> str:
    """最新の観測履歴1件をJSON文字列として返す。"""
    loader = get_loader()
    history, _ = loader.get_latest_history()
    return json.dumps(history, ensure_ascii=False, indent=2)

def blocks_and_visibilities(
    block_names: Optional[list[str]] = None,
    blacklist: Optional[list[str]] = None,
) -> str:
    """記憶しているブロックの位置を種類ごとにまとめる。

    ブロック名が指定されていない場合は除外リストを使う。
    ここで可視性の再計算は行わない。
    """
    loader = get_loader()
    if isinstance(block_names, str):
        block_names = [block_names]

    if isinstance(blacklist, str):
        blacklist = [blacklist]

    blacklist_set = set(blacklist or [])
    selected_names = set(block_names) if block_names is not None else None
    positions_by_name = defaultdict(list)

    for position, block in loader.iter_blocks():
        name = block["name"]
        if selected_names is not None and name not in selected_names:
            continue
        if block_names is None and name in blacklist_set:
            continue
        positions_by_name[name].append(str(tuple(position)))

    if block_names is None:
        target_names = sorted(positions_by_name.keys())
    else:
        target_names = block_names

    sections = []
    for name in target_names:
        positions = positions_by_name.get(name)
        if positions:
            sections.append(f"{name}: {json.dumps(positions, ensure_ascii=False)}")
        else:
            sections.append(f"{name}: Not observed")

    return "\n".join(sections)

def events_and_visibilities() -> str:
    """出来事に同じtickの人物位置を添え、プロンプト用の一覧にする。

    思考などの対象外イベントは除く。
    """
    loader = get_loader()
    events = loader.history.events

    lines = [
        "time;agent_name;agent_position;action;description",
    ]

    has_rows = False
    for tick in sorted(events, key=int):
        history_at_tick, _ = loader.get_history(int(tick))
        status_at_tick = history_at_tick.get("status", {})

        for event in events[tick]:
            event_name = event["eventName"]
            if event_name in ["think", "blockUpdate", "useChest", "swingHand"]:
                continue

            if event_name == "chat":
                agent_name = event["visible"]["agentName"]
            else:
                agent_name = event.get("agentName", "unknown")
            
            if any(agent_name.startswith(prefix) for prefix in EXCLUDE_NAMES): # Admin等関係のないエージェント情報を排除するため
                continue

            try:
                description = _event_to_description(event)
            except Exception:
                description = json.dumps(event, ensure_ascii=False)

            position = "No data"
            try:
                position = json.dumps(
                    status_at_tick[agent_name]["visible"]["position"]["__Vec3__"],
                    ensure_ascii=False,
                )
            except Exception:
                pass

            lines.append(
                f"{int(tick)};{agent_name};{position};{event_name};{description}"
            )
            has_rows = True

    if not has_rows:
        lines.append("No observable events")

    return "\n".join(lines)


FILTERS = [
    position,
    thought,
    chat_log,
    inventory,
    equipment,
    helditem,
    chests,
    other_players,
    blocks,
    blocks_and_visibilities,
    block_property,
    events,
    events_and_visibilities,
    latest_state_json,
    latest_history_json,
]

FILTER_DICT = {func.__name__: func for func in FILTERS}
#############################################################################

def load_from_template(
    loader: WorldObservationLoader,
    template: str,
    variables: Optional[dict] = None,
    extra_filters: Optional[list] = None,
    allow_filter_override: bool = False,
) -> str:
    """観測データを参照する関数をJinjaテンプレートへ登録し、変数を埋め込んだ文章を返す。

    未定義変数や許可されていない組み込み名の上書きはエラーにする。
    """
    variables = {} if variables is None else dict(variables)
    extra_filters = [] if extra_filters is None else list(extra_filters)

    current_loader.set(loader)

    env = Environment(
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )

    extra_filter_dict = {func.__name__: func for func in extra_filters}
    if not allow_filter_override:
        overridden_keys = set(FILTER_DICT) & set(extra_filter_dict)
        if overridden_keys:
            names = ", ".join(sorted(overridden_keys))
            raise ValueError(f"Cannot override filters without allow_filter_override=True: {names}")

    env.filters = dict(FILTER_DICT, **extra_filter_dict)
    env.globals.update(FILTER_DICT)
    env.globals.update(extra_filter_dict)
    return env.from_string(template).render(variables)


def render_belief_snapshot(snapshot: dict, template: str, variables: dict | None = None) -> str:
    """記憶のスナップショットをテンプレート用の読み出しデータに変換し、指定テンプレートからプロンプトを作る。"""
    loader = WorldObservationLoader(
        state_dict=snapshot["state_dict"], events=snapshot["events"], objective_list=snapshot["objective_list"], ticks=snapshot["ticks"],
        main_agent_name=snapshot.get("main_agent_name"), block_snapshot=snapshot.get("block_snapshot"),
    )
    return load_from_template(loader, template, variables=variables or {}, extra_filters=[], allow_filter_override=True)
