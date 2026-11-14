import bisect
import threading
from copy import deepcopy
from dataclasses import dataclass

from modules.agent_view import prepare_observation_for_belief
from modules.belief import Belief, WorldState, apply_block_updates, advance_state_one_observation


DEFAULT_CHECKPOINT_INTERVAL = 100
DEFAULT_QUERY_LIMIT = 100
MAX_QUERY_LIMIT = 1000


@dataclass
class StoredBelief:
    belief: Belief
    checkpoints: list[tuple[int, dict]]
    latest_visibility: dict[str, bool]
    latest_observation: dict | None
    pending_internal_events: list[dict]


@dataclass
class BeliefStoreState:
    initial_state: dict
    agent_states: dict[str, StoredBelief]
    lock: threading.Lock


def _normalize_limit(limit: int) -> int:
    """履歴検索の取得件数を整数へ変換し、1件以上・上限以下であることを確認する。"""
    limit = int(limit)
    if limit < 1 or limit > MAX_QUERY_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_QUERY_LIMIT}")
    return limit


def _normalize_agent(agent_name: str, observer_name: str, status: dict, visibility: dict) -> dict:
    """人物の状態を検索結果用にコピーし、非公開情報を分離して、自分・見える他者・見えない他者の区分を付ける。"""
    status = deepcopy(status)
    private_status = status.pop("hidden", None)
    if agent_name == observer_name:
        visibility_name = "self"
    elif visibility.get(agent_name):
        visibility_name = "visible"
    else:
        visibility_name = "not_visible"

    result = {
        "visibility": visibility_name,
        "status": status,
    }
    if private_status is not None:
        result["private_status"] = private_status
    return result


def _normalize_agents(observer_name: str, status: dict, visibility: dict) -> dict:
    """人物ごとの状態を、可視性と非公開情報を区別した検索結果の形式へまとめて変換する。"""
    return {
        agent_name: _normalize_agent(agent_name, observer_name, agent_status, visibility)
        for agent_name, agent_status in status.items()
    }


def _history_entry_to_frame(history_entry: dict, observer_name: str, include: set[str], agent_names: set[str] | None = None,
                            agent_fields: list[str] | None = None) -> dict:
    """観測履歴1件から指定された人物・出来事・ブロック更新を選び、人物名や項目で絞った検索結果を作る。"""
    result = {"tick": history_entry["globalTick"]}
    if "agents" in include:
        status = history_entry["status"]
        if agent_names is not None:
            status = {name: info for name, info in status.items() if name in agent_names}
        agents = _normalize_agents(observer_name, status, history_entry.get("player_visibility", {}))
        if agent_fields is not None:
            agents = {name: _select_agent_fields(info, agent_fields) for name, info in agents.items()}
        result["agents"] = agents
    if "events" in include:
        result["events"] = deepcopy(history_entry["events"])
    if "block_observations" in include:
        result["block_observations"] = deepcopy(history_entry["blocksToUpdate"])
    return result


def _add_observation_to_belief(agent_state: StoredBelief, observation: dict, checkpoint_interval: int) -> None:
    """観測を記憶と履歴へ登録し、最新の可視性を更新する。

    設定した間隔で、過去状態の復元に使うチェックポイントも保存する。
    """
    belief = agent_state.belief
    belief.add_observation(observation)
    agent_state.latest_visibility = observation.get("player_visibility", {})
    if len(belief.history.objective_list) % checkpoint_interval == 0:
        current = belief.state
        agent_state.checkpoints.append((current.global_tick, {"globalTick": current.global_tick, "status": deepcopy(current.status), "containers": current.containers.to_dict()}))


