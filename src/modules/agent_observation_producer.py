from __future__ import annotations

import threading
import time
from queue import Empty, Queue
from copy import deepcopy
from dataclasses import dataclass, field

from modules.agent_view import build_agent_observation
from modules.belief import Vec3Map, WorldState
from modules.visibility import BlockSpatialIndex, Vec3BoolMap


@dataclass(frozen=True)
class AgentObservationProducerConfig:
    agent_name: str
    env_box: list[list[int]]
    should_enqueue_observations: bool
    visibility_distance: float


@dataclass
class WorldReceptionState:
    world_state: WorldState | None = None
    block_index: BlockSpatialIndex | None = None
    initial_tick: int | None = None
    reserved_agent_names: list[str] = field(default_factory=list)
    receiver_error: Exception | None = None


@dataclass
class ObservationProductionState:
    reported_blocks: Vec3Map | None = None
    previous_block_visibility: Vec3BoolMap | None = None
    latest_observation: dict | None = None

class AgentObservationProducer:
    """Track Minecraft world state, build agent-visible observations, and optionally queue them."""

    def __init__(self, agent_name: str, env_box: list[list[int]], should_enqueue_observations: bool = False, visibility_distance: float = 20.0):
        """エージェント名・観測範囲・視認距離を保存し、観測結果を保持する状態とキューを用意する。"""
        self.config = AgentObservationProducerConfig(agent_name, env_box, should_enqueue_observations, visibility_distance)
        self.world_reception = WorldReceptionState()
        self.observation_production = ObservationProductionState()
        self.observation_queue: Queue[dict] = Queue()
        self.lock = threading.Lock()

    def initialize(self, initial_state: dict, reserved_agent_names: list[str]) -> None:
        """初期ワールド状態と観測対象から除く予約名を登録し、ブロック検索用の索引を作る。

        二重初期化はエラーにする。
        """
        world_reception, observation_production, lock = self.world_reception, self.observation_production, self.lock
        with lock:
            if world_reception.world_state is not None:
                raise RuntimeError("Agent observation producer is already initialized")
            world_reception.initial_tick = int(initial_state["globalTick"])
            world_reception.world_state = WorldState(initial_state)
            observation_production.reported_blocks = Vec3Map([])
            world_reception.block_index = BlockSpatialIndex(world_reception.world_state.blocks)
            world_reception.reserved_agent_names = list(reserved_agent_names)

    def process_world_observation(self, world_observation: dict) -> float:
        """ワールドの観測を取り込み、このエージェントから見える人物・出来事・ブロック更新を作る。

        最新観測を保存し、設定に応じてキューへ追加して、視点変換の所要秒数を返す。
        """
        config, world_reception, observation_production, observation_queue, lock = (
            self.config, self.world_reception, self.observation_production, self.observation_queue, self.lock
        )
        with lock:
            world_state, reported_blocks = world_reception.world_state, observation_production.reported_blocks
            if world_state is None or reported_blocks is None:
                raise RuntimeError("Agent observation producer is not initialized")
            world_state.update(world_observation)
            for block in world_observation["objective"]["blocksToUpdate"]:
                world_reception.block_index.update(block)
            if config.agent_name not in world_state.status:
                return 0.0
            perspective_started_at = time.perf_counter()
            observation, block_visibility = build_agent_observation(
                world_observation, config.agent_name, world_state, reported_blocks, config.env_box,
                visibility_distance=config.visibility_distance, block_index=world_reception.block_index,
                previous_block_visibility=observation_production.previous_block_visibility,
            )
            perspective_seconds = time.perf_counter() - perspective_started_at
            for block in observation["objective"]["blocksToUpdate"]:
                value = {key: deepcopy(block[key]) for key in ("name", "stateId", "properties") if key in block and block[key] is not None}
                reported_blocks.set(block["position"]["__Vec3__"], value)
            observation_production.previous_block_visibility = block_visibility
            observation_production.latest_observation = observation
            if config.should_enqueue_observations:
                observation_queue.put(observation)
            return perspective_seconds

    def get_status(self) -> dict:
        """受信済みの最新tick、初期tick、予約名、未取得の観測件数を返す。

        受信側で発生したエラーも呼び出し元へ伝える。
        """
        world_reception, observation_production, observation_queue, lock = (
            self.world_reception, self.observation_production, self.observation_queue, self.lock
        )
        with lock:
            if world_reception.receiver_error is not None:
                raise world_reception.receiver_error
            return {
                "latest_received_tick": observation_production.latest_observation["globalTick"] if observation_production.latest_observation is not None else None,
                "initial_tick": world_reception.initial_tick,
                "reserved_agent_names": list(world_reception.reserved_agent_names),
                "pending_observations": observation_queue.qsize(),
            }

    def get_latest_observation(self) -> dict:
        """最後に作成したエージェント視点の観測をコピーして返す。

        まだ観測がない場合や受信が失敗した場合は例外を返す。
        """
        world_reception, observation_production, lock = self.world_reception, self.observation_production, self.lock
        with lock:
            if world_reception.receiver_error is not None:
                raise world_reception.receiver_error
            if observation_production.latest_observation is None:
                raise ValueError("No observation has been received yet")
            return deepcopy(observation_production.latest_observation)

    def take_queued_observations(self, timeout_sec: float) -> dict:
        """キュー内の観測を到着順にまとめて取り出す。

        空なら指定秒数まで待ち、時間切れでは空の観測一覧を返す。
        キューを有効にした場合だけ利用できる。
        """
        config, world_reception, observation_queue, lock = self.config, self.world_reception, self.observation_queue, self.lock
        if not config.should_enqueue_observations:
            raise RuntimeError("Queuing agent-visible observations is not enabled")
        deadline = time.monotonic() + timeout_sec
        while True:
            with lock:
                if world_reception.receiver_error is not None:
                    raise world_reception.receiver_error
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"observations": []}
            try:
                first_observation = observation_queue.get(timeout=min(remaining, 1.0))
                break
            except Empty:
                continue
        with lock:
            if world_reception.receiver_error is not None:
                raise world_reception.receiver_error
            observations = [first_observation]
            while True:
                try:
                    observations.append(observation_queue.get_nowait())
                except Empty:
                    break
        return {"observations": deepcopy(observations)}

    def set_receiver_error(self, error: Exception) -> None:
        """観測の受信エラーを保存し、以後の状態・観測取得時に呼び出し元へ通知できるようにする。"""
        world_reception, lock = self.world_reception, self.lock
        with lock:
            world_reception.receiver_error = error
