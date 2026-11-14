from __future__ import annotations
import asyncio
import json
import threading
import logging
import time
from typing import Any
from uuid import uuid4

import numpy as np
import websocket

def json_default(o):
    """JSON標準変換で扱えない値のうち、3要素のNumPy配列を座標リストへ変換する。

    それ以外はTypeErrorにする。
    """
    if isinstance(o, np.ndarray):
        if o.shape == (3,):
            return o.tolist()
        raise TypeError(f"Unsupported ndarray shape: {o.shape}, value={o!r}")

    raise TypeError(f"Object is not JSON serializable: type={type(o).__name__}, value={o!r}")

def build_mineflayer_variables(env_box: list[list[int]]) -> dict[str, str]:
    """観測範囲の最小・最大座標を、Mineflayerへ渡すenvBoxのJSON文字列にする。"""
    return {
        "envBox": json.dumps([{"__Vec3__": env_box[0]}, {"__Vec3__": env_box[1]}]),
    }

class MineflayerConnection:
    def __init__(self, host="localhost", port=3000, logger=None|logging.Logger, ws_factory=None):
        """Mineflayerの接続先、WebSocket生成方法、応答管理用の辞書とロックを用意する。"""
        self.host = host
        self.port = port
        self.logger = logger
        self.ws_factory = ws_factory or websocket.WebSocket
        self.ws = None
        self.running = False
        self.receiver_thread = None
        self.receiver_error = None
        self.responses = {}
        self.lock = threading.Lock()

    async def connect(self):
        """イベントループを止めないよう別スレッドでWebSocket接続を開始する。"""
        await asyncio.to_thread(self._connect)

    def _connect(self):
        """未接続であればMineflayerへ接続し、応答を受信するバックグラウンドスレッドを開始する。"""
        if self.ws is not None:
            return
        self.ws = self.ws_factory()
        self.ws.connect(f"ws://{self.host}:{self.port}")
        self.running = True
        self.receiver_thread = threading.Thread(target=self._receiver_loop, daemon=True)
        self.receiver_thread.start()

    async def close(self):
        """別スレッドでMineflayer接続の終了処理を実行する。"""
        await asyncio.to_thread(self._close)

    def _close(self):
        """受信ループの停止を指示し、接続中のWebSocketを閉じて参照を解除する。"""
        self.running = False
        if self.ws is None:
            return
        self.ws.close()
        self.ws = None

    def _send_command(self, command, js_params, timeout=180):
        """一意のメッセージIDを付けてコマンドを送信し、そのIDに対応する応答を待って返す。"""
        if self.ws is None:
            raise RuntimeError("MineflayerJsClient is not connected.")

        message_id = str(uuid4())
        msg = {
            "messageId": message_id,
            "command": command,
            "params": js_params,
        }
        self.ws.send(json.dumps(msg, default=json_default))
        return self._wait_for_response(message_id, timeout, command)

    async def _run_command(self, command: str, params: dict[str, Any], timeout: int | None = 180) -> Any:
        """コマンド送信と応答待ちを別スレッドで実行し、非同期の呼び出し元へ結果を返す。"""
        return await asyncio.to_thread(self._send_command, command, params, timeout)

    def _wait_for_response(self, message_id, timeout, command):
        """指定IDの応答が届くまで待ち、取得した応答を管理辞書から取り出す。

        コマンド失敗・受信停止・時間切れは例外で通知する。
        """
        start = time.perf_counter()
        while timeout is None or time.perf_counter() - start < timeout:
            with self.lock:
                if message_id in self.responses:
                    resp = self.responses.pop(message_id)
                    if isinstance(resp, dict) and resp.get("errorMsg") and "success" not in resp:
                        raise RuntimeError(
                            f"Mineflayer command failed (command='{command}'): {resp['errorMsg']}"
                        )
                    return resp
                receiver_error = self.receiver_error
            if receiver_error:
                raise ConnectionError(
                    f"Receiver loop stopped before response (command='{command}'): {receiver_error}"
                )
            time.sleep(0.001)

        raise TimeoutError(f"Response timeout (command:'{command}', timeout_sec:{timeout}, message_id:'{message_id}')")

    def _receiver_loop(self):
        """WebSocketから応答を受信し、メッセージIDごとに保存する。

        接続断や不正なメッセージは受信エラーとして記録する。
        """
        logger = self.logger
        while self.running:
            try:
                raw = self.ws.recv()
                if raw == "":
                    self.receiver_error = "Connection closed."
                    if logger is not None and self.running:
                        logger.warning("Connection closed.")
                    break
            except Exception as e:
                self.receiver_error = str(e)
                if logger is not None and self.running:
                    logger.warning(f"Receiver error: Failed to receive data. {e}")
                break

            try:
                msg = json.loads(raw)
            except Exception as e:
                self.receiver_error = f"Failed to parse JSON message: {e}"
                if logger is not None:
                    logger.warning(f"Receiver error: Failed to parse json message. {e}")
                break

            try:
                msg_type = msg["type"]
                if msg_type == "response":
                    with self.lock:
                        self.responses[msg["messageId"]] = msg["data"]
                elif msg_type == "heartBeat":
                    pass
                else:
                    raise Exception(f'Unknown msg_type "{msg_type}"')

            except Exception as e:
                self.receiver_error = f"Failed to process receiver message: {e}"
                if logger is not None:
                    logger.warning(f"Receiver error: {e}")
                break

