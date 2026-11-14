"""Continuing belief inference from one observer's recorded observations."""

import threading
from dataclasses import dataclass

from modules.agent_view import infer_other_observation
from modules.belief import Belief, WorldState
from modules.belief_store import BeliefStore
from modules.visibility import build_block_arrays


@dataclass(frozen=True)
class BeliefInferenceConfig:
    env_box: list[list[int]]
    visibility_distance: float


@dataclass
class BeliefInferenceState:
    """State retained for one target"""

    observer_state: WorldState
    previous_block_visibility: dict | None = None
    block_arrays: tuple | None = None


class BeliefInference:
    """Continuously estimate other agents' beliefs from one observer's belief."""

    def __init__(self, store: BeliefStore, observer_name: str, env_box: list[list[int]], visibility_distance: float = 20.0):
        """観測者の記憶、観測範囲、視認距離を保持し、対象者ごとの推定状態と同時更新を防ぐロックを用意する。"""
        self.store = store
        self.observer_name = observer_name
        self.config = BeliefInferenceConfig(env_box, visibility_distance)
        self.states_by_target: dict[str, BeliefInferenceState] = {}
        self.lock = threading.Lock()

    def infer_other_belief(self, target_name: str) -> Belief:
        """前回以降の観測者の履歴を使って、対象者が知っているはずの内容を順に推定・登録する。

        失敗時は最後に登録できた時点まで推定用の観測者状態を戻す。
        """
        store, observer_name, env_box, visibility_distance, states_by_target, lock = (
            self.store, self.observer_name, self.config.env_box, self.config.visibility_distance, self.states_by_target, self.lock
        )
        if not isinstance(target_name, str) or not target_name or target_name == observer_name:
            raise ValueError("target_name must identify another person")
        with lock:
            # 1. Continue from the saved inference state; on the first call, start with an empty target belief.
            inference_state = states_by_target.get(target_name)
            after_tick = None if inference_state is None else store.get_observation_status(target_name)["current_state_tick"]
            data = store.get_recorded_observations(observer_name, after_tick=after_tick)
            if inference_state is None:
                initial = data["initial_state"]
                empty = {"globalTick": initial["globalTick"], "status": {}, "blocks": {"__Vec3Map__": []}, "containers": {"__Vec3Map__": []}}
                store.add_agents([target_name], initial_belief_states={target_name: empty})
                inference_state = BeliefInferenceState(WorldState(initial))
                states_by_target[target_name] = inference_state
            observer_state = inference_state.observer_state
            try:
                # 2. Process each observation: update the observer state, estimate the target's observation, and register it in the target belief.
                for observation in data["observations"]:
                    observer_state.update(observation)
                    if observation["objective"]["blocksToUpdate"]:
                        inference_state.block_arrays = None
                    if inference_state.block_arrays is None and observation["player_visibility"].get(target_name, False):
                        inference_state.block_arrays = build_block_arrays(observer_state.blocks, ())
                    inferred_observation = infer_other_observation(observer_state, observation, target_name, env_box, visibility_distance,
                                                                  previous_inferred_block_visibility=inference_state.previous_block_visibility,
                                                                  block_arrays=inference_state.block_arrays)
                    store.register_observation(target_name, inferred_observation)
                    inference_state.previous_block_visibility = inferred_observation["block_visibility"]
            except Exception:
                # On failure, restore the observer state to the last observation successfully registered in the target belief.
                inference_state.observer_state = _restore_observer_state(store, observer_name, store.get_observation_status(target_name)["current_state_tick"])
                inference_state.block_arrays = None
                raise
            # 3. Save the inference state and return the same target belief.
            states_by_target[target_name] = inference_state
            return store.get_belief(target_name)


def _restore_observer_state(store: BeliefStore, observer_name: str, through_tick: int) -> WorldState:
    """初期状態に指定tickまでの観測を適用し、推定に使う観測者の状態を復元する。"""
    data = store.get_recorded_observations(observer_name)
    observer_state = WorldState(data["initial_state"])
    for observation in data["observations"]:
        if observation["globalTick"] > through_tick:
            break
        observer_state.update(observation)
    return observer_state
