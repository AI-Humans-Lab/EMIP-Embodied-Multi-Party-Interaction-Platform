from __future__ import annotations
import base64
import binascii
import numpy as np
import av
import time
import json
import queue
import logging
import audioop
import asyncio
import threading
from dataclasses import dataclass

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modules.asr import RealtimeSTT
if TYPE_CHECKING:
    from modules.tts import VOICEVOX_TTS
from modules.config_dataclass import OpusConfig, TTSConfig
from modules.utils import make_file_logger


# PCM/Opus変換。エンコーダーはストリームの端数とcodec状態を維持する。
def decode_base64(value: str) -> bytes:
    """Base64で渡されたPCMまたはOpusのバイト列を復元する。

    不正なBase64は分かりやすいValueErrorに変換する。
    """
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(
            "Expected base64-encoded raw PCM/Opus, not a WAV file"
        ) from exc


def pcm_frame(data: bytes, rate: int):
    """16bitリトルエンディアン・モノラルのPCMバイト列を、指定周波数のPyAV音声フレームへ変換する。"""
    frame = av.AudioFrame.from_ndarray(
        np.frombuffer(data, dtype="<i2").reshape(1, -1), format="s16", layout="mono"
    )
    frame.sample_rate = rate
    return frame


def frame_bytes(frame) -> bytes:
    # to_ndarray excludes plane alignment padding.
    """PyAV音声フレームから、内部の位置合わせ用余白を含まない16bitリトルエンディアンのPCMバイト列を取り出す。"""
    return frame.to_ndarray().astype("<i2", copy=False).tobytes()


class PCMOpusEncoder:
    """One instance per stream; retain odd bytes, resampling delay and Opus state."""

    def __init__(self, sample_rate=24000, output_rate=48000, frame_ms=20):
        """1本のPCMストリームを48kHz・20msのOpusへ変換する状態を用意する。

        断片間で端数・リサンプル状態・符号化状態を保持する。
        """
        if sample_rate not in (16000, 24000, 48000):
            raise ValueError("sample_rate must be 16000, 24000 or 48000")
        if output_rate != 48000 or frame_ms != 20:
            raise ValueError("Streaming Minecraft output requires 48000 Hz / 20 ms")
        self.sample_rate = sample_rate
        self.output_rate = output_rate
        self.frame_bytes = output_rate * frame_ms // 1000 * 2
        self.pending = bytearray()
        self.pcm = bytearray()
        self.finished = False
        self.padding_samples = 0
        self.trace_frame = None
        self.resampler = av.AudioResampler(
            format="s16", layout="mono", rate=output_rate
        )
        self.codec = av.CodecContext.create("libopus", "w")
        self.codec.sample_rate = output_rate
        self.codec.layout = "mono"
        self.codec.format = "s16"
        self.codec.options = {"application": "voip", "frame_duration": str(frame_ms)}

    def feed(self, data: bytes, final=False) -> list[bytes]:
        """追加のPCMを受け取り、変換できた分のOpusパケットを返す。

        端数は次回へ持ち越し、終了時は無音でフレームを埋めて残りを出力する。
        """
        if self.finished:
            raise ValueError("Stream has already ended")
        if final and (len(self.pending) + len(data)) % 2:
            raise ValueError("Final PCM16 stream has an incomplete 16-bit sample")
        self.pending.extend(data)
        size = len(self.pending) // 2 * 2
        if size:
            chunk = bytes(self.pending[:size])
            del self.pending[:size]
            for frame in self.resampler.resample(pcm_frame(chunk, self.sample_rate)):
                self.pcm.extend(frame_bytes(frame))
        if final:
            for frame in self.resampler.resample(None):
                self.pcm.extend(frame_bytes(frame))
            if self.pcm and len(self.pcm) % self.frame_bytes:
                padding_bytes = self.frame_bytes - len(self.pcm) % self.frame_bytes
                self.padding_samples += padding_bytes // 2
                self.pcm.extend(bytes(padding_bytes))
        packets = []
        while len(self.pcm) >= self.frame_bytes:
            chunk = bytes(self.pcm[: self.frame_bytes])
            del self.pcm[: self.frame_bytes]
            if self.trace_frame:
                self.trace_frame(chunk)
            packets.extend(
                bytes(p) for p in self.codec.encode(pcm_frame(chunk, self.output_rate))
            )
        if final:
            packets.extend(bytes(p) for p in self.codec.encode(None))
            self.finished = True
        return packets


