"""利用者が編集する実行設定。Agent情報・他者belief追跡は既存config-moduleで指定します。"""

import os
from modules.config_dataclass import LiveSettings
from modules.config_dataclass import (
    MinecraftObservationConnectionConfig,
    MinecraftWorldConfig,
    ObservationActionConnectionConfig,
    MineflayerConnectionConfig,
    OpenAILLMConfig,
    OpusConfig,
    TTSConfig,
    VoiceChannel_WebsocketConfig,
    DECISION_MODE_LIST,
    VOICE_MODE_LIST
)

BASE_LOG_DIR = "logs"
STEP_EXECUTION = False
ENABLE_OBSERVATION_MONITOR = True  # 観測モニターのみ。Agentの観測取得は維持
OBSERVATION_MONITOR_PORT = 8000  # 観測モニターTCPポート番号

# Decision 方式
DECISION_MODE = DECISION_MODE_LIST[0]  # 0:"enter", 1:"decision_api"
DECISION_INTERVAL_SECONDS = 5.0  # 判定・行動完了後から次回判定までの待ち時間
DECISION_THRESHOLD = 0.5

# 音声方式: gpt_live=直接音声対話、asr_tts=従来ASR/TTS、none=音声なし
VOICE_MODE = VOICE_MODE_LIST[1]  # 0:"none", 1:"asr_tts", 2:"gpt_live"

# 観測範囲: env_boxは最小/最大座標、visibility_distanceはブロック数。
MINECRAFT_WORLD_CFG = MinecraftWorldConfig(
    env_box=[[-20, -64, -20], [20, -60, 20]],
    visibility_distance=20.0,
)

MINECRAFT_INTERFACE_CONNECTION_CFG = ObservationActionConnectionConfig(
    mineflayer=MineflayerConnectionConfig(
        host="beliefnestjs",
        port=3000,
    ),
    observation=MinecraftObservationConnectionConfig(
        host="minecraft",
        port=7891
    ),
)

# 行動生成用LLM（GPT-Liveのモデルや音声設定ではありません）。
# request_timeoutは1要求の秒数、max_trialは最大試行回数。
LLM_CFG = OpenAILLMConfig(
    llm_type="openai",
    api_key=os.environ.get("OPENAI_API_KEY", ""),
    model_name="gpt-5.2",
    temperature=0.7,
    request_timeout=120,
    max_trial=3,
)

# 音声Modが接続する待受。gpt_live / asr_tts共通。hostはPythonの待受アドレス。
VOICECHANNEL_CFG = VoiceChannel_WebsocketConfig(host="0.0.0.0", port=8765)

# 以下のOpus/TTS設定はasr_tts用。Live音声のPCM仕様は変更しません。
OPUS_CFG = OpusConfig(
    sample_rate=48000,
    frame_millis=20,
    application="voip",
)

TTS_CFG = TTSConfig(
    host="voicevox_engine",
    port=50021,
    enable_interrogative_upspeak=True,
    output_sampling_rate=48000,
    output_stereo=False,
)


# GPT-Liveの機能・周期。以下の値は別の設定モジュールから上書きしません。
GPT_LIVE_SETTINGS = LiveSettings(
    enable_observation_updates=True,  # Liveへの定期観測送信。Agentのbelief更新自体は維持
    enable_delegated_actions=True,  # Live委譲による行動実行
    observation_interval_seconds=5.0,  # 観測送信完了後の待機秒数
    transcript_wait_seconds=0.5,  # 委譲後に文字起こしの到着を待つ秒数
    transcript_history_limit=400,  # 会話に保持する文字起こし断片数
    observation_character_limit=4000,  # 1回に渡す観測の最大文字数
    session_duration_seconds=None,  # 実行秒数。Noneなら時間制限なし
    voice_name="marin",  # GPT-Liveの声
    interrupt_on_user_speech=True,  # 入力文字起こしを契機に未送信音声を破棄
    other_name="kou_terao",  # 相手名（目的・行動方針のテンプレート展開）
)
