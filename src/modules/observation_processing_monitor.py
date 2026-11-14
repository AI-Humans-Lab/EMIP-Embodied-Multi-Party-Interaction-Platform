import asyncio
import json
import logging
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from modules.observation_action_mcp import OBSERVATION_RUNTIME_LOGGER


_METRICS_PAGE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Observation Processing Monitor</title>
<style>
body { margin: 0; padding: 24px; color: #1e293b; background: #f1f5f9; font-family: system-ui, sans-serif; }
h1 { margin: 0 0 8px; }
#message { margin-bottom: 20px; color: #64748b; }
.processor { margin-bottom: 16px; padding: 18px; border-left: 6px solid #22c55e; border-radius: 8px; background: white; box-shadow: 0 1px 4px #0002; }
.processor.pending { border-left-color: #f59e0b; }
h2 { margin: 0 0 12px; font-size: 1.15rem; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 6px 18px; margin: 0; }
dt { color: #64748b; }
dd { margin: 0; font-family: ui-monospace, monospace; }
</style>
</head>
<body>
<h1>Observation Processing Monitor</h1>
<div id="message">Waiting for observation processors...</div>
<main id="processors"></main>
<script>
const processorsElement = document.getElementById("processors");
const messageElement = document.getElementById("message");

function addRow(list, name, value) {
  const term = document.createElement("dt");
  const description = document.createElement("dd");
  term.textContent = name;
  description.textContent = value;
  list.append(term, description);
}

function seconds(value) {
  return value == null ? "-" : `${(value * 1000).toFixed(1)} ms`;
}

function render(data) {
  processorsElement.replaceChildren();
  messageElement.textContent = data.processors.length ? `${data.processors.length} observation processor(s)` : "Waiting for observation processing data...";
  const now = Date.now() / 1000;
  for (const processor of data.processors) {
    const section = document.createElement("section");
    section.className = `processor${processor.pending_messages > 0 ? " pending" : ""}`;
    const heading = document.createElement("h2");
    heading.textContent = `${processor.agent_name} — ${processor.host}:${processor.port}`;
    const list = document.createElement("dl");
    addRow(list, "Processing", processor.enabled == null ? "-" : processor.enabled ? "enabled" : "disabled");
    addRow(list, "Pending messages", String(processor.pending_messages));
    addRow(list, "Last processing", seconds(processor.processing_seconds));
    addRow(list, "Recent maximum", seconds(processor.max_recent_processing_seconds));
    addRow(list, "Last processed", processor.last_processed_at == null ? "-" : `${(now - processor.last_processed_at).toFixed(1)} sec ago`);
    addRow(list, "Queue at last stop", processor.stop_pending_messages == null ? "-" : String(processor.stop_pending_messages));
    addRow(list, "Stop lock wait", seconds(processor.stop_lock_wait_seconds));
    addRow(list, "Stop processing", seconds(processor.stop_processing_seconds));
    section.append(heading, list);
    processorsElement.append(section);
  }
}

async function refresh() {
  try {
    const response = await fetch("/status", {cache: "no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    render(await response.json());
  } catch (error) {
    messageElement.textContent = `Monitor error: ${error}`;
  }
}

refresh();
setInterval(refresh, 500);
</script>
</body>
</html>
"""


class _ObservationProcessingMetricsHandler(logging.Handler):
    def __init__(self):
        """観測処理の測定ログを受け取るハンドラーと、接続ごとの集計値を保護するロックを用意する。"""
        super().__init__(logging.INFO)
        self.processor_metrics = {}
        self.state_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        """観測処理専用のログから、処理時間・視点計算時間・未処理件数・停止にかかった時間などを接続ごとに記録する。"""
        processor_metrics = self.processor_metrics
        state_lock = self.state_lock
        processing_metrics = getattr(record, "observation_runtime", None)
        if not isinstance(processing_metrics, dict):
            return

        processor_key = (processing_metrics["agent_name"], processing_metrics["host"], processing_metrics["port"])
        event_name = processing_metrics["event"]
        now = time.time()
        with state_lock:
            metrics = processor_metrics.setdefault(
                processor_key,
                {
                    "agent_name": processor_key[0], "host": processor_key[1], "port": processor_key[2],
                    "enabled": None, "pending_messages": 0, "processing_seconds": None,
                    "processing_times": deque(maxlen=20), "last_processed_at": None,
                    "stop_pending_messages": None, "stop_lock_wait_seconds": None, "stop_processing_seconds": None,
                },
            )
            if event_name == "pending":
                metrics["enabled"] = False
                metrics["pending_messages"] = processing_metrics["pending_messages"]
            elif event_name == "processed":
                processing_seconds = processing_metrics["processing_seconds"]
                metrics["enabled"] = True
                metrics["pending_messages"] = processing_metrics["pending_messages"]
                metrics["processing_seconds"] = processing_seconds
                metrics["processing_times"].append(processing_seconds)
                metrics["last_processed_at"] = now
            elif event_name == "stop":
                metrics["enabled"] = False
                metrics["stop_pending_messages"] = processing_metrics["stop_pending_messages"]
                metrics["stop_lock_wait_seconds"] = processing_metrics["stop_lock_wait_seconds"]
                metrics["stop_processing_seconds"] = processing_metrics["stop_processing_seconds"]

    def snapshot(self) -> dict:
        """集計済みの観測処理状況をコピーし、直近の処理時間などをブラウザ表示用の一覧にまとめる。"""
        processor_metrics = self.processor_metrics
        state_lock = self.state_lock
        with state_lock:
            processors = []
            for metrics in processor_metrics.values():
                processing_times = metrics["processing_times"]
                processors.append({
                    "agent_name": metrics["agent_name"], "host": metrics["host"], "port": metrics["port"], "enabled": metrics["enabled"],
                    "pending_messages": metrics["pending_messages"], "processing_seconds": metrics["processing_seconds"],
                    "max_recent_processing_seconds": max(processing_times) if processing_times else None,
                    "last_processed_at": metrics["last_processed_at"], "stop_pending_messages": metrics["stop_pending_messages"],
                    "stop_lock_wait_seconds": metrics["stop_lock_wait_seconds"], "stop_processing_seconds": metrics["stop_processing_seconds"],
                })
        processors.sort(key=lambda metrics: (metrics["port"], metrics["agent_name"]))
        return {"processors": processors}


class _MetricsRequestHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, metrics_handler: _ObservationProcessingMetricsHandler, **kwargs):
        """HTTP要求の処理に、観測処理状況を読み出すハンドラーを渡して初期化する。"""
        self.metrics_handler = metrics_handler
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        """ルートURLでは監視画面、/statusでは最新状況のJSONを返す。

        それ以外のURLは404にする。
        """
        path = self.path
        metrics_handler = self.metrics_handler
        if path == "/":
            self._respond("text/html; charset=utf-8", _METRICS_PAGE_HTML.encode("utf-8"))
        elif path == "/status":
            body = json.dumps(metrics_handler.snapshot(), ensure_ascii=False).encode("utf-8")
            self._respond("application/json; charset=utf-8", body)
        else:
            self.send_error(404)

    def _respond(self, content_type: str, body: bytes) -> None:
        """指定した内容と形式を、キャッシュを無効にしたHTTP応答として送る。"""
        wfile = self.wfile
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        wfile.write(body)

    def log_message(self, _format: str, *_args) -> None:
        """HTTPサーバーの通常のアクセスログ出力を抑制する。"""
        pass


class _MetricsHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


@asynccontextmanager
async def observation_processing_monitor(*, port: int):
    """観測処理ログの収集と監視用HTTPサーバーを利用期間中だけ起動する。

    終了時はサーバーを止め、元のログ設定へ戻す。
    """
    metrics_handler = _ObservationProcessingMetricsHandler()
    server = _MetricsHTTPServer(("0.0.0.0", port), partial(_MetricsRequestHandler, metrics_handler=metrics_handler))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    previous_disabled = OBSERVATION_RUNTIME_LOGGER.disabled
    previous_level = OBSERVATION_RUNTIME_LOGGER.level
    OBSERVATION_RUNTIME_LOGGER.addHandler(metrics_handler)
    OBSERVATION_RUNTIME_LOGGER.disabled = False
    OBSERVATION_RUNTIME_LOGGER.setLevel(logging.INFO)
    server_thread.start()
    try:
        yield server.server_port
    finally:
        OBSERVATION_RUNTIME_LOGGER.removeHandler(metrics_handler)
        OBSERVATION_RUNTIME_LOGGER.disabled = previous_disabled
        OBSERVATION_RUNTIME_LOGGER.setLevel(previous_level)
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        await asyncio.to_thread(server_thread.join)
