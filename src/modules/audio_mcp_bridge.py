"""GPT-Live子プロセスの音声ツール呼び出しを、親が所有するAudioMCPへ中継する。"""

import asyncio
import threading


def send_audio_message(connection, lock, message):
    """スレッド側のロックで送信を直列化し、待機元のキャンセル時もPipeへの書き込みの混在を防ぐ。"""
    with lock:
        connection.send(message)


async def receive_audio_message(connection):
    """音声MCP専用Pipeの到着を短い間隔で待ち、受信する。待機中はキャンセルできる。"""
    while not await asyncio.to_thread(connection.poll, 0.1):
        await asyncio.sleep(0)
    return connection.recv()


class AudioMCPProxy:
    """MCPを作成せず、親のClientへのツール呼び出しだけを代行する。"""

    def __init__(self, connection):
        """専用Pipe、送信ロック、要求番号と応答待ちの一覧を保持する。"""
        self.connection = connection
        self.send_lock = threading.Lock()
        self.pending = {}
        self.next_request_id = 0
        self.receiver = None
        self.failure = None

    async def __aenter__(self):
        """応答受信タスクを開始し、音声ツール呼び出しを受け付ける。"""
        self.receiver = asyncio.create_task(self.receive_results())
        return self

    async def __aexit__(self, exc_type, exc, tb):
        """応答受信を終了して待機中の呼び出しを解除し、子側の専用Pipeを閉じる。"""
        self.receiver.cancel()
        await asyncio.gather(self.receiver, return_exceptions=True)
        self.connection.close()

    async def call_tool(self, name, arguments):
        """要求番号付きで親へツール呼び出しを送り、その番号のMCP応答を待つ。

        音声取得を待つ間も、送信や割り込み停止を並行して要求できる。
        """
        if self.failure is not None:
            raise self.failure
        request_id = self.next_request_id
        self.next_request_id += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await asyncio.to_thread(send_audio_message, self.connection, self.send_lock, {
                "id": request_id, "name": name, "arguments": arguments,
            })
            return await future
        finally:
            self.pending.pop(request_id, None)

    async def receive_results(self):
        """親からの応答を要求元へ返す。接続終了時は全ての応答待ちをエラーで解除する。"""
        try:
            while True:
                message = await receive_audio_message(self.connection)
                future = self.pending.get(message["id"])
                if future is None or future.done():
                    continue
                if "error" in message:
                    future.set_exception(RuntimeError(message["error"]))
                else:
                    future.set_result(message["result"])
        except (EOFError, OSError) as error:
            self.failure = RuntimeError(f"AudioMCP connection closed: {error}")
        finally:
            if self.failure is None:
                self.failure = RuntimeError("AudioMCP response receiver stopped")
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(self.failure)


async def serve_audio_mcp(connection, audio_mcp_client):
    """親の既存AudioMCPへ子の要求を並行して渡し、応答を要求番号付きで返す。

    MCPの作成・初期設定・終了は行わない。子の終了まで中継を維持し、最後に専用Pipeを閉じる。
    """
    send_lock = threading.Lock()
    calls = set()

    async def dispatch(message):
        """指定ツールを既存Clientで実行し、MCP応答または接続側の例外内容を子へ返す。"""
        try:
            result = await audio_mcp_client.call_tool(message["name"], message["arguments"])
            response = {"id": message["id"], "result": result}
        except Exception as error:
            response = {"id": message["id"], "error": f"{type(error).__name__}: {error}"}
        await asyncio.to_thread(send_audio_message, connection, send_lock, response)

    try:
        async with asyncio.TaskGroup() as group:
            try:
                while True:
                    message = await receive_audio_message(connection)
                    task = group.create_task(dispatch(message))
                    calls.add(task)
                    task.add_done_callback(calls.discard)
            except (EOFError, OSError):
                pass
            finally:
                for task in calls:
                    task.cancel()
    finally:
        connection.close()
