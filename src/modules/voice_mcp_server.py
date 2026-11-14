from __future__ import annotations
import asyncio
import base64
import queue
import time
import threading
import numpy as np
from time import perf_counter
from contextlib import asynccontextmanager
from mcp.server import MCPServer
from modules.config_dataclass import OpusConfig, TTSConfig, VoiceChannel_WebsocketConfig
from modules.websocketconnector import WebsocketConnector
from modules.voice_mcp_process import PCMOpusEncoder, decode_base64, MinecraftAudioProcessor


def build_easy_llm_voice_variables(agent_id, player_name, sample_rate, frame_millis):
    """エージェント種別・名前・Opusの周波数とフレーム長を、Minecraft音声接続の初期設定メッセージにまとめる。"""
    return {
        "type": "setup",
        "agent_id": agent_id,
        "player_name": player_name,
        "audio_codec": "opus",
        "sample_rate": sample_rate,
        "channels": 1,
        "frame_millis": frame_millis,
    }


def build_voice_mcp_server(
    log_dir: str,
    opus_cfg: OpusConfig,
    tts_cfg: TTSConfig,
    voicechannel_cfg: VoiceChannel_WebsocketConfig,
    minecraft_voicechannel_connection: WebsocketConnector | None = None,
    *,
    enable_asr: bool = True,
    enable_tts: bool = True,
    playback_buffer_ms: float = 0,
    buffer_each_speech: bool = False,
    speech_silence_ms: float = 500,
    speech_rms_threshold: float = 200,
) -> MCPServer:
    """音声受信・文字起こし取得・音声合成・PCM送信を公開するMCPサーバーを作る。

    ASRとTTSの有効設定、再生バッファ、発話の無音判定設定を保持する。
    """
    if not 0 <= playback_buffer_ms < float("inf"):
        raise ValueError("playback_buffer_ms must be finite and nonnegative")
    if not 0 < speech_silence_ms < float(
        "inf"
    ) or not 0 <= speech_rms_threshold < float("inf"):
        raise ValueError("Invalid speech silence settings")
    active_connection = minecraft_voicechannel_connection
    process = None
    process_tasks = []
    setup_started = False
    setup_complete = False
    pcm_task = None
    setup_lock = asyncio.Lock()
    active_playback_id = 0
    output_lock = asyncio.Lock()
    audio_request_queue = queue.Queue()
    minecraft_receive_queue = queue.Queue()
    shutdown_event = threading.Event()
    pcm_error = None
    sent_frame_count = 0

    def audio_for_mcp(items):
        """受信内容のうちPCMバイト列を、16kHzの情報とBase64文字列を持つMCP返却用データへ変換する。

        それ以外のパケットはそのまま保持する。
        """
        return [
            (
                {
                    "type": "pcm",
                    "sample_rate": 16000,
                    "pcm_data_base64": base64.b64encode(item).decode("ascii"),
                }
                if isinstance(item, bytes)
                else item
            )
            for item in items
        ]

    @asynccontextmanager
    async def minecraft_voicechannel_lifespan(_server):
        """音声WebSocketの待ち受けを起動し、MCPの利用終了時にPCMワーカー・音声処理・接続を順に終了する。"""
        nonlocal active_connection, process, setup_complete
        if active_connection is None:
            active_connection = WebsocketConnector(
                ws_name="minecraft_voicechannel_connection",
                host=voicechannel_cfg.host,
                port=voicechannel_cfg.port,
                output_data_for_q=True,
                output_q=minecraft_receive_queue,
            )
        else:
            active_connection.queue = minecraft_receive_queue
        connection_task = asyncio.create_task(asyncio.to_thread(active_connection.run))
        try:
            await asyncio.to_thread(active_connection.wait_until_listening, 10.0)
            yield
        finally:
            setup_complete = False
            shutdown_event.set()
            audio_request_queue.put(("shutdown", None))
            try:
                if pcm_task is not None:
                    await pcm_task
            finally:
                if process is not None:
                    process.stop_event.set()
                try:
                    await asyncio.gather(*process_tasks, return_exceptions=True)
                finally:
                    await asyncio.to_thread(active_connection.close)
                    await asyncio.gather(connection_task, return_exceptions=True)

    mcp = MCPServer(
        "minecraft-voice-mcp-server",
        instructions="",
        lifespan=minecraft_voicechannel_lifespan,
    )

    def require_setup():
        """音声初期設定が完了しているか確認し、完了前の音声ツール利用を拒否する。"""
        if not setup_complete:
            raise RuntimeError(
                "Call easy_llm_voice_setup successfully before using audio tools"
            )

    @mcp.tool()
    async def easy_llm_voice_setup(agent_name: str, agent_type: str = "BotPlayer"):
        """エージェント名を登録してMinecraftへ音声設定を送り、受信処理とPCM送信ワーカーを開始する。

        設定の試行は1インスタンスにつき1回だけ許可する。
        """
        nonlocal process, setup_started, setup_complete, pcm_task
        if not agent_name.strip():
            raise ValueError("agent_name must not be empty")
        async with setup_lock:
            if setup_started:
                raise RuntimeError(
                    "Audio setup already attempted; create a new server instance"
                )
            setup_started = True
            try:
                process = MinecraftAudioProcessor(
                    log_dir,
                    agent_name,
                    minecraft_receive_queue,
                    opus_cfg,
                    tts_cfg,
                    enable_asr=enable_asr,
                    enable_tts=enable_tts,
                    debug_audio=False,
                )
                await asyncio.to_thread(
                    active_connection.send,
                    build_easy_llm_voice_variables(
                        agent_type,
                        agent_name,
                        opus_cfg.sample_rate,
                        opus_cfg.frame_millis,
                    ),
                )
                pcm_task = asyncio.create_task(asyncio.to_thread(pcm_worker))
                process_tasks.append(asyncio.create_task(process.run()))
                setup_complete = True
            except BaseException:
                if process is not None:
                    process.stop_event.set()
                await asyncio.to_thread(active_connection.close)
                raise
        return {"result": True}

    @mcp.tool()
    async def get_all_audio():
        """蓄積された受信音声の全件を、MCPで返せる形式へ変換して返す。

        未読位置は進めない。
        """
        require_setup()
        return audio_for_mcp(process.buffers.received_audio.get_all())

    @mcp.tool()
    async def get_new_audio(timeout_sec: float = 0.0, pcm: bool = False):
        """前回取得以降の音声だけを返す。

        pcm指定で16kHzの復号済み音声を選び、未読データがなければ指定秒数まで待つ。
        無音も取得対象に含む。
        """
        require_setup()
        if not 0 <= timeout_sec <= 1:
            raise ValueError("timeout_sec must be between 0 and 1")
        buffer = process.received_pcm if pcm else process.buffers.received_audio
        items = await asyncio.to_thread(buffer.get_since_last_read, timeout_sec)
        return audio_for_mcp(items)

    @mcp.tool()
    async def get_all_asr_result():
        """蓄積された確定済みの音声認識結果を全件返す。

        未読位置は進めない。
        """
        require_setup()
        return process.buffers.received_asr_result.get_all()

    @mcp.tool()
    async def get_new_asr_result():
        """前回取得以降に追加された確定済みの音声認識結果を返し、未読位置を進める。"""
        require_setup()
        return process.buffers.received_asr_result.get_since_last_read()

    def send_voice_commands(active_connection, commands, frame_sec):
        """音声コマンドを順に送る。

        音声フレームは設定された間隔に合わせて待機し、停止通知などは待機せず送る。
        """
        next_send_time = time.monotonic()
        for command in commands:
            if command["type"] == "voice_frame":
                sleep_time = next_send_time - time.monotonic()
                if sleep_time > 0:
                    time.sleep(sleep_time)
                active_connection.send(command)
                next_send_time += frame_sec
            else:
                active_connection.send(command)

    @mcp.tool()
    async def gen_audio_from_text_and_send_for_minecraft(
        speech_str: str, speaker_id: int = 23
    ):
        """文章を音声フレームへ合成し、設定した間隔でMinecraftへ送信する。

        合成と送信は別スレッドで実行する。
        """
        require_setup()
        commands = await asyncio.to_thread(
            process.build_tts_voice_commands, speech_str, speaker_id
        )
        await asyncio.to_thread(
            send_voice_commands, active_connection, commands, opus_cfg.frame_sec
        )
        return {"result": True}

    @mcp.tool()
    async def send_audio_data_for_minecraft(speech_str: str, speaker_id: int = 23):
        """文章を音声合成してMinecraftへ送る既存ツールへの呼び出しを、共通の合成・送信処理へ渡す。"""
        return await gen_audio_from_text_and_send_for_minecraft(speech_str, speaker_id)

    def pcm_worker():
        """キューのPCMを状態を保ってOpusへ変換し、音声フレームを設定間隔で送信する。

        古い再生IDの要求は省き、停止・終了要求と変換・送信エラーも扱う。
        """
        nonlocal pcm_error, sent_frame_count

        opus_encoder = None
        next_frame_sequence = 0
        next_send_time_s = None
        last_sent_sequence = -1
        silence_ms = 0.0
        waiting_for_speech_start = True
        while True:
            # 1. PCMまたは停止・終了要求を取り出す。
            type, data = audio_request_queue.get()
            try:
                if type == "shutdown":
                    return
                if type == "stop":

                    opus_encoder = None
                    next_send_time_s = None
                    active_connection.send(
                        {
                            "type": "voice_stop",
                            "last_sequence": max(0, last_sent_sequence),
                        }
                    )

                    continue
                raw, rate, final, playback_id = data

                if playback_id != active_playback_id or shutdown_event.is_set():
                    continue
                else:
                    if pcm_error is not None:
                        raise RuntimeError(str(pcm_error))
                    # 2. リサンプリング・20 ms Opus化。変換器の状態はチャンク間で保持する。
                    if opus_encoder is None:
                        opus_encoder = PCMOpusEncoder(
                            rate, opus_cfg.sample_rate, opus_cfg.frame_millis
                        )
                        next_frame_sequence = 0
                        silence_ms = 0.0
                        waiting_for_speech_start = True
                    if rate != opus_encoder.sample_rate:
                        raise ValueError("sample_rate cannot change inside a stream")
                    buffer_flags = []

                    def inspect_frame(frame):
                        """PCMフレームの音量と連続無音時間から発話の始まりを判定し、発話ごとの再生バッファを適用する位置を記録する。"""
                        nonlocal silence_ms, waiting_for_speech_start
                        onset = False
                        if buffer_each_speech:
                            samples = np.frombuffer(frame, dtype="<i2").astype(
                                np.float64
                            )
                            rms = float(np.sqrt(np.mean(samples * samples)))
                            if rms <= speech_rms_threshold:
                                silence_ms += opus_cfg.frame_millis
                                if silence_ms >= speech_silence_ms:
                                    waiting_for_speech_start = True
                            else:
                                onset = waiting_for_speech_start
                                waiting_for_speech_start = False
                                silence_ms = 0.0
                        buffer_flags.append(onset)

                    opus_encoder.trace_frame = (
                        inspect_frame if buffer_each_speech else None
                    )
                    packets = opus_encoder.feed(raw, final=final)
                    commands = []
                    for index, packet in enumerate(packets):
                        commands.append(
                            {
                                "type": "voice_frame",
                                "sequence": next_frame_sequence,
                                "opus_data_base64": base64.b64encode(packet).decode(
                                    "ascii"
                                ),
                                "whispering": False,
                            }
                        )
                        if index < len(buffer_flags) and buffer_flags[index]:
                            commands[-1]["_buffer_before"] = True
                        next_frame_sequence += 1
                    if final:
                        commands.append(
                            {
                                "type": "voice_stop",
                                "last_sequence": max(0, next_frame_sequence - 1),
                            }
                        )
                        opus_encoder = None

                    for command in commands:
                        if not shutdown_event.is_set():
                            if command["type"] == "voice_frame":
                                if command.pop("_buffer_before", False):
                                    next_send_time_s = None
                                now = perf_counter()
                                if next_send_time_s is None:
                                    next_send_time_s = now + playback_buffer_ms / 1000
                                if now < next_send_time_s:
                                    time.sleep(next_send_time_s - now)
                                elif now - next_send_time_s >= opus_cfg.frame_sec:
                                    next_send_time_s = now
                                active_connection.send(command)

                                sent_frame_count += 1
                                last_sent_sequence = command["sequence"]
                                next_send_time_s += opus_cfg.frame_sec
                            else:
                                active_connection.send(command)
                                next_send_time_s = None

            except Exception as exc:
                pcm_error = exc
            finally:
                audio_request_queue.task_done()

    @mcp.tool()
    async def send_pcm_audio_for_minecraft(
        pcm_base64: str,
        sample_rate: int = 24000,
        end_of_stream: bool = False,
        playback_id: int = 0,
    ):
        """PCMを復元して送信キューへ追加し、受付量を返す。

        古い再生IDなら受け付けず割り込み済みと返す。
        ストリーム終了指定もキューへ渡し、この関数では送信完了を待たない。
        """
        require_setup()
        raw = decode_base64(pcm_base64)
        async with output_lock:
            if pcm_error is not None:
                raise RuntimeError(str(pcm_error))
            if playback_id != active_playback_id:
                return {
                    "result": True,
                    "interrupted": True,
                    "sent_frames": sent_frame_count,
                }
            audio_request_queue.put(
                ("audio", (raw, sample_rate, end_of_stream, playback_id))
            )
        return {
            "result": True,
            "accepted_pcm_bytes": len(raw),
            "sent_frames": sent_frame_count,
        }

    @mcp.tool()
    async def stop_pcm_audio_for_minecraft():
        """未処理のPCM要求をキューから破棄し、停止要求を追加して再生IDを更新する。

        ワーカーが処理中の音声断片はそのまま完了させる。
        """
        nonlocal active_playback_id
        require_setup()
        async with output_lock:
            while True:
                try:
                    discarded_type, discarded_data = audio_request_queue.get_nowait()
                except queue.Empty:
                    break
                audio_request_queue.task_done()
            audio_request_queue.put(("stop", None))
            active_playback_id += 1
            return {
                "result": True,
                "playback_id": active_playback_id,
                "sent_frames": sent_frame_count,
            }

    return mcp