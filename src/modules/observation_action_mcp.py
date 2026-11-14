from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp.server import MCPServer

from modules.config_dataclass import (
    ObservationActionConnectionConfig,
    MinecraftWorldConfig,
)
from modules.mineflayerconnection import (
    AgentMineflayerConnection,
    build_mineflayer_variables,
)
from modules.minecraft_observation import MinecraftObservationProcessor
from modules.agent_observation_producer import AgentObservationProducer
from modules.observation_source import (
    MinecraftObservationConnection,
    ObservationsProcessedMarker,
    ProcessingStopMarker,
)
from modules.utils import make_file_logger


OBSERVATION_RUNTIME_LOGGER = logging.getLogger("easyllm.observation.runtime")
OBSERVATION_RUNTIME_LOGGER.disabled = True
OBSERVATION_RUNTIME_LOGGER.propagate = False


@dataclass(frozen=True)
class ObservationActionConnections:
    mineflayer: AgentMineflayerConnection
    observation: MinecraftObservationConnection


async def _prepare_mineflayer_player(
    mineflayer_connection: AgentMineflayerConnection,
    agent_name: str,
    minecraft_world_config: MinecraftWorldConfig,
) -> str:
    """ワールド範囲をMineflayer用の設定へ変換し、指定名のエージェントを参加させて管理者名を返す。"""
    variables = build_mineflayer_variables(minecraft_world_config.env_box)
    return await mineflayer_connection.join(agent_name, variables)


def build_observation_action_mcp_server(
    *,
    connection_config: ObservationActionConnectionConfig,
    minecraft_world_config: MinecraftWorldConfig,
    primitives: list[str],
    log_dir: str,
    logger_name_prefix: str | None = None,
    observation_enabled: bool = True,
    should_enqueue_observations: bool = False,
) -> MCPServer:
    """設定から行動用・観測用の接続とログを用意し、1人のエージェントに対応する観測・行動MCPサーバーを作る。"""
    mineflayer_connection_cfg = connection_config.mineflayer
    observation_cfg = connection_config.observation
    env_box = minecraft_world_config.env_box
    if logger_name_prefix is None:
        logger_name_prefix = (
            f"{mineflayer_connection_cfg.host}:{mineflayer_connection_cfg.port}"
        )
    mineflayer_logger = make_file_logger(
        f"{logger_name_prefix}.mineflayer", f"{log_dir}/mineflayer_connection.log"
    )
    connections = ObservationActionConnections(
        mineflayer=AgentMineflayerConnection(
            logger=mineflayer_logger,
            host=mineflayer_connection_cfg.host,
            port=mineflayer_connection_cfg.port,
        ),
        observation=MinecraftObservationConnection(
            log_dir=log_dir,
            host=observation_cfg.host,
            port=observation_cfg.port,
            env_box=env_box,
            logger_name=f"{logger_name_prefix}.observation",
        ),
    )
    return _build_observation_action_mcp_server(
        connections=connections,
        minecraft_world_config=minecraft_world_config,
        primitives=primitives,
        observation_enabled=observation_enabled,
        should_enqueue_observations=should_enqueue_observations,
    )