class AgentMineflayerConnection(MineflayerConnection):
    async def join(self, mc_name: str, variables: dict[str, Any]) -> str:
        """指定名のエージェントをMinecraftへ参加させ、応答に含まれる管理者のMinecraft名を検証して返す。"""
        result = await self._run_command("join", {"mcName": mc_name, "variables": variables})
        if not isinstance(result, dict) or not isinstance(result.get("adminMcName"), str):
            raise RuntimeError(f"Mineflayer join returned an invalid result: {result!r}")
        return result["adminMcName"]

    async def leave(self, mc_name: str) -> Any:
        """指定名のエージェントをMinecraftから退出させるコマンドを送る。"""
        return await self._run_command("leave", {"mcName": mc_name})

    async def stop_moving(self, mc_name: str) -> Any:
        """指定名のエージェントの移動停止をMineflayerへ要求する。"""
        return await self._run_command("stopMoving", {"mcName": mc_name})

    async def exec_js(self, mc_name: str, code: str, primitives: list[str]) -> Any:
        """指定エージェントでJavaScriptを実行するよう、コードと利用可能な基本操作の定義をMineflayerへ渡す。"""
        params = {"mcName": mc_name, "code": code, "primitives": primitives}
        return await self._run_command("execJs", params, timeout=180)


class AdminMineflayerConnection(MineflayerConnection):
    async def acquire(self, variables: dict[str, Any]) -> str:
        """ワールド管理者の操作権限を取得し、応答の管理者名を検証して返す。"""
        result = await self._run_command("acquireAdmin", {"variables": variables})
        if not isinstance(result, dict) or not isinstance(result.get("mcName"), str):
            raise RuntimeError(f"Mineflayer acquireAdmin returned an invalid result: {result!r}")
        return result["mcName"]

    async def set_blocks(self, block_info_list: list[dict[str, Any]]) -> Any:
        """座標とブロック情報の一覧を渡し、管理者権限で配置するよう要求する。"""
        params = {"blockInfoList": block_info_list}
        return await self._run_command("setBlocks", params)

    async def set_containers(self, container_info_list: list[dict[str, Any]]) -> Any:
        """コンテナの座標と中身の一覧を渡し、管理者権限で設定するよう要求する。"""
        params = {"containerInfoList": container_info_list}
        return await self._run_command("setContainers", params)

    async def clear_box(self) -> Any:
        """設定されたワールド範囲を消去する管理者コマンドを送る。"""
        return await self._run_command("clearBox", {})

    async def teleport(
        self,
        mc_name: str,
        position: list[float],
        pitch: float,
        yaw: float,
        teleport_offset: list[float] | None = None,
        wait_for_arrival: bool = True,
    ) -> Any:
        """指定人物の位置と向きを変更するコマンドを送る。

        座標補正を省略した場合はY方向に1を加える指定を使う。
        """
        params = {
            "mcName": mc_name,
            "position": position,
            "pitch": pitch,
            "yaw": yaw,
            "teleportOffset": [0, 1, 0] if teleport_offset is None else teleport_offset,
            "waitForArrival": wait_for_arrival,
        }
        return await self._run_command("teleport", params)

    async def set_inventory_and_equipment(
        self, mc_name: str, inventory: dict[str, int], equipment: list[Any],
    ) -> Any:
        """指定人物の所持品と装備をまとめて設定するコマンドを送る。"""
        params = {"mcName": mc_name, "inventory": inventory, "equipment": equipment}
        return await self._run_command("setInventoryAndEquipment", params)

    async def exec_mc(self, commands: list[str]) -> Any:
        """Minecraftコマンドの一覧を管理者として実行するよう要求する。"""
        return await self._run_command("execMc", {"commands": commands})

    async def set_speed(self, speed_multiplier: float) -> float:
        """Minecraftワールドの速度倍率を変更し、サーバーが返した適用倍率を取得する。"""
        result = await self._run_command("setSpeed", {"speedMultiplier": speed_multiplier})
        return result["speedMultiplier"]

    async def pause_agent_actions(self) -> None:
        """エージェントの行動実行を一時停止するよう管理者接続から要求する。"""
        await self._run_command("pauseAgentActions", {})

    async def resume_agent_actions(self) -> None:
        """一時停止したエージェントの行動実行を再開するよう要求する。"""
        await self._run_command("resumeAgentActions", {})