# 受信バッファと話者ごとの音声処理
class Buffer:
    def __init__(self):
        """受信内容を蓄積する一覧、未読位置、追加を待つための同期機構を用意する。"""
        self._items = []
        self._read_index = 0
        self._lock = threading.Condition()

    def append(self, item) -> None:
        """受信内容を末尾へ追加し、新しいデータを待っている処理へ通知する。"""
        with self._lock:
            self._items.append(item)
            self._lock.notify_all()

    def get_all(self) -> list:
        """これまでに蓄積した内容をリストのコピーで返す。

        未読位置は変更しない。
        """
        with self._lock:
            return list(self._items)

    def get_since_last_read(self, timeout_sec: float = 0.0) -> list:
        """前回の取得以降に追加された内容を返し、未読位置を進める。

        指定があれば新しい内容の到着を制限時間まで待つ。
        蓄積済みのデータ自体は削除しない。
        """
        with self._lock:
            if timeout_sec > 0:
                self._lock.wait_for(
                    lambda: self._read_index < len(self._items), timeout_sec
                )
            items = self._items[self._read_index :]
            self._read_index = len(self._items)
            return list(items)


def encode_opus_packets(pcm, sample_rate, frame_ms, application="voip"):
    """モノラルPCM配列を指定長のフレームに区切ってOpusへ符号化する。

    最後の端数を無音で埋め、符号化器に残るパケットも取り出す。
    """
    frame_samples = sample_rate * frame_ms // 1000
    if frame_samples <= 0:
        raise ValueError("invalid frame_ms")
    codec = av.CodecContext.create("libopus", "w")
    codec.sample_rate = sample_rate
    codec.layout = "mono"
    codec.format = "s16"
    codec.options = {"application": application, "frame_duration": str(frame_ms)}
    pcm = np.ascontiguousarray(pcm, dtype=np.int16)
    packets = []
    for start in range(0, len(pcm), frame_samples):
        chunk = pcm[start : start + frame_samples]
        if len(chunk) < frame_samples:
            chunk = np.pad(chunk, (0, frame_samples - len(chunk)))
        frame = av.AudioFrame(format="s16", layout="mono", samples=frame_samples)
        frame.sample_rate = sample_rate
        frame.planes[0].update(np.ascontiguousarray(chunk, dtype=np.int16).tobytes())
        packets.extend(bytes(packet) for packet in codec.encode(frame))
    packets.extend(bytes(packet) for packet in codec.encode(None))
    return packets


def decoded_frame_to_mono_int16(frame):
    """復号済みの音声フレームを、そのチャンネル配置と数値形式に従ってモノラル16bit整数PCMへ変換する。"""
    samples = frame.to_ndarray()
    channels = len(frame.layout.channels)
    samples = samples.T if frame.format.is_planar else samples.reshape(-1, channels)
    if np.issubdtype(samples.dtype, np.floating):
        samples = samples.astype(np.float64) * 32768.0
    elif samples.dtype == np.uint8:
        samples = (samples.astype(np.float64) - 128.0) * 256.0
    else:
        samples = samples.astype(np.float64) * (
            32768.0 / (2 ** (samples.dtype.itemsize * 8 - 1))
        )
    mono = samples.mean(axis=1)
    return np.clip(np.rint(mono), -32768, 32767).astype(np.int16)


def decode_opus_packets(opus_packets, sample_rate, decoder=None):
    """Opusパケット列をPCM配列へ復号する。

    共有の復号器が渡された場合は状態を保ち、自分で作った場合だけ最後の残りも取り出す。
    """
    owns_decoder = decoder is None
    if owns_decoder:
        decoder = av.CodecContext.create("libopus", "r")
        decoder.sample_rate = sample_rate
    decoded_chunks = []
    for opus_packet in opus_packets:
        for frame in decoder.decode(av.Packet(opus_packet)):
            decoded_chunks.append(decoded_frame_to_mono_int16(frame))
    if owns_decoder:
        for frame in decoder.decode(None):
            decoded_chunks.append(decoded_frame_to_mono_int16(frame))
    if not decoded_chunks:
        return np.zeros(0, dtype=np.int16)
    return np.concatenate(decoded_chunks)


