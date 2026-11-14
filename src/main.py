import argparse
import asyncio
import importlib
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from mcp import Client

from modules.agent import Agent
from modules.websocketconnector import AudioWebsocketConnector
from modules.config_dataclass import (
    BUFFER_EACH_SPEECH, PLAYBACK_BUFFER_MS,
    SPEECH_RMS_THRESHOLD, SPEECH_SILENCE_MS,
)
from modules.mcp_client_request import call_mcp_tool, print_mcp_tool_list, print_mcp_resource_list
from modules.voice_mcp_server import build_voice_mcp_server
from modules.observation_action_mcp import build_observation_action_mcp_server
from modules.observation_processing_monitor import observation_processing_monitor
from modules.utils import load_primitives, read_files
from modules.config_dataclass import AgentConfig, MinecraftWorldConfig, ObservationActionConnectionConfig, OpusConfig, TTSConfig, VoiceChannel_WebsocketConfig
from modules.world_state_file import load_world_state
import runtime_settings as settings

__VERSION__ = "20261018_0630"

def build_observation_action_mcp_client(
    *,
    log_dir,
    primitives,
    connection_config: ObservationActionConnectionConfig,
    minecraft_world_config: MinecraftWorldConfig,
    should_enqueue_observations: bool = False,
):
    """観測・行動MCPを指定の接続設定で作り、起動側で管理するClientを返す。"""
    server = build_observation_action_mcp_server(
        connection_config=connection_config,
        minecraft_world_config=minecraft_world_config,
        primitives=primitives,
        log_dir=log_dir,
        should_enqueue_observations=should_enqueue_observations,
    )
    return Client(server)

def build_voice_mcp_client(
    *,
    log_dir: str,
    opus_cfg: OpusConfig,
    tts_cfg: TTSConfig,
    voicechannel_cfg: VoiceChannel_WebsocketConfig,
    voice_mode: str = "asr_tts",
):
    """音声モードに応じて共通のVoice MCPを作り、Agentへ渡すClientを返す。

    GPT-Liveでは従来のPCM・Opus・バッファ設定を保持し、ASRとTTSを無効にする。
    """
    options = {}
    if voice_mode == "gpt_live":
        # Preserve the settings previously used in the audio child process.
        log_dir = str(Path(log_dir) / "audio")
        opus_cfg = OpusConfig(48000, 20, "voip")
        tts_cfg = TTSConfig("localhost", 50021, True, 48000, False)
        options = dict(
            minecraft_voicechannel_connection=AudioWebsocketConnector(
                ws_name="minecraft_voicechannel_connection",
                host=voicechannel_cfg.host, port=voicechannel_cfg.port,
            ),
            enable_asr=False,
            enable_tts=False,
            playback_buffer_ms=PLAYBACK_BUFFER_MS,
            buffer_each_speech=BUFFER_EACH_SPEECH,
            speech_silence_ms=SPEECH_SILENCE_MS,
            speech_rms_threshold=SPEECH_RMS_THRESHOLD,
        )
    elif voice_mode != "asr_tts":
        raise ValueError("AudioMCP requires asr_tts or gpt_live mode")
    server = build_voice_mcp_server(log_dir, opus_cfg, tts_cfg, voicechannel_cfg, **options)
    return Client(server)

async def setup_observation_action_mcp(
    minecraft_mcp_client: Client, agent_cfg: AgentConfig
):
    """エージェント名を観測・行動MCPへ登録し、初期設定の応答を表示する。"""
    result = await call_mcp_tool(
        minecraft_mcp_client,
        "setup_observation_action",
        {"agent_name": agent_cfg.name},
    )
    print("========== minecraft_mcp SETUP RESULT ==========")
    print(result)
    print("agent_name:", getattr(result, "agent_name", None))
    print("initialized:", getattr(result, "initialized", None))
    print("==================================")


