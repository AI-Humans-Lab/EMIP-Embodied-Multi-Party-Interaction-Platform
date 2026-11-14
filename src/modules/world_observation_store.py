from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from modules.belief import WorldState


@dataclass(frozen=True)
class WorldObservationRecording:
    initial_world_state: WorldState
    observations: list[dict[str, Any]]
    final_world_state: WorldState


class WorldObservationStore:
    """Stores unfiltered environment observations without owning their connection or receiving thread."""

    def __init__(self, initial_world_state: WorldState):
        """初期ワールド状態をコピーし、更新用の現在状態と空の観測履歴を用意する。"""
        self.initial_world_state = initial_world_state.copy()
        self.current_world_state = initial_world_state.copy()
        self.observations: list[dict[str, Any]] = []

    def update(self, observation: dict[str, Any]) -> None:
        """1回分の観測で現在のワールド状態を更新し、同じ観測をコピーして履歴へ追加する。"""
        current_world_state, observations = self.current_world_state, self.observations
        current_world_state.update(observation)
        observations.append(deepcopy(observation))

    def get_status(self) -> tuple[int, int]:
        """現在のワールドtickと、記録済みの観測件数を返す。"""
        return self.current_world_state.global_tick, len(self.observations)

    def snapshot(self) -> WorldObservationRecording:
        """初期状態・全観測履歴・最終状態をコピーし、保存や再生に使える一まとまりの記録として返す。"""
        initial_world_state, current_world_state, observations = self.initial_world_state, self.current_world_state, self.observations
        return WorldObservationRecording(initial_world_state.copy(), deepcopy(observations), current_world_state.copy())