def split_packets_by_speaker(
    packet: dict,
    agent_name: str,
):
    """受信したvoice_frameを話者別に分け、Base64のOpusを復元する。

    自分の声や音声フレーム以外は対象にしない。
    """
    packets_by_speaker = {}

    if packet.get("type") != "voice_frame":
        return packets_by_speaker

    speaker_name = packet["speaker_name"]

    if speaker_name == agent_name:
        return packets_by_speaker

    packets_by_speaker.setdefault(speaker_name, []).append(
        {
            "speaker_uuid": packet["speaker_uuid"],
            "sequence": packet["sequence"],
            "opus_bytes": base64.b64decode(packet["opus_data_base64"]),
        }
    )

    return packets_by_speaker


def mix_timed_pcm_chunks_to_single_pcm(chunks, sample_rate=48000):
    """各PCM断片を記録時刻に合わせて重ね、16bitの範囲に収めた混合音声を返す。

    入力が空の場合は空配列とNoneを2つ含むタプルを返す。
    """
    if not chunks:
        return np.zeros(0, dtype=np.int16), None, None
    t0 = min(chunk["captured_at_ms"] for chunk in chunks)
    total_samples = 0
    for chunk in chunks:
        start_sample = int((chunk["captured_at_ms"] - t0) * sample_rate / 1000)
        total_samples = max(total_samples, start_sample + len(chunk["pcm"]))
    mixed = np.zeros(total_samples, dtype=np.int32)
    for chunk in chunks:
        start_sample = int((chunk["captured_at_ms"] - t0) * sample_rate / 1000)
        end_sample = start_sample + len(chunk["pcm"])
        mixed[start_sample:end_sample] += chunk["pcm"].astype(np.int32)
    mixed = np.clip(mixed, -32768, 32767).astype(np.int16)
    return mixed


