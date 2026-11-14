import json
import queue
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

from modules.minecraft_observation import build_world_config_from_block_snapshot_buffer
from modules.utils import make_file_logger
from websockets.sync.client import connect as connect_websocket


def build_easy_llm_variables(env_box):
    """初期ワールド情報を要求するため、観測範囲を含むfirst_accessメッセージを作る。"""
    return {
        "type": "first_access",
        "min": {"x": env_box[0][0], "y": env_box[0][1], "z": env_box[0][2]},
        "max": {"x": env_box[1][0], "y": env_box[1][1], "z": env_box[1][2]},
    }


def _receive_initial_world_snapshot(incoming_queue, timeout: float):
    """受信キューから分割された初期ブロック情報を集め、完成した初期状態を返す。

    受信エラーや制限時間超過は呼び出し元へ伝える。
    """
    block_snapshot_buffer = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            message = incoming_queue.get(timeout=min(0.1, max(0.0, deadline - time.monotonic())))
        except queue.Empty:
            continue
        if isinstance(message, BaseException):
            raise message
        if not isinstance(message, str):
            continue
        message_from_minecraft = json.loads(message)
        snapshot_result = build_world_config_from_block_snapshot_buffer(
            message_from_minecraft,
            block_snapshot_buffer,
        )
        if snapshot_result is not None:
            return snapshot_result
    raise TimeoutError(f"Minecraft observation server did not return the initial world snapshot within {timeout} seconds")


@dataclass(frozen=True)
class _ObservationConnectionConfig:
    host: str
    port: int
    env_box: list[list[int]]
    connect_timeout: float


@dataclass
class _ObservationConnectionState:
    websocket: Any | None = None
    receiver_thread: threading.Thread | None = None


class ObservationsProcessedMarker(Future):
    """A queued marker completed after preceding observations reach the store."""


class ProcessingStopMarker(ObservationsProcessedMarker):
    """A queued marker completed after preceding observations reach the store and processing stops."""