def select_state(state_dict: dict, observer_name: str, visibility: dict, *, center=None, radius=None, include=None) -> dict:
    """ワールド状態から要求された項目を取り出す。

    半径が指定された場合は、中心から各軸方向にその距離以内のブロックとコンテナへ絞る。
    """
    include_set = set(["agents", "blocks", "containers"] if include is None else include)
    unknown = include_set - {"agents", "blocks", "containers"}
    if unknown:
        raise ValueError(f"Unknown state sections: {sorted(unknown)}")
    if center is not None and len(center) != 3:
        raise ValueError("center must have three coordinates")
    if radius is not None and radius < 0:
        raise ValueError("radius must not be negative")
    result = {"tick": int(state_dict["globalTick"])}
    if "agents" in include_set:
        result["agents"] = _normalize_agents(observer_name, state_dict["status"], visibility)
    if radius is not None and center is None and include_set.intersection({"blocks", "containers"}):
        center = state_dict["status"][observer_name]["visible"]["position"]["__Vec3__"]
    for section in ("blocks", "containers"):
        if section not in include_set:
            continue
        selected = state_dict[section]
        if radius is not None:
            selected = {**selected, "__Vec3Map__": [entry for entry in selected["__Vec3Map__"]
                                                  if all(abs(coordinate - origin) <= radius for coordinate, origin in zip(entry["position"], center))]}
        result[section] = deepcopy(selected)
    return result

def reconstruct_state(checkpoint: dict, observations: list[dict], initial_blocks: dict, block_frames: list[dict]) -> dict:
    """チェックポイント、初期ブロック、ブロック更新履歴、以後の観測を組み合わせて、過去のワールド状態を復元する。"""
    state = WorldState({**checkpoint, "blocks": initial_blocks})
    for frame in block_frames:
        apply_block_updates(state.blocks, frame["blocksToUpdate"])
    for observation in observations:
        advance_state_one_observation(state, observation)
    return state.to_dict()


def _select_agent_fields(agent_info: dict, fields: list[str]) -> dict:
    """人物の検索結果から、位置・装備・最後の目撃情報・所持品など、指定された項目だけをコピーする。"""
    result = {}
    if "visibility" in fields:
        result["visibility"] = agent_info["visibility"]
    status = agent_info["status"]
    visible = status.get("visible", {})
    selected_visible = {field: deepcopy(visible[field]) for field in fields if field in ("position", "velocity", "yaw", "pitch", "equipment") and field in visible}
    selected_status = {}
    if selected_visible:
        selected_status["visible"] = selected_visible
    if "last_seen" in fields and "last_seen" in status:
        selected_status["last_seen"] = deepcopy(status["last_seen"])
    if selected_status:
        result["status"] = selected_status
    private_status = agent_info.get("private_status", {})
    if "inventory" in fields and "inventory" in private_status:
        result["private_status"] = {"inventory": deepcopy(private_status["inventory"])}
    return result