async def setup_voice_mcp(voice_mcp_client: Client, agent_cfg: AgentConfig):
    """共通のVoice MCPへエージェント名と種別を登録し、音声初期設定の応答を表示する。"""
    result = await call_mcp_tool(
        voice_mcp_client,
        "easy_llm_voice_setup",
        {
            "agent_type": agent_cfg.type,
            "agent_name": agent_cfg.name,
        },
    )
    print("========== voice_mcp SETUP RESULT ==========")
    print(result)
    print("is_error:", getattr(result, "is_error", None))
    print("content:", getattr(result, "content", None))
    print("==================================")


async def main(config_module):
    """設定に従って各MCPを作成・接続・初期設定し、Agentへ渡して実行する。

    音声を使う両モードで同じ音声Clientを管理し、Agent終了後に接続を閉じる。
    """
    AGENT_CFG = config_module.AGENT_CFG
    USE_AUDIO = settings.VOICE_MODE in {"asr_tts", "gpt_live"}

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    log_dir = f"{settings.BASE_LOG_DIR}/{timestamp}"
    primitives = load_primitives()
    initial_belief_data = None
    if AGENT_CFG.initial_belief is not None:
        initial_belief_path = Path(__file__).resolve().parent / AGENT_CFG.initial_belief
        initial_belief_data = load_world_state(initial_belief_path)

    minecraft_mcp_client = build_observation_action_mcp_client(
        log_dir=log_dir,
        primitives=primitives,
        connection_config=settings.MINECRAFT_INTERFACE_CONNECTION_CFG,
        minecraft_world_config=settings.MINECRAFT_WORLD_CFG,
        should_enqueue_observations=True,
    )

    if USE_AUDIO:
        voice_mcp_client = build_voice_mcp_client(
            log_dir=log_dir,
            opus_cfg=settings.OPUS_CFG,
            tts_cfg=settings.TTS_CFG,
            voicechannel_cfg=settings.VOICECHANNEL_CFG,
            voice_mode=settings.VOICE_MODE,
        )
    else:
        voice_mcp_client = None

    agent = Agent(
        log_dir=log_dir,
        decision_mode=settings.DECISION_MODE,
        decision_interval_seconds=settings.DECISION_INTERVAL_SECONDS,
        decision_threshold=settings.DECISION_THRESHOLD,
        name=AGENT_CFG.name,
        agent_type=AGENT_CFG.type,
        speaker_id=AGENT_CFG.speaker_id,
        prompts=read_files(AGENT_CFG.prompts),
        step_execution=settings.STEP_EXECUTION,
        minecraft_mcp_client=minecraft_mcp_client,
        track_other_beliefs=config_module.TRACK_OTHER_BELIEFS,
        initial_belief_data=initial_belief_data,
        voice_mcp_client=voice_mcp_client,
        llm_cfg=settings.LLM_CFG,
        voice_mode=settings.VOICE_MODE,
        voicechannel_config=settings.VOICECHANNEL_CFG,
        gptlive_settings=settings.GPT_LIVE_SETTINGS,
    )

    belief_mcp_client = agent.modules.mcp_clients.belief
    audio_context = voice_mcp_client if USE_AUDIO else nullcontext()
    monitor_context = (
        observation_processing_monitor(port=settings.OBSERVATION_MONITOR_PORT)
        if settings.ENABLE_OBSERVATION_MONITOR
        else nullcontext()
    )
    try:
        async with (
            minecraft_mcp_client,
            belief_mcp_client,
            audio_context,
            monitor_context,
        ):

            await print_mcp_tool_list(minecraft_mcp_client)
            await print_mcp_resource_list(minecraft_mcp_client)
            await setup_observation_action_mcp(minecraft_mcp_client, AGENT_CFG)

            await print_mcp_tool_list(belief_mcp_client)
            await print_mcp_resource_list(belief_mcp_client)

            if USE_AUDIO:
                await print_mcp_tool_list(voice_mcp_client)
                await setup_voice_mcp(voice_mcp_client, AGENT_CFG)

            await agent.run()

    except (EOFError, KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        agent.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-module", required=True)
    args = parser.parse_args()
    asyncio.run(main(importlib.import_module(args.config_module)))