def _build_observation_action_mcp_server(
    *,
    connections: ObservationActionConnections,
    minecraft_world_config: MinecraftWorldConfig,
    primitives: list[str],
    observation_enabled: bool,
    should_enqueue_observations: bool = False,
) -> MCPServer:
    """渡された接続を使い、初期化・観測取得・処理の停止再開・JavaScript実行を公開するMCPサーバーを組み立てる。"""
    mineflayer_connection = connections.mineflayer
    observation_connection = connections.observation
    observation_host = observation_connection.host
    observation_port = observation_connection.port
    env_box = minecraft_world_config.env_box
    receiver_thread: threading.Thread | None = None
    stop_event = threading.Event()
    observation_lock = threading.Lock()
    receive_gate = threading.Event()
    receive_gate.set()
    stop_lock = threading.Lock()
    processing_enabled = observation_enabled
    registered_agent_name: str | None = None
    producer: AgentObservationProducer | None = None
    setup_state = "not_started"
    setup_lock = asyncio.Lock()
    lifespan_active = False
    connections_started = False

    def require_setup() -> None:
        """MCPサーバーの初期設定が正常に完了しているか確認し、未完了なら操作を拒否する。"""
        if setup_state != "ready":
            raise RuntimeError(
                "Call setup_observation_action successfully before using this server"
            )

    observation_processor: MinecraftObservationProcessor | None = None

    def receive_observations() -> None:
        """Minecraftの更新を受信し、処理有効時はエージェント視点の観測へ変換する。

        停止中は更新を破棄し、処理完了・停止用マーカーと受信エラーも扱う。
        """
        nonlocal processing_enabled

        try:
            while not stop_event.is_set():
                receive_gate.wait()
                if stop_event.is_set():
                    break
                with observation_lock:
                    if not receive_gate.is_set():
                        continue
                    if processing_enabled:
                        received = observation_connection.receive(timeout=0.1)
                        if isinstance(received, ProcessingStopMarker):
                            if not received.done():
                                processing_enabled = False
                                received.set_result(None)
                            continue
                        if isinstance(received, ObservationsProcessedMarker):
                            if not received.done():
                                received.set_result(None)
                            continue
                        if received is not None:
                            processing_started_at = time.perf_counter()
                            perspective_seconds = 0.0
                            for world_observation in observation_processor.process(
                                received
                            ):
                                perspective_seconds += (
                                    producer.process_world_observation(
                                        world_observation
                                    )
                                )
                            OBSERVATION_RUNTIME_LOGGER.info(
                                "processed",
                                extra={
                                    "observation_runtime": {
                                        "event": "processed",
                                        "agent_name": registered_agent_name,
                                        "host": observation_host,
                                        "port": observation_port,
                                        "processing_seconds": time.perf_counter()
                                        - processing_started_at,
                                        "perspective_seconds": perspective_seconds,
                                        "pending_messages": observation_connection.pending_message_count,
                                    }
                                },
                            )
                    else:
                        observation_connection.discard(timeout=0.1)
                        OBSERVATION_RUNTIME_LOGGER.info(
                            "pending",
                            extra={
                                "observation_runtime": {
                                    "event": "pending",
                                    "agent_name": registered_agent_name,
                                    "host": observation_host,
                                    "port": observation_port,
                                    "pending_messages": observation_connection.pending_message_count,
                                }
                            },
                        )
        except Exception as error:
            if not stop_event.is_set():
                producer.set_receiver_error(error)

    async def close_connections() -> None:
        """観測受信に停止を指示し、観測接続と受信スレッドを終了してMineflayer接続を閉じる。"""
        nonlocal receiver_thread, observation_processor, connections_started
        if not connections_started:
            return
        connections_started = False
        stop_event.set()
        receive_gate.set()
        try:
            try:
                await asyncio.to_thread(observation_connection.close)
            finally:
                if receiver_thread is not None:
                    await asyncio.to_thread(receiver_thread.join)
        finally:
            receiver_thread = None
            observation_processor = None
            await mineflayer_connection.close()

    @asynccontextmanager
    async def observation_lifespan(_server):
        """MCP接続中の利用期間を管理し、終了時は初期化処理と競合しないようロックして関連接続を閉じる。"""
        nonlocal lifespan_active, setup_state
        lifespan_active = True
        try:
            yield
        finally:
            # 接続中のsetupと終了処理を同時に実行しない。
            async with setup_lock:
                lifespan_active = False
                setup_state = "closed"
                await close_connections()

    mcp = MCPServer(
        "Observation-Action",
        instructions="Receives observations and executes this agent's actions in one bound Minecraft world.",
        lifespan=observation_lifespan,
    )

    # [関数説明]
    # サーバーを1人のエージェントへ結び付け、Minecraftへの参加・初期観測取得・受信スレッド開始を行う。
    # 設定は1回だけ許可し、失敗時は接続を片付ける。
    @mcp.tool()
    async def setup_observation_action(agent_name: str) -> dict[str, Any]:
        """Bind this server to one Agent and start Minecraft connections once."""
        nonlocal setup_state, producer, receiver_thread, observation_processor, connections_started, registered_agent_name
        async with setup_lock:
            if not lifespan_active:
                raise RuntimeError(
                    "Connect the MCP client before setup_observation_action"
                )
            if setup_state != "not_started":
                raise RuntimeError(
                    "setup_observation_action may only be called once per server"
                )
            if not isinstance(agent_name, str) or not agent_name.strip():
                raise ValueError("agent_name must be a non-empty string")
            setup_state = "starting"
            registered_agent_name = agent_name
            try:
                producer = AgentObservationProducer(
                    agent_name,
                    env_box,
                    should_enqueue_observations=should_enqueue_observations,
                    visibility_distance=minecraft_world_config.visibility_distance,
                )
                stop_event.clear()
                connections_started = True
                await mineflayer_connection.connect()
                admin_mc_name = await _prepare_mineflayer_player(
                    mineflayer_connection, agent_name, minecraft_world_config
                )
                initial_state = await asyncio.to_thread(observation_connection.connect)
                observation_processor = MinecraftObservationProcessor(
                    initial_tick=initial_state["globalTick"],
                    admin_mc_name=admin_mc_name,
                )
                producer.initialize(initial_state, [admin_mc_name])
                receiver_thread = threading.Thread(
                    target=receive_observations, daemon=True
                )
                receiver_thread.start()
                setup_state = "ready"
                return {"initialized": True, "agent_name": agent_name}
            except BaseException as error:
                setup_state = "failed"
                try:
                    await close_connections()
                except BaseException as cleanup_error:
                    error.add_note(f"Setup cleanup also failed: {cleanup_error}")
                raise

    # [関数説明]
    # 最新受信tickや未取得観測件数に、観測範囲・視認距離・未処理メッセージ件数を加えて返す。
    @mcp.resource("minecraft-observation://status", mime_type="application/json")
    def observation_status() -> dict[str, Any]:
        """Latest received observation and pending Minecraft messages."""
        require_setup()
        return {
            **producer.get_status(),
            "env_box": env_box,
            "visibility_distance": minecraft_world_config.visibility_distance,
            "pending_messages": observation_connection.pending_message_count,
        }

    # [関数説明]
    # 記憶への登録状況とは独立して、最後に処理したエージェント視点の観測を返す。
    @mcp.resource(
        "minecraft-observation://latest-observation", mime_type="application/json"
    )
    def latest_observation() -> dict[str, Any]:
        """Latest agent-visible observation, independent of any Belief MCP."""
        require_setup()
        return producer.get_latest_observation()

    # [関数説明]
    # 最新観測を指示文とJSONの文章へ整える。
    # 観測処理中なら先に届いた更新の処理を待ち、停止中なら最後の処理済み観測を使う。
    # 取得キューの観測は消さない。
    @mcp.tool()
    async def render_latest_observation_prompt(
        instruction: str = "Decide your next action and utterance based on the latest Minecraft observation.",
    ) -> dict[str, str]:
        """Render the latest processed observation without removing observations from take_queued_observations.

        While processing runs, wait for earlier Minecraft updates; after stop, use the last processed observation.
        """
        require_setup()
        deadline = asyncio.get_running_loop().time() + 30.0
        while producer.get_status()["latest_received_tick"] is None:
            with observation_lock:
                if not processing_enabled:
                    raise ValueError("No processed observation is available")
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    "No Minecraft observation was received within 30 seconds"
                )
            await asyncio.sleep(min(0.05, remaining))
        with observation_lock:
            marker = (
                observation_connection.register_observations_processed_marker()
                if processing_enabled
                else None
            )
        if marker is not None:
            try:
                await asyncio.wait_for(asyncio.wrap_future(marker), timeout=30.0)
            except RuntimeError as error:
                if str(error) != "Observation processing is disabled":
                    raise
        observation = producer.get_latest_observation()
        context = {"agent_name": registered_agent_name, "observation": observation}
        return {
            "prompt": instruction
            + "\n観測 (JSON。blocksToUpdate は今回の更新):\n"
            + json.dumps(context, ensure_ascii=False)
        }

    # [関数説明]
    # 未取得の観測を順番にまとめて取り出す。
    # 空なら指定秒数まで待ち、観測処理が停止中でも待機を行う。
    @mcp.tool()
    async def take_queued_observations(timeout_sec: float = 30.0) -> dict[str, Any]:
        """Return and remove all queued agent-visible observations in order; if empty, wait up to timeout_sec even while processing is stopped."""
        require_setup()
        if (
            isinstance(timeout_sec, bool)
            or not isinstance(timeout_sec, (int, float))
            or timeout_sec <= 0
        ):
            raise ValueError("timeout_sec must be positive")
        return await asyncio.to_thread(producer.take_queued_observations, timeout_sec)

    # [関数説明]
    # 観測変換を停止し、以後の受信更新を破棄する。
    # 既受信分は標準では処理してから止まり、指定時は破棄する。
    # 取得待ちの観測キューは保持する。
    @mcp.tool()
    def stop_observation_processing(
        timeout_sec: float = 30.0, discard_unprocessed_updates: bool = False
    ) -> dict[str, Any]:
        """Stop processing Minecraft updates, processing earlier updates by default or discarding them when discard_unprocessed_updates is true.

        Later updates are discarded until start_observation_processing; observations waiting for take_queued_observations remain queued.
        """
        require_setup()
        nonlocal processing_enabled
        if not isinstance(discard_unprocessed_updates, bool):
            raise ValueError("discard_unprocessed_updates must be a boolean")
        if (
            isinstance(timeout_sec, bool)
            or not isinstance(timeout_sec, (int, float))
            or timeout_sec <= 0
        ):
            raise ValueError("timeout_sec must be positive")

        if not stop_lock.acquire(blocking=False):
            raise RuntimeError("Another observation processing stop is active")
        try:
            receive_gate.clear()
            lock_started_at = time.perf_counter()
            try:
                with observation_lock:
                    lock_wait_seconds = time.perf_counter() - lock_started_at
                    processing_stop_marker = (
                        observation_connection.register_processing_stop_marker()
                    )
                    processing_enabled = not discard_unprocessed_updates
            finally:
                receive_gate.set()
            processing_started_at = time.perf_counter()
            try:
                processing_stop_marker.result(timeout=timeout_sec)
            except TimeoutError as error:
                with observation_lock:
                    if processing_stop_marker.cancel():
                        processing_enabled = False
                raise TimeoutError(
                    f"Observation processing did not stop within {timeout_sec} seconds"
                ) from error
            except BaseException:
                with observation_lock:
                    processing_enabled = False
                raise
            finally:
                OBSERVATION_RUNTIME_LOGGER.info(
                    "stop",
                    extra={
                        "observation_runtime": {
                            "event": "stop",
                            "agent_name": registered_agent_name,
                            "host": observation_host,
                            "port": observation_port,
                            "stop_pending_messages": None,
                            "stop_lock_wait_seconds": lock_wait_seconds,
                            "stop_processing_seconds": time.perf_counter()
                            - processing_started_at,
                        }
                    },
                )
        finally:
            stop_lock.release()
        return {
            "latest_received_tick": producer.get_status()["latest_received_tick"],
            "pending_messages": observation_connection.pending_message_count,
        }

    # [関数説明]
    # 受信更新の観測変換を開始または再開する。
    # 保持中の状態と取得待ち観測は初期化しない。
    @mcp.tool()
    def start_observation_processing() -> dict[str, Any]:
        """Start or resume processing Minecraft updates without resetting state or removing observations queued for take_queued_observations."""
        require_setup()
        nonlocal processing_enabled
        with stop_lock:
            with observation_lock:
                processing_enabled = True
        return {"enabled": True}

    # [関数説明]
    # このサーバーに登録されたエージェントで、指定したJavaScriptを利用可能な基本操作とともに実行する。
    @mcp.tool()
    async def exec_js(code: str) -> Any:
        """Execute JavaScript as this agent in the bound Minecraft world."""
        require_setup()
        return await mineflayer_connection.exec_js(
            registered_agent_name, code, primitives
        )

    return mcp
