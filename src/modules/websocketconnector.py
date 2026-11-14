import json
import sys
import time
import queue
import threading
from websockets.sync.server import serve

MAX_DATA_SIZE = None
# MAX_DATA_SIZE = 1 * 1024 * 1024  # default of websockets

class WebsocketConnector:
    def __init__(self,
                 ws_name,
                 host:str="localhost",
                 port:int=8765,
                 output_data_for_q:bool=True,
                 output_q:queue.Queue=queue.Queue()
                 ):
        """WebSocketサーバーの名前・待ち受け先・受信キューを保持し、接続状態と待ち受け開始の通知を用意する。"""
        self.ws_name = ws_name
        self.host = host
        self.port = port
        self.bool_output_data_for_q = output_data_for_q
        self.websocket = None
        self.queue = output_q
        self.server = None
        self.listening = threading.Event()

    def handler(self, websocket): # 接続すると駆動する recv と 同意義
        """受け付けた接続を現在の送信先として保持し、設定が有効なら受信内容をキューへ追加する。

        切断時は接続参照を解除する。
        """
        print(f"[{self.ws_name}] connected", file=sys.stderr)
        self.websocket = websocket
        try:
            for message in websocket:   # 内部では recv() を繰り返している
                if self.bool_output_data_for_q:
                    self.queue.put(message)
        finally:
            if self.websocket is websocket:
                self.websocket = None
            print(f"[{self.ws_name}] disconnected", file=sys.stderr)

    def send(self, data: dict, timeout: float = 30.0) -> None:
        """クライアント接続を制限時間まで待ち、指定データをJSONとして送る。

        接続できなければタイムアウトにする。
        """
        deadline = time.monotonic() + timeout
        websocket = self.websocket
        while websocket is None and time.monotonic() < deadline:
            time.sleep(0.1)
            websocket = self.websocket
        if websocket is None:
            raise TimeoutError(f"WebSocket client did not connect to {self.host}:{self.port} within {timeout} seconds")
        websocket.send(json.dumps(data))
        # print(f"[{self.ws_name}] sent data", file=sys.stderr)

    def run(self):
        """WebSocketサーバーの待ち受けを開始して起動を通知し、接続を受け付け続ける。

        終了時は起動通知を解除する。
        """
        handler = self.handler
        host = self.host
        port = self.port
        listening = self.listening

        with serve(handler, host, port, max_size=MAX_DATA_SIZE) as server:
            self.server = server
            listening.set()
            print(f"listening on ws://{host}:{port}", file=sys.stderr)
            try:
                server.serve_forever()
            finally:
                listening.clear()
                self.server = None

    def wait_until_listening(self, timeout: float = 10.0) -> None:
        """WebSocketサーバーの待ち受け開始を指定秒数まで待つ。

        起動しなければタイムアウトにする。
        """
        if not self.listening.wait(timeout):
            raise TimeoutError(f"WebSocket server did not start on {self.host}:{self.port} within {timeout} seconds")

    def close(self):
        """現在のクライアント接続を閉じ、その後WebSocketサーバーの待ち受けを停止する。"""
        websocket = self.websocket
        if websocket is not None:
            websocket.close()
            self.websocket = None

        server = self.server
        if server is not None:
            server.shutdown()


class AudioWebsocketConnector(WebsocketConnector):
    """Preserve audio transport; expose bind failures to the startup waiter."""

    def __init__(self, **kwargs):
        """音声用WebSocket接続を初期化し、待ち受け開始時のエラーを親側へ伝えるための保存欄を用意する。"""
        super().__init__(**kwargs)
        self.startup_error = None

    def run(self):
        """音声WebSocketの待ち受けを開始する。

        起動に失敗した場合は原因を保存し、起動完了待ちを解除して失敗を通知できるようにする。
        """
        try:
            super().run()
        except Exception as error:
            self.startup_error = error
            self.listening.set()  # Wake wait_until_listening so it can report the cause.

    def wait_until_listening(self, timeout=10.0):
        """指定秒数まで音声WebSocketの起動を待つ。

        起動処理が失敗していれば、接続先と原因を含むエラーを返す。
        """
        super().wait_until_listening(timeout)
        if self.startup_error is not None:
            raise RuntimeError(
                f"Audio WebSocket could not listen on {self.host}:{self.port}: "
                f"{type(self.startup_error).__name__}: {self.startup_error}"
            ) from self.startup_error


if __name__ == "__main__":
    wscon = WebsocketConnector("easy_llm", "0.0.0.0", 7891, True)
    threading.Thread(target=wscon.run).start()
    first_access_data = {"type": "first_access",
                         "min": {"x": 0, "y": 60, "z": 0},
                         "max": {"x": 15, "y": 80, "z": 15}}
    wscon.send(first_access_data)
    while True:
        data = wscon.output_data_for_q.get()
        print(json.dumps(json.loads(data), ensure_ascii=False, indent=2))