class MinecraftObservationConnection:
    def __init__(
        self,
        *,
        log_dir: str,
        host: str,
        port: int,
        env_box: list[list[int]],
        connect_timeout: float = 30.0,
        logger_name: str | None = None,
    ):
        """観測接続のホスト・ポート・範囲・ログを保持し、受信キューと接続状態を用意する。"""
        incoming_queue = queue.Queue()
        if logger_name is None:
            logger_name = f"{host}:{port}.observation"
        logger = make_file_logger(
            logger_name,
            f"{log_dir}/MinecraftObservationConnection.log",
        )

        self.config = _ObservationConnectionConfig(host, port, env_box, connect_timeout)
        self.incoming_queue = incoming_queue
        self.logger = logger
        self.state = _ObservationConnectionState()

    @property
    def host(self) -> str:
        """観測WebSocketの接続先ホストを返す。"""
        return self.config.host

    @property
    def port(self) -> int:
        """観測WebSocketの接続先ポートを返す。"""
        return self.config.port

    @property
    def pending_message_count(self) -> int:
        """受信済みでまだ取り出されていないメッセージの件数を返す。"""
        return self.incoming_queue.qsize()

    def register_observations_processed_marker(self) -> ObservationsProcessedMarker:
        """それ以前に受信した更新の処理完了を待つためのマーカーをキューへ追加し、完了通知用のFutureを返す。"""
        state, incoming_queue = self.state, self.incoming_queue
        if state.websocket is None:
            raise RuntimeError("MinecraftObservationConnection is not connected")
        observations_processed_marker = ObservationsProcessedMarker()
        incoming_queue.put(observations_processed_marker)
        return observations_processed_marker

    def register_processing_stop_marker(self) -> ProcessingStopMarker:
        """それ以前の受信更新を処理した地点で観測処理を止めるため、停止マーカーをキューへ追加する。"""
        state, incoming_queue = self.state, self.incoming_queue
        if state.websocket is None:
            raise RuntimeError("MinecraftObservationConnection is not connected")
        processing_stop_marker = ProcessingStopMarker()
        incoming_queue.put(processing_stop_marker)
        return processing_stop_marker

    def connect(self, *, include_snapshot_info: bool = False) -> dict | tuple[dict, dict]:
        """観測WebSocketへ接続して受信スレッドを開始し、初期ワールド情報を要求・取得する。

        失敗時は接続を閉じる。
        """
        config = self.config
        incoming_queue = self.incoming_queue
        logger = self.logger
        state = self.state

        if state.websocket is not None:
            raise RuntimeError("MinecraftObservationConnection is already connected")
        if config.connect_timeout <= 0:
            raise ValueError("connect_timeout must be positive")

        websocket = self._connect_websocket()
        state.websocket = websocket
        receiver_thread = threading.Thread(target=self._receive_messages, args=(websocket,), daemon=True)
        state.receiver_thread = receiver_thread
        receiver_thread.start()
        try:
            websocket.send(json.dumps(build_easy_llm_variables(config.env_box)))
            world_config, snapshot_info = _receive_initial_world_snapshot(incoming_queue, config.connect_timeout)
        except BaseException:
            self.close()
            raise

        initial_state = world_config["state"]

        logger.info(
            "block_snapshot complete: requestId=%s sequences=%s blocks=%s/%s chests=%s",
            snapshot_info["request_id"],
            snapshot_info["sequences"],
            snapshot_info["actual_count"],
            snapshot_info["expected_count"],
            snapshot_info["chest_count"],
        )
        if include_snapshot_info:
            return initial_state, snapshot_info
        return initial_state

    def receive(self, timeout: float = 0.1) -> dict | ObservationsProcessedMarker | None:
        """受信キューから1件を取り出し、JSONメッセージまたは処理用マーカーを返す。

        時間内に届かなければNone、受信エラーなら例外を返す。
        """
        incoming_queue = self.incoming_queue

        try:
            message = incoming_queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if isinstance(message, BaseException):
            raise message
        if isinstance(message, ObservationsProcessedMarker):
            return message
        if not isinstance(message, str):
            return None
        return json.loads(message)

    def discard(self, timeout: float = 0.1) -> None:
        """受信キューの1件を破棄する。

        受信エラーは伝達し、停止マーカーは完了に、通常の処理完了マーカーは処理停止のエラーにする。
        """
        incoming_queue = self.incoming_queue
        try:
            message = incoming_queue.get(timeout=timeout)
        except queue.Empty:
            return
        if isinstance(message, BaseException):
            raise message
        if isinstance(message, ProcessingStopMarker) and not message.done():
            message.set_result(None)
        elif isinstance(message, ObservationsProcessedMarker) and not message.done():
            message.set_exception(RuntimeError("Observation processing is disabled"))

    def close(self) -> None:
        """観測接続と受信スレッドを終了し、キューを空にする。

        待機中のマーカーには接続終了を通知する。
        """
        incoming_queue = self.incoming_queue
        state = self.state
        websocket = state.websocket
        receiver_thread = state.receiver_thread

        state.websocket = None
        if websocket is not None:
            websocket.close()
        if receiver_thread is not None and receiver_thread is not threading.current_thread():
            receiver_thread.join()
        state.receiver_thread = None
        while not incoming_queue.empty():
            try:
                message = incoming_queue.get_nowait()
                if isinstance(message, ObservationsProcessedMarker) and not message.done():
                    message.set_exception(RuntimeError("Minecraft observation connection closed"))
            except queue.Empty:
                break

    def _connect_websocket(self):
        """接続期限までWebSocket接続を再試行し、接続できなければタイムアウトにする。"""
        config = self.config
        uri = f"ws://{config.host}:{config.port}"
        deadline = time.monotonic() + config.connect_timeout
        last_error = None
        while time.monotonic() < deadline:
            try:
                return connect_websocket(uri, open_timeout=min(1.0, max(0.1, deadline - time.monotonic())), max_size=None)
            except Exception as error:
                last_error = error
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        error_message = f"Minecraft observation server did not accept a WebSocket connection at {uri} within {config.connect_timeout} seconds"
        raise TimeoutError(error_message) from last_error

    def _receive_messages(self, websocket) -> None:
        """WebSocketのメッセージを順番に受信キューへ渡す。

        利用中の接続が切れた場合は接続エラーもキューへ渡す。
        """
        incoming_queue = self.incoming_queue
        state = self.state
        try:
            for message in websocket:
                incoming_queue.put(message)
        except Exception as error:
            if state.websocket is websocket:
                incoming_queue.put(ConnectionError(f"Minecraft observation connection closed: {error}"))
        else:
            if state.websocket is websocket:
                incoming_queue.put(ConnectionError("Minecraft observation connection closed"))