class BeliefStore:
    """Stores one observer's own belief and its inferred beliefs about others."""

    def __init__(
        self,
        *,
        initial_state: dict,
        checkpoint_interval: int = DEFAULT_CHECKPOINT_INTERVAL,
        agent_names: list[str] | None = None,
        initial_belief_states: dict[str, dict] | None = None,
    ):
        """初期状態を保存し、エージェントごとの記憶・チェックポイント・更新ロックを管理する保管先を作る。"""
        if checkpoint_interval < 1:
            raise ValueError("checkpoint_interval must be positive")

        state = BeliefStoreState(
            initial_state=deepcopy(initial_state),
            agent_states={},
            lock=threading.Lock(),
        )

        self.checkpoint_interval = checkpoint_interval
        self.state = state
        self.add_agents(agent_names or [], initial_belief_states=initial_belief_states)

    def add_agents(self, agent_names: list[str], initial_belief_states: dict[str, dict] | None = None) -> dict[str, dict]:
        """指定したエージェントの記憶を初期状態から作り、登録した初期状態を返す。

        重複名や対象外の初期記憶指定はエラーにする。
        """
        state = self.state
        initial_belief_states = {} if initial_belief_states is None else initial_belief_states

        with state.lock:
            if len(agent_names) != len(set(agent_names)):
                raise ValueError("Agent names must be unique")
            registered_agents = set(state.agent_states)
            duplicate_agents = registered_agents.intersection(agent_names)
            if duplicate_agents:
                names = ", ".join(sorted(duplicate_agents))
                raise ValueError(f"Agents are already registered: {names}")
            unknown_initial_beliefs = set(initial_belief_states) - set(agent_names)
            if unknown_initial_beliefs:
                raise KeyError(f"Initial Belief contains unknown agents: {sorted(unknown_initial_beliefs)}")

            initial_states = {}
            for agent_name in agent_names:
                if agent_name in initial_belief_states:
                    initial_state = deepcopy(initial_belief_states[agent_name])
                else:
                    initial_state = deepcopy(state.initial_state)
                initial_tick = int(initial_state["globalTick"])
                state.agent_states[agent_name] = StoredBelief(
                    belief=Belief(
                        initial_state=WorldState(initial_state),
                        main_agent_name=agent_name,
                    ),
                    checkpoints=[(initial_tick, deepcopy(initial_state))],
                    latest_visibility={},
                    latest_observation=None,
                    pending_internal_events=[],
                )
                initial_states[agent_name] = initial_state
        return initial_states

    def add_agent(self, agent_name: str) -> dict:
        """1人分の記憶を登録し、その初期状態を返す。"""
        return self.add_agents([agent_name])[agent_name]

    def has_agent(self, agent_name: str) -> bool:
        """指定エージェントの記憶が登録されているかを確認する。"""
        state = self.state

        with state.lock:
            return agent_name in state.agent_states

    def remove_agent(self, agent_name: str):
        """指定エージェントの記憶を管理対象から削除する。

        未登録の名前はKeyErrorにする。
        """
        state = self.state

        with state.lock:
            if state.agent_states.pop(agent_name, None) is None:
                raise KeyError(f"Agent '{agent_name}' is not registered")

    def get_current_state(self, agent_name: str, *, center: list[float] | None = None, radius: float | None = None, include: list[str] | None = None) -> dict:
        """指定エージェントが現在知っている状態をコピーして返す。

        項目や空間範囲で絞り込むこともできる。
        """
        store_state = self.state
        with store_state.lock:
            belief = self._get_agent_state(store_state, agent_name).belief
            state, history = belief.state, belief.history
            frames = history.objective_list
            state_dict = state.to_dict()
            visibility = deepcopy(frames[-1].get("player_visibility", {})) if frames else {}
        return select_state(state_dict, agent_name, visibility, center=center, radius=radius, include=include)

    def get_belief_snapshot(self, agent_name: str) -> dict:
        """指定エージェントの初期状態・現在状態・観測履歴・出来事を、独立したスナップショットとして取得する。"""
        state = self.state

        with state.lock:
            agent_state = state.agent_states.get(agent_name)
            if agent_state is None:
                raise KeyError(f"Agent '{agent_name}' is not registered")
            belief = agent_state.belief
            state, history = belief.state, belief.history
            
            objective_list = deepcopy(history.objective_list)
            ticks = history.ticks
            state_dict = state.to_dict()
            return {
                "initial_state": belief.initial_state.to_dict(),
                "state_dict": state_dict,
                "events": history.events_until(),
                "objective_list": objective_list,
                "ticks": ticks,
                "main_agent_name": belief.main_agent_name,
            }

    def get_prompt_snapshot(self, agent_name: str, *, tick: int | None = None) -> dict:
        """現在または指定tickの状態と、プロンプトに必要な履歴を取得する。

        全履歴の複製を避け、出来事のある観測と最新観測を使う。
        """
        state = self.state

        with state.lock:
            agent_state = self._get_agent_state(state, agent_name)
            belief = agent_state.belief
            current_state, history = belief.state, belief.history
            if tick is not None:
                tick = int(tick)
                if tick > current_state.global_tick:
                    raise ValueError(f"tick {tick} is later than the latest observation ({current_state.global_tick})")
                checkpoints = agent_state.checkpoints
                index = bisect.bisect_right([saved_tick for saved_tick, _ in checkpoints], tick) - 1
                if index < 0:
                    raise ValueError(f"tick {tick} is earlier than the initial state")
                if tick < current_state.global_tick:
                    checkpoint_tick, checkpoint = checkpoints[index]
                    block_frames = history.get_block_updates(to_tick=checkpoint_tick)
                    frames = history.get_range(from_tick=checkpoint_tick + 1, to_tick=tick)
                    current_state = WorldState(reconstruct_state(checkpoint, frames, belief.initial_state.blocks.to_dict(), block_frames))
            # Block updates replace value dictionaries, so existing snapshot references remain stable.
            block_snapshot = dict(current_state.blocks.data)
            events = history.events_until(current_state.global_tick)
            event_ticks = {int(tick) for tick in events}
            latest_observation = next((observation for observation in reversed(history.objective_list) if observation["globalTick"] <= current_state.global_tick), None)
            selected_observations = [deepcopy(observation) for observation in history.objective_list if observation["globalTick"] in event_ticks or observation is latest_observation]
            return {
                "state_dict": {
                    "globalTick": current_state.global_tick,
                    "status": deepcopy(current_state.status),
                    "blocks": None,
                    "containers": current_state.containers.to_dict(),
                },
                "events": events,
                "objective_list": selected_observations,
                "ticks": [int(observation["globalTick"]) for observation in selected_observations],
                "main_agent_name": belief.main_agent_name,
                "block_snapshot": block_snapshot,
            }

    def add_pending_event(self, agent_name: str, event: dict) -> dict:
        """次の新しい観測と一緒に登録する出来事をコピーして予約し、予約件数を返す。"""
        state = self.state

        with state.lock:
            agent_state = state.agent_states.get(agent_name)
            if agent_state is None:
                raise KeyError(f"Agent '{agent_name}' is not registered")
            agent_state.pending_internal_events.append(deepcopy(event))
            pending_count = len(agent_state.pending_internal_events)
        return {"queued": True, "pending_count": pending_count}

    def get_observation_status(self, agent_name: str) -> dict:
        """指定エージェントの最新受信tick・現在状態のtick・保存履歴件数など、記憶更新の進み具合を返す。"""
        state = self.state

        with state.lock:
            agent_state = self._get_agent_state(state, agent_name)
            ticks = agent_state.belief.history.ticks
            return {
                "latest_received_tick": (
                    agent_state.latest_observation["globalTick"]
                    if agent_state.latest_observation is not None
                    else None
                ),
                "current_state_tick": agent_state.belief.state.global_tick,
                "saved": {
                    "count": len(ticks),
                    "oldest_tick": ticks[0] if ticks else None,
                    "latest_tick": ticks[-1] if ticks else None,
                },
            }

    def get_latest_observation(self, agent_name: str) -> dict:
        """最後に登録された観測をコピーして返す。

        まだ登録されていなければエラーにする。
        """
        state = self.state

        with state.lock:
            agent_state = self._get_agent_state(state, agent_name)
            if agent_state.latest_observation is None:
                raise ValueError("No observation has been received yet")
            return deepcopy(agent_state.latest_observation)

    def register_observation(self, agent_name: str, observation: dict) -> dict:
        """新しいtickの観測を記憶に反映し、予約済みの出来事も同時に登録する。

        登録済みtickは重複として扱い、未登録の古いtickはエラーにする。
        """
        checkpoint_interval = self.checkpoint_interval
        state = self.state
        tick = observation["globalTick"]

        with state.lock:
            agent_state = self._get_agent_state(state, agent_name)
            current_state_tick = agent_state.belief.state.global_tick
            if tick <= current_state_tick:
                if agent_state.belief.history.contains(tick):
                    return {"registered": False, "already_registered": True, "tick": tick, "current_state_tick": current_state_tick}
                raise ValueError(f"Observation tick must be later than current state tick {current_state_tick}")
            observation = prepare_observation_for_belief(observation, agent_name, agent_state.belief.state)
            if agent_state.pending_internal_events:
                observation["objective"]["events"].extend(deepcopy(agent_state.pending_internal_events))
            _add_observation_to_belief(agent_state, observation, checkpoint_interval)
            agent_state.latest_observation = observation
            agent_state.pending_internal_events.clear()
            return {"registered": True, "already_registered": False, "tick": tick, "current_state_tick": agent_state.belief.state.global_tick}

    def delete_saved_observations(self, agent_name: str, ticks: list[int]) -> dict:
        """指定tickの保存履歴を削除し、必要なら復元用チェックポイントを初期状態へ戻す。

        現在の記憶状態そのものは巻き戻さない。
        """
        state = self.state
        requested = set(ticks)

        with state.lock:
            agent_state = self._get_agent_state(state, agent_name)
            history = agent_state.belief.history
            deleted = history.remove_ticks(requested)
            if deleted:
                initial_state = agent_state.belief.initial_state.to_dict()
                agent_state.checkpoints[:] = [(initial_state["globalTick"], initial_state)]
        return {"deleted": deleted, "not_found": sorted(requested - set(deleted))}

    def get_observation_history(
        self,
        agent_name: str,
        *,
        from_tick: int | None = None,
        to_tick: int | None = None,
        include: list[str] | None = None,
        agent_names: list[str] | None = None,
        agent_fields: list[str] | None = None,
        limit: int = DEFAULT_QUERY_LIMIT,
        cursor: int | None = None,
    ) -> dict:
        """観測履歴をtick範囲・人物・項目で検索し、取得件数と続きの位置を付けて返す。"""
        state = self.state
        limit = _normalize_limit(limit)
        include_set = set(["agents", "events", "block_observations"] if include is None else include)
        allowed = {"agents", "events", "block_observations"}
        unknown = include_set - allowed
        if unknown:
            raise ValueError(f"Unknown observation sections: {sorted(unknown)}")
        if (agent_names is not None or agent_fields is not None) and "agents" not in include_set:
            raise ValueError("agent_names and agent_fields require agents in include")
        allowed_agent_fields = {"visibility", "position", "velocity", "yaw", "pitch", "equipment", "inventory", "last_seen"}
        unknown_agent_fields = set(agent_fields or []) - allowed_agent_fields
        if unknown_agent_fields:
            raise ValueError(f"Unknown agent fields: {sorted(unknown_agent_fields)}")
        selected_agent_names = set(agent_names) if agent_names is not None else None

        with state.lock:
            agent_state = state.agent_states.get(agent_name)
            if agent_state is None:
                raise KeyError(f"Agent '{agent_name}' is not registered")
            history = agent_state.belief.history
            frames, ticks = history.objective_list, history._ticks

            lower_tick = max(from_tick, cursor) if from_tick is not None and cursor is not None else cursor if cursor is not None else from_tick
            start = 0 if lower_tick is None else bisect.bisect_left(ticks, int(lower_tick))
            end = len(ticks) if to_tick is None else bisect.bisect_right(ticks, int(to_tick))
            if from_tick is not None and to_tick is not None and from_tick > to_tick:
                raise ValueError("from_tick must be less than or equal to to_tick")
            selected = deepcopy(frames[start:min(start + limit, end)])
            next_index = start + len(selected)
            next_cursor = ticks[next_index] if next_index < end else None

        observations = [_history_entry_to_frame(entry, agent_name, include_set, selected_agent_names, agent_fields) for entry in selected]
        return {"observations": observations, "next_cursor": next_cursor}

    def get_state_at(self, agent_name: str, tick: int, *, center: list[float] | None = None, radius: float | None = None, include: list[str] | None = None) -> dict:
        """指定tick時点の記憶を履歴から復元し、人物・項目・空間範囲で絞って返す。

        初期状態より前や現在より後の指定は拒否する。
        """
        store_state = self.state
        tick = int(tick)
        with store_state.lock:
            agent_state = self._get_agent_state(store_state, agent_name)
            belief, checkpoints = agent_state.belief, agent_state.checkpoints
            state, initial, history = belief.state, belief.initial_state.to_dict(), belief.history
            checkpoint_ticks = [saved_tick for saved_tick, _ in checkpoints]
            if tick > state.global_tick:
                raise ValueError(f"tick {tick} is later than the latest observation ({state.global_tick})")
            index = bisect.bisect_right(checkpoint_ticks, tick) - 1
            if index < 0:
                raise ValueError(f"tick {tick} is earlier than the initial state")
            checkpoint = deepcopy(checkpoints[index][1])
            initial_blocks = deepcopy(initial["blocks"])
            block_frames = history.get_block_updates(to_tick=checkpoint_ticks[index])
            frames = history.get_range(from_tick=checkpoint_ticks[index] + 1, to_tick=tick)
            latest = history.at_or_before(tick)
            visibility = latest.get("player_visibility", {}) if latest is not None else {}
        result = select_state(reconstruct_state(checkpoint, frames, initial_blocks, block_frames), agent_name, visibility, center=center, radius=radius, include=include)
        result["requested_tick"] = tick
        return result

    def get_events(
        self,
        agent_name: str,
        *,
        from_tick: int | None = None,
        to_tick: int | None = None,
        event_types: list[str] | None = None,
        actors: list[str] | None = None,
        limit: int = MAX_QUERY_LIMIT,
        cursor: int = 0,
    ) -> dict:
        """出来事をtick範囲・種類・主体で絞り込み、件数制限と次ページ用の位置を付けて返す。"""
        state = self.state
        if from_tick is not None and to_tick is not None and from_tick > to_tick:
            raise ValueError("from_tick must be less than or equal to to_tick")
        limit = _normalize_limit(limit)
        event_type_set = set(event_types) if event_types is not None else None
        actor_set = set(actors) if actors is not None else None

        with state.lock:
            agent_state = state.agent_states.get(agent_name)
            if agent_state is None:
                raise KeyError(f"Agent '{agent_name}' is not registered")
            history = agent_state.belief.history
            observations, ticks = history.objective_list, history._ticks
            start = 0 if from_tick is None else bisect.bisect_left(ticks, int(from_tick))
            end = len(ticks) if to_tick is None else bisect.bisect_right(ticks, int(to_tick))
            selected_observations = deepcopy(observations[start:end])

        events = []
        for observation in selected_observations:
            for event in observation["events"]:
                event_type = event.get("eventName")
                actor = event.get("agentName") or event.get("visible", {}).get("agentName")
                if event_type_set is not None and event_type not in event_type_set:
                    continue
                if actor_set is not None and actor not in actor_set:
                    continue
                events.append({"tick": int(observation["globalTick"]), "event": event})

        cursor = int(cursor)
        if cursor < 0:
            raise ValueError("cursor must not be negative")
        selected_events = events[cursor:cursor + limit]
        next_cursor = cursor + len(selected_events) if cursor + len(selected_events) < len(events) else None
        return {"events": selected_events, "next_cursor": next_cursor}

    def get_belief(self, agent_name: str) -> Belief:
        """指定エージェントのBeliefオブジェクトを返す。

        コピーではなく、保管中の記憶そのものを返す。
        """
        state = self.state
        with state.lock:
            return self._get_agent_state(state, agent_name).belief

    def get_known_agent_names(self, agent_name: str) -> list[str]:
        """指定エージェントの現在の記憶に登場する人物名を返す。"""
        state = self.state
        with state.lock:
            return list(self._get_agent_state(state, agent_name).belief.state.status)

    def get_recorded_observations(self, agent_name: str, *, after_tick: int | None = None) -> dict:
        """指定tickより後の記録済み観測をコピーして返す。

        開始tickを省略した場合は初期状態も含め、他者の記憶推定などに使う。
        """
        store_state = self.state
        with store_state.lock:
            belief = self._get_agent_state(store_state, agent_name).belief
            initial, state, history = belief.initial_state, belief.state, belief.history
            frames = history.get_range(from_tick=None if after_tick is None else after_tick + 1)
            observations = []
            for frame in frames:
                observation = {"globalTick": frame.pop("globalTick"), "player_visibility": frame.pop("player_visibility")}
                if "block_visibility" in frame:
                    observation["block_visibility"] = frame.pop("block_visibility")
                observation["objective"] = frame
                observations.append(observation)
            result = {"tick": state.global_tick, "observations": observations}
            if after_tick is None:
                result["initial_state"] = initial.to_dict()
            return result

    @staticmethod
    def _get_agent_state(state: BeliefStoreState, agent_name: str) -> StoredBelief:
        """保管先から指定エージェントの記憶管理データを取得する。

        未登録ならKeyErrorにする。
        """
        agent_state = state.agent_states.get(agent_name)
        if agent_state is None:
            raise KeyError(f"Agent '{agent_name}' is not registered")
        return agent_state


