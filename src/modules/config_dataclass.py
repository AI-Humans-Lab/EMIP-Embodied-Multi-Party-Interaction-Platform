from dataclasses import dataclass

@dataclass(frozen=True)
class MinecraftWorldConfig:
    env_box: list[list[int]]
    visibility_distance: float = 20.0

@dataclass(frozen=True)
class MineflayerConnectionConfig:
    host: str
    port: int

@dataclass(frozen=True)
class OpenAILLMConfig:
    llm_type: str
    api_key: str
    model_name: str
    temperature: float
    request_timeout: int
    max_trial: int

@dataclass(frozen=True)
class AgentConfig:
    name: str
    type: str
    speaker_id: int
    prompts: dict
    initial_belief: str | None = None

@dataclass(frozen=True)
class MinecraftObservationConnectionConfig:
    host:str
    port:int

@dataclass(frozen=True)
class ObservationActionConnectionConfig:
    mineflayer: MineflayerConnectionConfig
    observation: MinecraftObservationConnectionConfig

@dataclass(frozen=True)
class VoiceChannel_WebsocketConfig:
    host:str
    port:int

@dataclass(frozen=True)
class OpusConfig:
    sample_rate: int
    frame_millis: int
    application: str

    @property
    def frame_sec(self) -> float:
        """設定された音声フレームの長さを、ミリ秒から秒へ換算して返す。"""
        return self.frame_millis / 1000.0

    @property
    def asr_chunk_bytes(self) -> int:
        """音声認識へ渡す1チャンクのバイト数を、16kHz・16bit・モノラルのPCMとして計算する。"""
        asr_sample_rate = 16000
        bytes_per_sample = 2  # int16
        return (
            asr_sample_rate
            * self.frame_millis
            // 1000
            * bytes_per_sample
        )

    @property
    def silence_chunk(self) -> bytes:
        """音声認識用の1チャンクと同じ長さの、無音のPCMバイト列を作る。"""
        return b"\x00" * self.asr_chunk_bytes

@dataclass(frozen=True)
class TTSConfig:
    host: str
    port: int
    enable_interrogative_upspeak: bool
    output_sampling_rate: int
    output_stereo: bool

DECISION_MODE_LIST = ["enter", "decision_api"]
VOICE_MODE_LIST = ["none", "asr_tts", "gpt_live"]


@dataclass
class LiveSettings:
    """GPT-Liveの取り付け設定。既存AgentのLLMモデルや接続先は変更しない。"""

    enable_observation_updates: "bool" = True
    enable_delegated_actions: "bool" = True
    observation_interval_seconds: "float" = 5.0
    transcript_wait_seconds: "float" = 0.5
    transcript_history_limit: "int" = 400
    observation_character_limit: "int" = 4000
    session_duration_seconds: "float | None" = None
    voice_name: "str" = "marin"
    interrupt_on_user_speech: "bool" = False
    other_name: "str" = "Player"


BUFFER_EACH_SPEECH = True
SPEECH_SILENCE_MS = 500
SPEECH_RMS_THRESHOLD = 200
PLAYBACK_BUFFER_MS = 0