class MinecraftAudioProcessor:
    @dataclass(frozen=True)
    class Config:
        name: str
        opus: OpusConfig
        tts: TTSConfig

    @dataclass(frozen=True)
    class Queues:
        from_websocket: queue.Queue
        asr_input_audio: queue.Queue | None
        asr_result: queue.Queue | None

    @dataclass(frozen=True)
    class Modules:
        asr: RealtimeSTT | None
        tts: VOICEVOX_TTS | None
        logger: logging.Logger

    @dataclass(frozen=True)
    class Buffers:
        received_audio: Buffer
        received_asr_result: Buffer

    def __init__(
        self,
        log_dir,
        self_name,
        from_websocket_q: queue.Queue,
        opus_cfg: OpusConfig,
        tts_cfg: TTSConfig,
        *,
        enable_asr: bool = True,
        enable_tts: bool = True,
        audio_trace=None,
        debug_audio: bool = True,
    ):
        """Minecraft音声の受信・復号・保存に必要なキューとバッファを作り、設定で有効な場合だけ音声認識器と音声合成器を準備する。"""
        self.stop_event = threading.Event()
        self.audio_trace = audio_trace if debug_audio else None
        asr = None
        if enable_asr:
            from modules.asr import RealtimeSTT

            asr = RealtimeSTT(logdir=log_dir)
        tts = None
        if enable_tts:
            from modules.tts import VOICEVOX_TTS

            tts = VOICEVOX_TTS(
                log_dir=log_dir,
                host=tts_cfg.host,
                port=tts_cfg.port,
                enable_interrogative_upspeak=tts_cfg.enable_interrogative_upspeak,
                output_sampling_rate=tts_cfg.output_sampling_rate,
                output_stereo=tts_cfg.output_stereo,
            )

        self.debug_audio = debug_audio
        logger = (
            make_file_logger(
                name="AudioMcpProcess", log_path=f"{log_dir}/AudioMcpProcess.log"
            )
            if debug_audio
            else logging.Logger("AudioMcpProcess.disabled")
        )

        self.queues = self.Queues(
            from_websocket=from_websocket_q,
            asr_input_audio=asr.input_buffer if asr else None,
            asr_result=asr.output_buffer if asr else None,
        )
        self.modules = self.Modules(asr=asr, tts=tts, logger=logger)
        self.config = self.Config(name=self_name, opus=opus_cfg, tts=tts_cfg)

        self.opus_decoders_by_speaker_uuid = {}
        self.received_pcm = Buffer()
        self.buffers = self.Buffers(
            received_audio=Buffer(), received_asr_result=Buffer()
        )

    def send_audio_data_for_asr(self, audio_data):
        """16kHzのPCMを受信PCMバッファへ保存し、音声認識が有効なら入力キューにも渡す。

        その後、設定されたフレーム時間の残りを待つ。
        """
        frame_sec = self.config.opus.frame_sec
        asr_input_audio_q = self.queues.asr_input_audio

        start_time = time.monotonic()
        if self.audio_trace is not None:
            self.audio_trace.write(
                "asr_input_pcm", _pcm=audio_data, _rate=16000, _wav="asr_input_16k"
            )
        self.received_pcm.append(audio_data)
        if asr_input_audio_q is not None:
            asr_input_audio_q.put(audio_data)
        elapsed = time.monotonic() - start_time
        remain = frame_sec - elapsed
        if remain > 0:
            time.sleep(remain)

    def build_pcm_chunks_from_speaker_packets(self, split_packets, sample_rate):
        """話者UUIDごとに復号器の状態を保持してOpusをPCMへ戻し、シーケンス番号から時刻を付ける。

        PCM断片の一覧と最後に処理した話者名を返す。
        """
        timed_chunks = []
        for speaker_name, packets in split_packets.items():
            opus_packets = [packet["opus_bytes"] for packet in packets]
            speaker_uuid = packets[0]["speaker_uuid"]
            decoder = self.opus_decoders_by_speaker_uuid.get(speaker_uuid)
            if decoder is None:
                decoder = av.CodecContext.create("libopus", "r")
                decoder.sample_rate = sample_rate
                self.opus_decoders_by_speaker_uuid[speaker_uuid] = decoder
            pcm = decode_opus_packets(opus_packets, sample_rate, decoder)
            if len(pcm) == 0:
                continue
            timed_chunks.append(
                {
                    "speaker_name": speaker_name,
                    "speaker_uuid": packets[0]["speaker_uuid"],
                    "captured_at_ms": packets[0]["sequence"]
                    * self.config.opus.frame_millis,
                    "pcm": pcm,
                }
            )
            self.currentspeakername = speaker_name
        return timed_chunks, speaker_name

    def convert_mixed_pcm_to_asr_input_and_queue(
        self, mixed_pcm: np.ndarray, audio_obs_sample_rate: int, resample_state
    ):
        """混合PCMを音声認識用の16kHzバイト列へ変換し、次回に引き継ぐ変換状態とともに返す。

        名前にqueueを含むが、この関数ではキューへ追加しない。
        """
        if audio_obs_sample_rate == 16000:
            pcm_bytes = mixed_pcm.tobytes()
        else:
            pcm_bytes, resample_state = audioop.ratecv(
                mixed_pcm.tobytes(),
                2,
                1,
                audio_obs_sample_rate,
                16000,
                resample_state,
            )
        return pcm_bytes, resample_state

    def build_pcm_bytes_data_from_audio_obs(
        self, audio_obs: list, last_speaker_name, resample_state
    ):
        """受信音声から自分以外の声を選び、話者別のOpus復号・時刻に沿った混合・16kHzへの変換を行う。

        PCM、最後の話者名、変換状態を返す。
        """
        self_name = self.config.name
        opus_sample_rate = self.config.opus.sample_rate

        split_packets = split_packets_by_speaker(audio_obs, self_name)
        if not split_packets:
            return None, last_speaker_name, resample_state

        timed_chunks, latest_speaker_name = self.build_pcm_chunks_from_speaker_packets(
            split_packets, opus_sample_rate
        )
        if not timed_chunks:
            return None, None, resample_state

        last_speaker_name = latest_speaker_name
        mixed_pcm = mix_timed_pcm_chunks_to_single_pcm(
            timed_chunks,
            sample_rate=opus_sample_rate,
        )
        pcm_bytes, resample_state = self.convert_mixed_pcm_to_asr_input_and_queue(
            mixed_pcm, opus_sample_rate, resample_state
        )
        return pcm_bytes, last_speaker_name, resample_state



    def put_pcm_bytes_to_asr_queue_in_chunks(self, pcm_bytes: bytes):
        """PCMを音声認識用の一定長に分割し、最後の不足分を無音で埋めて、順番に音声認識の入力処理へ渡す。"""
        asr_chunk_bytes = self.config.opus.asr_chunk_bytes
        for i in range(0, len(pcm_bytes), asr_chunk_bytes):
            chunk = pcm_bytes[i : i + asr_chunk_bytes]
            if len(chunk) < asr_chunk_bytes:
                chunk += b"\x00" * (asr_chunk_bytes - len(chunk))
            self.send_audio_data_for_asr(chunk)

    def pickup_audio_data(self, data: str) -> list:
        """WebSocket受信内容が文字列ならJSONとして読み込み、すでに解析済みならそのまま返す。"""
        if isinstance(data, str):
            data = json.loads(data)
        return data

    def save_audio_data(self, audio_packet):
        """受信した音声パケットまたは無音データを、後で取得できる受信音声バッファへ追加する。"""
        self.buffers.received_audio.append(audio_packet)

    def get_audio_data_from_minecraft(self):
        """停止要求までMinecraft音声を受信・保存し、復号したPCMを音声認識用に渡す。

        受信待ちが時間切れなら無音を供給し、終了時は話者別の復号器を解除する。
        """
        input_q = self.queues.from_websocket
        logger = self.modules.logger
        silence_chunk = self.config.opus.silence_chunk

        last_speaker_name = None
        resample_state = None
        try:
            while not self.stop_event.is_set():
                try:
                    data = input_q.get(timeout=0.1)
                    audio_packet = self.pickup_audio_data(data)
                    if self.debug_audio:
                        logger.info("audio_packet:%s", audio_packet)
                    self.save_audio_data(audio_packet)

                    pcm_bytes, last_speaker_name, resample_state = (self.build_pcm_bytes_data_from_audio_obs(audio_packet, last_speaker_name, resample_state))
                    if pcm_bytes is not None:
                        self.put_pcm_bytes_to_asr_queue_in_chunks(pcm_bytes)
                except queue.Empty:
                    self.send_audio_data_for_asr(silence_chunk)
                    self.save_audio_data(silence_chunk)
        finally:
            self.opus_decoders_by_speaker_uuid.clear()

    def get_asr_result_loop(self, input_q: queue.Queue):
        """音声認識の出力キューを読み続け、確定した文字列を取得用バッファへ保存する。

        途中結果はログだけへ記録する。
        """
        logger = self.modules.logger
        asr_result_buffer = self.buffers.received_asr_result
        while not self.stop_event.is_set():
            try:
                text, is_final = input_q.get(timeout=0.1)
            except queue.Empty:
                continue
            if is_final:
                logger.info("[ASR final] %s", text)
                asr_result_buffer.append(text)
            else:
                logger.info("[ASR partial] %s", text)

    def build_tts_voice_commands(
        self,
        chat: str,
        speaker_id: int,
    ) -> list[dict]:
        """文章をVOICEVOXで音声化してOpusへ変換し、連番付きの音声フレームと最後の停止通知を作る。

        送信自体は行わない。
        """
        tts = self.modules.tts
        opus_cfg = self.config.opus

        if tts is None:
            raise RuntimeError("TTS is disabled for this AudioMCP server")

        wav_array, _, _, _ = tts.txt_to_speech(
            chat,
            speaker_id,
        )

        encoded_packets = encode_opus_packets(
            wav_array,
            sample_rate=opus_cfg.sample_rate,
            frame_ms=opus_cfg.frame_millis,
            application=opus_cfg.application,
        )

        commands = []
        for sequence, packet in enumerate(encoded_packets):
            commands.append(
                {
                    "type": "voice_frame",
                    "sequence": sequence,
                    "opus_data_base64": base64.b64encode(packet).decode("ascii"),
                    "whispering": False,
                }
            )
        commands.append(
            {
                "type": "voice_stop",
                "last_sequence": max(0, len(encoded_packets) - 1),
            }
        )
        return commands

    async def run(self):
        """Minecraft音声の受信処理を開始し、音声認識が有効なら認識器の起動と結果の収集も並行して実行する。"""
        async with asyncio.TaskGroup() as tg:
            tg.create_task(asyncio.to_thread(self.get_audio_data_from_minecraft))
            if self.modules.asr is not None:
                tg.create_task(asyncio.to_thread(self.modules.asr.run))
                tg.create_task(
                    asyncio.to_thread(self.get_asr_result_loop, self.queues.asr_result)
                )
