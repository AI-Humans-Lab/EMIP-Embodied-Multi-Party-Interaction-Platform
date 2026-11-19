from __future__ import annotations
import re
import os
import json
import math
from collections import Counter, deque
from time import perf_counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import requests
from contextlib import asynccontextmanager
import logging
import asyncio
import threading
import multiprocessing
import traceback
import audioop
from mcp import Client
from jinja2 import Template
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any, Awaitable, Callable
from modules.belief import Belief, EXCLUDE_NAMES, render_belief_snapshot
from modules.belief_store import BeliefStore
from modules.belief_inference import BeliefInference
from modules.belief_mcp import build_belief_mcp_server
from modules.world_state_file import build_initial_belief_state
from modules.llm import DECISION_MODEL, OpenAILLM
from modules.voice_mcp_bridge import AudioMCPProxy, serve_audio_mcp
from modules.voice_mcp_process import decode_base64
import modules.llm as llm
from modules.mcp_client_request import (
    call_mcp_tool,
    extract_json_object_from_tool_result,
    extract_texts_from_tool_result,
    extract_json_object_from_resource_result,
)
from modules.transfer_observations_to_belief import (
    continuously_transfer_observations_to_belief,
    transfer_queued_observations,
)
from modules.utils import make_file_logger
from modules.config_dataclass import LiveSettings, OpenAILLMConfig, VoiceChannel_WebsocketConfig




class Agent:
    @dataclass(frozen=True)
    class Config:
        name: str
        agent_type: str
        speaker_id: int
        prompts: dict[str, dict[str, str] | str]
        step_execution: bool
        voice_mode: str
        voicechannel_config: VoiceChannel_WebsocketConfig | None
        gptlive_settings: LiveSettings | None
        gpt_live_api_key: str | None = field(repr=False)
        log_directory: str
        initial_belief_data: dict | None
        other_name: str
        track_other_beliefs: bool
        decision_mode: str
        decision_interval_seconds: float
        decision_threshold: float

    @dataclass(frozen=True)
    class MCPClients:
        minecraft: Client
        belief: Client
        audio: Client | None

    @dataclass(frozen=True)
    class Modules:
        belief_store: BeliefStore | None
        llm: OpenAILLM
        mcp_clients: Agent.MCPClients
        logger: logging.Logger

    @dataclass
    class State:
        start_generation_action_event: asyncio.Event
        state_lock: threading.Lock
        closed: bool
        memory_ready: asyncio.Event
        belief_inference: BeliefInference | None
        tracked_names: set[str] = field(default_factory=set)

    @staticmethod
    def _validate_decision_configuration(
        decision_mode, decision_interval_seconds, decision_threshold, voice_mode
    ):
        """Check that the action trigger mode, decision interval, and probability threshold are valid.

        Also check compatibility with the voice mode and the API key required for API-based decisions. Raise an exception for invalid settings.
        """
        if decision_mode not in {"enter", "decision_api"}:
            raise ValueError("decision_mode must be enter or decision_api")
        if not math.isfinite(decision_interval_seconds) or decision_interval_seconds <= 0:
            raise ValueError("decision_interval_seconds must be finite and positive")
        if not 0 < decision_threshold <= 1:
            raise ValueError("decision_threshold must be in (0, 1]")
        if decision_mode == "decision_api":
            if voice_mode == "gpt_live":
                raise ValueError("GPT-Live uses delegation; select decision_mode='enter'")
            if not os.environ.get("OPENAI_API_KEY"):
                raise ValueError("decision_api requires OPENAI_API_KEY")

    @staticmethod
    def validate_configuration(
        voice_mode,
        voicechannel_config,
        voice_mcp_client,
        llm_cfg,
        gptlive_settings,
        api_key,
    ):
        """Check the voice and LLM settings for missing connection details, missing API keys, and invalid values.

        Raise an exception instead of continuing initialization if a mode or setting is unsupported.
        """
        if voice_mode not in {"gpt_live", "asr_tts", "none"}:
            raise ValueError("voice_mode must be gpt_live, asr_tts, or none")
        if voice_mode in {"gpt_live", "asr_tts"} and voice_mcp_client is None:
            raise ValueError(f"{voice_mode} requires voice_mcp_client")
        if llm_cfg.llm_type != "openai":
            raise NotImplementedError()
        if voice_mode == "gpt_live":
            if gptlive_settings.observation_interval_seconds <= 0:
                raise ValueError("observation_interval_seconds must be positive")
            if gptlive_settings.transcript_wait_seconds < 0:
                raise ValueError("transcript_wait_seconds must not be negative")
            if (
                gptlive_settings.transcript_history_limit <= 0
                or gptlive_settings.observation_character_limit <= 0
            ):
                raise ValueError(
                    "transcript_history_limit and observation_character_limit must be positive"
                )
            if (
                gptlive_settings.session_duration_seconds is not None
                and gptlive_settings.session_duration_seconds <= 0
            ):
                raise ValueError("session_duration_seconds must be None or positive")
            if not api_key:
                raise ValueError("Set OPENAI_API_KEY before creating a GPT-Live Agent.")

    def __init__(
        self,
        *,
        log_dir: str,
        name: str,
        agent_type: str,
        speaker_id: int,
        prompts: dict[str, dict[str, str] | str],
        step_execution: bool,
        minecraft_mcp_client: Client,
        voice_mcp_client: Client | None,
        llm_cfg: OpenAILLMConfig,
        track_other_beliefs: bool = False,
        initial_belief_data: dict | None = None,
        voice_mode: str = "none",
        voicechannel_config=None,
        gptlive_settings=None,
        decision_mode: str = "enter",
        decision_interval_seconds: float = 5.0,
        decision_threshold: float = 0.5,
    ):
        """Validate the agent settings and prepare components for text and action generation, service communication, and logging.

        The execution routines called by run initialize the world belief and start actions.
        """
        self._validate_decision_configuration(
            decision_mode, decision_interval_seconds, decision_threshold, voice_mode
        )
        if voice_mode == "gpt_live":
            gptlive_settings = gptlive_settings or LiveSettings()
        api_key = os.environ.get("OPENAI_API_KEY")
        self.validate_configuration(
            voice_mode,
            voicechannel_config,
            voice_mcp_client,
            llm_cfg,
            gptlive_settings,
            api_key,
        )
        llm = OpenAILLM(
            log_dir,
            llm_cfg.api_key,
            llm_cfg.model_name,
            llm_cfg.temperature,
            llm_cfg.request_timeout,
            llm_cfg.max_trial,
        )

        logger = make_file_logger(name, f"{log_dir}/{name}.log")
        config = Agent.Config(
            name=name,
            agent_type=agent_type,
            speaker_id=speaker_id,
            prompts=prompts,
            step_execution=step_execution,
            voice_mode=voice_mode,
            decision_mode=decision_mode,
            decision_interval_seconds=decision_interval_seconds,
            decision_threshold=decision_threshold,
            voicechannel_config=voicechannel_config,
            gptlive_settings=gptlive_settings,
            gpt_live_api_key=api_key,
            log_directory=log_dir,
            initial_belief_data=initial_belief_data,
            track_other_beliefs=track_other_beliefs,
            other_name=(
                gptlive_settings.other_name
                if gptlive_settings is not None
                else "Player"
            ),
        )
        belief_server = build_belief_mcp_server(
            get_store=self.get_belief_store,
            observer_name=name,
            infer_other_belief=self.infer_other_belief,
        )
        mcp_clients = Agent.MCPClients(
            minecraft=minecraft_mcp_client,
            belief=Client(belief_server),
            audio=voice_mcp_client,
        )
        modules = Agent.Modules(
            belief_store=None, llm=llm, mcp_clients=mcp_clients, logger=logger
        )
        state = Agent.State(
            start_generation_action_event=asyncio.Event(),
            state_lock=threading.Lock(),
            closed=False,
            memory_ready=asyncio.Event(),
            belief_inference=None,
        )

        self.config = config
        self.modules = modules
        self.state = state

    @property
    def name(self) -> str:
        """Return the name configured for this agent."""
        name = self.config.name
        return name

    def close(self):
        """Mark the agent as closed and clear the list of people whose beliefs are updated automatically.

        This method does not stop running tasks or close MCP connections.
        """
        state = self.state

        with state.state_lock:
            if state.closed:
                return
            state.closed = True
            state.tracked_names.clear()

    async def call_minecraft_tool(self, tool_name, arguments):
        """Ask the Minecraft action and observation service to perform the specified operation and return the result as a dictionary.

        Call an Observation-Action MCP tool and extract the JSON object from its response.
        """
        minecraft_client = self.modules.mcp_clients.minecraft
        result = await call_mcp_tool(minecraft_client, tool_name, arguments)
        return extract_json_object_from_tool_result(result, tool_name)

    async def read_minecraft_resource(self, uri: str) -> dict:
        """Retrieve information at the specified URI from the Minecraft action and observation service and return it as a dictionary.

        Use the resource-reading interface of Observation-Action MCP.
        """
        minecraft_client = self.modules.mcp_clients.minecraft
        result = await minecraft_client.read_resource(uri)
        return extract_json_object_from_resource_result(result, uri)

    def get_belief_store(self) -> BeliefStore:
        """Return the store that manages the agent's world belief and observation history.

        Raise RuntimeError if the BeliefStore has not been initialized.
        """
        store = self.modules.belief_store
        if store is None:
            raise RuntimeError("Belief store is not initialized")
        return store

    async def get_belief_tools(self, tool_names: list[str]) -> list[dict]:
        """Select belief-related operations for the LLM and return each operation's name, description, and argument definitions.

        Keep the requested order of Belief MCP tool definitions. Raise ValueError for unknown names and leave execution to call_belief_tool.
        """
        belief_client = self.modules.mcp_clients.belief
        result = await belief_client.list_tools()
        available = {tool.name: tool for tool in result.tools}
        unknown = set(tool_names) - set(available)
        if unknown:
            raise ValueError(f"Unknown Belief tools: {sorted(unknown)}")
        return [
            {
                "name": name,
                "description": available[name].description or "",
                "input_schema": available[name].input_schema,
            }
            for name in tool_names
        ]

    async def call_belief_tool(self, tool_name, arguments):
        """Ask the belief service to perform the specified operation and return the result as a dictionary.

        Call a Belief MCP tool and extract the JSON object from its response.
        """
        belief_client = self.modules.mcp_clients.belief
        result = await call_mcp_tool(belief_client, tool_name, arguments)
        return extract_json_object_from_tool_result(result, tool_name)

    async def read_belief_resource(self, uri: str) -> dict:
        """Retrieve information at the specified URI from the belief service and return it as a dictionary.

        Use the resource-reading interface of Belief MCP.
        """
        belief_client = self.modules.mcp_clients.belief
        result = await belief_client.read_resource(uri)
        return extract_json_object_from_resource_result(result, uri)

    async def initialize_belief(self, initial_belief_data: dict | None = None) -> None:
        """Use Minecraft environment information and supplied initial knowledge to prepare the agent's belief store and the mechanism for inferring other people's beliefs.

        Raise an exception if the agent is closed or its belief is already initialized. Subsequent processing imports actual observations.
        """
        name, modules, state = self.config.name, self.modules, self.state
        with state.state_lock:
            if state.closed:
                raise RuntimeError("Agent is closed")
            if state.belief_inference is not None:
                raise RuntimeError("Belief is already initialized")
        status = await self.read_minecraft_resource("minecraft-observation://status")
        empty = {
            "globalTick": int(status["initial_tick"]),
            "status": {},
            "blocks": {"__Vec3Map__": []},
            "containers": {"__Vec3Map__": []},
        }
        initial = (
            empty
            if initial_belief_data is None
            else build_initial_belief_state(
                initial_belief_data,
                agent_name=name,
                current_tick=status["initial_tick"],
                env_box=status["env_box"],
                reserved_agent_names=set(status["reserved_agent_names"]),
            )
        )
        store = BeliefStore(
            initial_state=empty,
            agent_names=[name],
            initial_belief_states={name: initial},
        )
        inference = BeliefInference(
            store, name, status["env_box"], status["visibility_distance"]
        )
        with state.state_lock:
            if state.closed:
                raise RuntimeError("Agent is closed")
            if state.belief_inference is not None:
                raise RuntimeError("Belief is already initialized")
            self.modules = replace(modules, belief_store=store)
            state.belief_inference = inference

    def _is_other_belief_target(self, target_name: str) -> bool:
        """Return whether the given name is eligible for automatic inference of another person's world belief.

        Exclude the agent itself and names that match an excluded prefix. This filter does not apply when tracking is explicitly registered by name.
        """
        return target_name != self.config.name and not any(
            target_name.startswith(prefix) for prefix in EXCLUDE_NAMES
        )

    async def infer_other_belief(self, target_name: str) -> Belief:
        """Use recorded observations to infer and return how the specified person perceives the world.

        Run inference in a separate thread. Raise an exception if the agent is closed or the inference mechanism is not initialized.
        """
        name, state = self.config.name, self.state
        inference, state_lock = state.belief_inference, state.state_lock
        with state_lock:
            if state.closed:
                raise RuntimeError(f"Agent '{name}' is closed")
        if inference is None:
            raise RuntimeError("Belief inference is not initialized")
        return await asyncio.to_thread(inference.infer_other_belief, target_name)

    async def start_belief_tracking(self, target_name: str) -> Belief:
        """Infer the specified person's world belief and register it for updates whenever new observations are recorded.

        Run inference before and after registration, then return the belief including observations that arrived during the first inference.
        """
        state = self.state
        await self.infer_other_belief(target_name)
        with state.state_lock:
            if state.closed:
                raise RuntimeError("Agent is closed")
            state.tracked_names.add(target_name)
        # Include observations that arrived during the first inference.
        return await self.infer_other_belief(target_name)

    def stop_belief_tracking(self, target_name: str) -> None:
        """Unregister the specified person from automatic belief updates.

        Keep the knowledge inferred so far.
        """
        state = self.state
        with state.state_lock:
            state.tracked_names.discard(target_name)

    async def update_tracked_beliefs(self, *, track_all: bool = False) -> None:
        """Use recorded observations to infer the beliefs of people registered for automatic updates again.

        When track_all is enabled, also update known people who meet the conditions for automatic inference.
        """
        state, store, name = self.state, self.get_belief_store(), self.config.name
        with state.state_lock:
            if state.closed:
                return
            targets = set(state.tracked_names)
        if track_all:
            targets.update(
                target_name
                for target_name in store.get_known_agent_names(name)
                if self._is_other_belief_target(target_name)
            )
        for target_name in sorted(targets):
            await self.infer_other_belief(target_name)

    async def generate_other_belief_prompts(
        self, belief_snapshot: dict, template: str
    ) -> dict[str, str]:
        """Create text describing how other people appearing in the supplied world belief perceive the world.

        Exclude the agent itself and excluded targets. Render each person's belief at the specified time using the template, and return a dictionary of names and texts.
        """
        name, store = self.config.name, self.get_belief_store()
        tick = belief_snapshot["state_dict"]["globalTick"]
        texts = {}
        for target_name in sorted(belief_snapshot["state_dict"]["status"]):
            if not self._is_other_belief_target(target_name):
                continue
            await self.infer_other_belief(target_name)
            texts[target_name] = render_belief_snapshot(
                store.get_prompt_snapshot(target_name, tick=tick),
                template,
                {"self_name": target_name, "observer_name": name},
            )
        return texts

    async def register_initial_observations(self, *, track_other_beliefs: bool = False):
        """Wait for the first observations from Minecraft and incorporate them into the agent's world belief.

        Update the relevant other people's beliefs as the observations are recorded.
        """
        minecraft_client, store, name = (
            self.modules.mcp_clients.minecraft,
            self.get_belief_store(),
            self.config.name,
        )
        # Register the first agent-visible observations before using the belief.
        observations_transferred = 0
        while observations_transferred == 0:
            observations_transferred = await transfer_queued_observations(
                minecraft_client,
                store,
                name,
                timeout_sec=30.0,
                on_registered=partial(
                    self.update_tracked_beliefs, track_all=track_other_beliefs
                ),
            )

    async def _start_belief_updates(self):
        """Initialize the agent's world belief, incorporate the first observations, and then start continuous observation updates.

        Signal readiness once the first observations are reflected and return the update task. _maintain_belief is responsible for stopping it.
        """
        initial_belief_data = self.config.initial_belief_data
        track_other_beliefs = self.config.track_other_beliefs
        minecraft = self.modules.mcp_clients.minecraft
        name = self.config.name
        memory_ready = self.state.memory_ready
        await self.initialize_belief(initial_belief_data)
        store = self.get_belief_store()
        await self.register_initial_observations(
            track_other_beliefs=track_other_beliefs
        )
        # Signal readiness only after the first observations are reflected in the belief.
        memory_ready.set()
        return asyncio.create_task(
            continuously_transfer_observations_to_belief(
                minecraft,
                store,
                name,
                on_registered=partial(
                    self.update_tracked_beliefs, track_all=track_other_beliefs
                ),
            )
        )

    @asynccontextmanager
    async def _maintain_belief(self):
        """Start the belief updates needed for actions, then stop and await the update task when its use ends.

        Provide the observation update task to the code inside the async with block.
        """
        transfer_task = await self._start_belief_updates()
        try:
            yield transfer_task
        finally:
            transfer_task.cancel()
            await asyncio.gather(transfer_task, return_exceptions=True)

    async def get_asr_result(self):
        """Retrieve new speech recognition results from the voice service and return a list of transcribed utterances."""
        voice_mcp_client, logger = (self.modules.mcp_clients.audio, self.modules.logger)
        result = await call_mcp_tool(voice_mcp_client, "get_new_asr_result", {})
        logger.info(result)
        return extract_texts_from_tool_result(result, "get_new_asr_result")

    async def enqueue_asr_transcripts(self, asr_result_list: list[str]):
        """Queue recognized utterances for incorporation into the agent's world belief.

        Register each utterance as a chat event with an empty speaker name. The observation update process applies the events.
        """
        logger, store, name = (
            self.modules.logger,
            self.get_belief_store(),
            self.config.name,
        )
        speaker = ""
        for text in asr_result_list:
            result = store.add_pending_event(
                name,
                {
                    "eventName": "chat",
                    "visible": {"agentName": speaker, "msg": text},
                    "hidden": None,
                },
            )
            logger.info(result)

    @staticmethod
    def _count_asr_utterances_since(snapshot, after_tick):
        """Count recognized utterances incorporated into the world belief after the specified time, grouped by text.

        Count chat events with an empty speaker name. Preserve the number of occurrences when the same text was spoken more than once.
        """
        return Counter(
            event["visible"]["msg"]
            for tick, events in snapshot["events"].items()
            if int(tick) > after_tick
            for event in events
            if event.get("eventName") == "chat"
            and event.get("visible", {}).get("agentName") == ""
            and "msg" in event["visible"]
        )

    async def wait_for_asr_in_belief(self, transcripts, after_tick, transfer_task):
        """Wait until all newly retrieved utterances are incorporated into the agent's world belief and return the updated snapshot.

        Match utterance texts and counts after the reference tick. Also notify the caller if the agent closes or observation updates stop or raise an exception.
        """
        expected = Counter(transcripts)
        while True:
            if self.state.closed:
                raise RuntimeError("Agent is closed")
            if transfer_task.done():
                await transfer_task  # Propagate observation-update errors or cancellation to the waiter.
                raise RuntimeError("Belief update task stopped")
            snapshot = await self.read_belief_resource("belief://snapshot")
            observed = self._count_asr_utterances_since(snapshot, after_tick)
            if expected <= observed:
                return snapshot
            await asyncio.sleep(0.1)

    async def _append_asr_and_wait_for_belief(self, transcripts, transfer_task):
        """Queue the retrieved utterances and return the agent's world belief once all of them have been incorporated.

        Record the tick before queuing so that earlier occurrences of the same text are not mistaken for the new utterances.
        """
        # Use the tick before registration to distinguish these utterances from earlier identical ones.
        after_tick = self.get_belief_store().get_observation_status(
            self.config.name
        )["current_state_tick"]
        await self.enqueue_asr_transcripts(transcripts)
        return await self.wait_for_asr_in_belief(transcripts, after_tick, transfer_task)

    async def generate_prompts(self, user_prompt, variables, belief_snapshot=None):
        """Combine the agent's world belief and supplied variables into text for the LLM and return it.

        Render the text from a template. Use the current belief if none is supplied, and include descriptions of other people's beliefs when configured.
        """
        name, template = self.config.name, self.config.prompts.get("other_belief")
        state = self.state
        store = self.get_belief_store()
        with state.state_lock:
            if state.closed:
                raise RuntimeError(f"Agent '{name}' is closed")

        if belief_snapshot is None:
            belief_snapshot = store.get_prompt_snapshot(name)
        variables = {} if variables is None else dict(variables)
        variables.setdefault("other_beliefs", {})
        if template is not None:
            variables["other_beliefs"] = await self.generate_other_belief_prompts(
                belief_snapshot, template
            )
        return render_belief_snapshot(belief_snapshot, user_prompt, variables)

    async def call_llm(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        variables: dict | None = None,
        response_parser: Callable[[str], Any] | None = None,
        response_format: dict | None = None,
        tools: list[dict] | None = None,
        tool_executor: Callable[[str, dict], Awaitable[Any]] | None = None,
        max_tool_rounds: int = 8,
        belief_snapshot: dict | None = None,
    ):
        """Build input text containing the agent's world belief, send it to the LLM, and return the generated result.

        Arguments can specify the response format, result parser, and available tools. Log the generated result and processing time.
        """
        llm = self.modules.llm
        logger = self.modules.logger
        variables = {} if variables is None else dict(variables)

        rendered_user_prompt = await self.generate_prompts(
            user_prompt, variables, belief_snapshot
        )

        prompts = llm.format_prompts_for_llm(
            [
                ("system", system_prompt),
                ("user", rendered_user_prompt),
            ]
        )

        tools = [] if tools is None else list(tools)
        generated, time_str = await llm.generate(
            prompts,
            tools=tools,
            tool_executor=tool_executor,
            response_parser=response_parser,
            response_format=response_format,
            max_tool_rounds=max_tool_rounds,
        )
        logger.info("-----------------------------------")
        logger.info("generated:%s", generated)
        logger.info("time_str:%s", time_str)
        logger.info("-----------------------------------")
        return generated

    async def generate_action_js(
        self, belief_snapshot=None, conversation_context=""
    ) -> list:
        """Ask the LLM to generate JavaScript actions for Minecraft from the world belief and conversation context.

        Return the generated code as a list of lines. This method does not execute the code.
        """
        name, prompts = (
            self.config.name,
            self.config.prompts,
        )

        action_js = await self.call_llm(
            system_prompt=prompts["system"]["primitive"],
            user_prompt=prompts["human"]["generate_action"]
            + "\n{{ conversation_context }}",
            variables={"self_name": name, "conversation_context": conversation_context},
            response_parser=llm.parse_javascript_action,
            response_format={"type": "json_object"},
            belief_snapshot=belief_snapshot,
        )

        return action_js.splitlines()

    async def _generate_actions_from_belief(self, conversation_context=""):
        """Retrieve the agent's current world belief and use it to generate and return a list of Minecraft action code lines.

        Include conversation context when supplied. Return an empty list if generation produces an empty result.
        """
        name = self.config.name
        store = self.get_belief_store()
        belief_snapshot = store.get_prompt_snapshot(name)
        return (
            await self.generate_action_js(belief_snapshot, conversation_context) or []
        )

    async def wait_user_input_loop(self, start_generation_action_event: asyncio.Event):
        """Notify the execution loop that action generation may begin each time Enter is pressed in the console.

        Stop monitoring input when standard input is closed.
        """
        name = self.config.name
        logger = self.modules.logger
        while True:
            try:
                await asyncio.to_thread(input, f"[enter: {name} acts]:")
                start_generation_action_event.set()
            except EOFError:
                logger.info(f"finish this program.")
                break

    @asynccontextmanager
    async def _listen_for_enter(self):
        """Monitor Enter input as the signal to start actions while other processing continues.

        Cancel and await the input-monitoring task when the async with block exits.
        """
        start_generation_action_event = self.state.start_generation_action_event
        input_task = asyncio.create_task(
            self.wait_user_input_loop(start_generation_action_event)
        )
        try:
            yield
        finally:
            input_task.cancel()
            await asyncio.gather(input_task, return_exceptions=True)

    async def _wait_between_actions(self, index, action_count):
        """Wait for Enter before proceeding to the next action when step-by-step execution is enabled.

        Do not wait after the final action.
        """
        step_execution = self.config.step_execution
        if step_execution and index < action_count - 1:
            await asyncio.to_thread(input, "[enter: execute next action]:")

    async def record_execution_failure(self, action_js: str, error_msg: str):
        """Log the failed action code and reason, and queue a belief record so that later action generation can refer to the failure.

        Add an event representing the agent's own thought. If the agent is closed, only log the failure without adding an event.
        """
        state = self.state
        logger, store, name = (
            self.modules.logger,
            self.get_belief_store(),
            self.config.name,
        )

        logger.warning("JS execution failed: %s", error_msg)
        with state.state_lock:
            if state.closed:
                return
        msg = (
            "I failed to act.\n"
            f"Attempted action:\n{action_js}\n"
            f"Execution error:\n{error_msg}"
        )
        store.add_pending_event(
            name,
            {
                "eventName": "think",
                "agentName": name,
                "visible": {},
                "hidden": {"msg": msg},
            },
        )

    async def record_js_execution_failure_if_needed(
        self, action_js, action_execute_result: dict
    ):
        """Record the action code and failure reason if JavaScript execution fails or returns no result.

        Add nothing on success. Use a default explanation if no reason was returned.
        """
        if action_execute_result is None:
            error_msg = "JavaScript execution returned no result."
        elif action_execute_result.get("success"):
            return
        else:
            error_msg = (
                action_execute_result.get("errorMsg")
                or "JavaScript execution failed for an unknown reason."
            )
        await self.record_execution_failure(action_js, error_msg)

    async def execute_action_js(self, code):
        """Execute the specified JavaScript code in Minecraft and return its result.

        If the returned result indicates failure, also queue a failure record for later action generation.
        """
        result = await self.call_minecraft_tool("exec_js", {"code": code})
        await self.record_js_execution_failure_if_needed(code, result)
        return result

    async def call_voice_mcp_tool_send_audio_for_minecraft(self, chat_text):
        """Ask the voice service to convert the supplied text to speech and send it to Minecraft.

        Use the speaker ID configured for the agent to select the voice.
        """
        speaker_id = self.config.speaker_id
        voice_mcp_client, logger = (self.modules.mcp_clients.audio, self.modules.logger)
        result = await call_mcp_tool(
            voice_mcp_client,
            "send_audio_data_for_minecraft",
            {
                "speech_str": chat_text,
                "speaker_id": speaker_id,
            },
        )
        logger.info(f"send_audio_for_minecraft result:{result}")

    async def send_audio_for_minecraft(self, code: str):
        """Extract chat text from JavaScript code and request that it also be spoken aloud.

        Use the first string argument to bot.chat matched by the regular expression. Do nothing if no matching call is found.
        """
        if "bot.chat(" in code:
            match = re.search(r"bot\.chat\(['\"](.*?)['\"]\)", code)
            if match:
                chat_text = match.group(1)
                await self.call_voice_mcp_tool_send_audio_for_minecraft(chat_text)

    async def _execute_actions(self, actions):
        """Execute action code in list order in Minecraft and return a list containing each code snippet and its result.

        In speech recognition and synthesis mode, also request speech for chat text. If step-by-step execution is enabled, wait for Enter before the next action.
        """
        results = []
        for index, action in enumerate(actions):
            result = await self.execute_action_js(action)
            results.append({"code": action, "result": result})
            if self.config.voice_mode == "asr_tts":
                await self.send_audio_for_minecraft(action)
            await self._wait_between_actions(index, len(actions))
        return results

    def _log_decision(self, call_id, event, **data):
        """Append a record of the action-necessity decision process to the log file with a timestamp and call identifier.

        Link related records with call_id and store one JSON object per line in JSONL format.
        """
        path = Path(self.config.log_directory) / f"{self.config.name}_decisions.jsonl"
        record = {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
                  "call_id": call_id, "event": event, "agent_name": self.config.name,
                  **data}
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.state.state_lock:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def _request_decision_logged(self, call_id, text, question):
        """Request an action-necessity decision through the LLM module, using this agent's decision logger.

        The caller runs this method in a separate thread; return the parsed API response without changing it.
        """
        return llm.request_decision(
            call_id, text, question, log_decision=self._log_decision
        )

    async def _prepare_decision_input(self, transfer_task, last_action_tick, last_results):
        """Build input for the API to assess whether action is needed, using the current world belief and previous action results.

        Wait for new speech recognition results to be incorporated when present. Return the belief snapshot, its tick, and the decision input as a JSON string, in that order.
        """
        snapshot = await self.read_belief_resource("belief://snapshot")
        if self.config.voice_mode == "asr_tts":
            transcripts = await self.get_asr_result()
            if transcripts:
                snapshot = await self._append_asr_and_wait_for_belief(
                    transcripts, transfer_task
                )
        
        tick = snapshot["state_dict"]["globalTick"]
        text = json.dumps({
            "agent_name": self.config.name,
            "current_tick": tick,
            "last_action_generation_tick": last_action_tick,
            "last_action_results": last_results,
            "belief": render_belief_snapshot(
                snapshot, self.config.prompts["human"]["generate_action"],
                {"self_name": self.config.name, "other_beliefs": {}},
            ),
        }, ensure_ascii=False, default=str)
        return snapshot, tick, text

    def _prepare_gpt_live(self):
        """Prepare components linking GPT-Live voice conversations to the agent's observations and actions, together with audio startup settings.

        Return the communication object and settings dictionary. The caller starts the separate audio process.
        """
        settings = self.config.gptlive_settings
        api_key = self.config.gpt_live_api_key
        name = self.config.name
        gpt_live_communication = GptLiveAgentCommunication(
            agent=self,
            enable_observation_updates=settings.enable_observation_updates,
            enable_delegated_actions=settings.enable_delegated_actions,
            observation_interval_seconds=settings.observation_interval_seconds,
            transcript_wait_seconds=settings.transcript_wait_seconds,
            transcript_history_limit=settings.transcript_history_limit,
            observation_character_limit=settings.observation_character_limit,
        )
        child_settings = dict(
            api_key=api_key,
            agent_name=name,
            duration=settings.session_duration_seconds,
            voice=settings.voice_name,
            interrupt_on_user_speech=settings.interrupt_on_user_speech,
            instructions=gpt_live_communication.gpt_live_instructions,
        )
        return gpt_live_communication, child_settings

    def _build_delegation_context(self, gpt_live_communication, template):
        """Combine conversation history and recent action results into text for the LLM to plan its next actions, and return it."""
        return Template(template).render(
            conversation_history=format_live_conversation(
                list(gpt_live_communication.conversation_transcript_events)
            ),
            action_results=format_live_action_results(
                list(gpt_live_communication.recent_action_results)
            ),
        )

    async def _execute_and_record_delegated_actions(self, actions, gpt_live_communication):
        """Execute actions generated for a GPT-Live request in order and save their results in history accessible to the conversation side.

        Return a list of code snippets and results. Record each result before optionally waiting for Enter, so completed records remain even if a later step raises an exception.
        """
        results = []
        for index, action in enumerate(actions):
            result = await self.execute_action_js(action)
            gpt_live_communication.remember_action_result(action, result)
            results.append({"code": action, "result": result})
            await self._wait_between_actions(index, len(actions))
        return results

    async def _run_delegated_actions(self, gpt_live_communication):
        """Receive action requests from GPT-Live, generate and execute actions from the current belief and conversation history, and return results to GPT-Live.

        Process requests one at a time. On failure, report the error and propagate the exception to the caller.
        """
        delegated_actions_template = self.config.prompts["human"]["delegated_actions"]
        try:
            async with self._maintain_belief():
                while True:
                    event = await gpt_live_communication.pending_delegations.get()
                    try:
                        await gpt_live_communication.begin_delegated_action(event)
                        conversation_context = self._build_delegation_context(
                            gpt_live_communication, delegated_actions_template
                        )
                        actions = await self._generate_actions_from_belief(
                            conversation_context
                        )
                        results = await self._execute_and_record_delegated_actions(
                            actions, gpt_live_communication
                        )
                        await gpt_live_communication.send_delegated_action_result(
                            results
                        )
                        gpt_live_communication.current_delegation_id = None
                    except asyncio.CancelledError:
                        raise
                    finally:
                        gpt_live_communication.pending_delegations.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await gpt_live_communication.report_delegated_action_error(error)
            raise
        finally:
            gpt_live_communication.current_delegation_id = None

    async def _receive_gpt_live_events(self, connection, gpt_live_communication, tasks):
        """Read notifications from the GPT-Live audio process to start tasks when ready, record conversations, and accept action requests.

        Stop receiving on a completion notification. Propagate failure notifications or a connection closing before completion as exceptions.
        """
        logger = self.modules.logger
        enable_observation_updates = (
            self.config.gptlive_settings.enable_observation_updates
        )
        started = False
        while True:
            try:
                message = await receive_process_message(connection)
            except EOFError as error:
                raise RuntimeError(
                    "Audio process pipe closed without a finished event"
                ) from error
            if message["kind"] == "ready" and not started:
                started = True
                if enable_observation_updates:
                    tasks.append(asyncio.create_task(gpt_live_communication.send_observations_periodically()))
                tasks.append(asyncio.create_task(self._run_delegated_actions(gpt_live_communication)))
            elif message["kind"] == "live_text_sent":
                logger.info(
                    "GPT-Live text sent at=%s payload=%s",
                    message["sent_at"],
                    message["payload"],
                    extra={"file_only": True},
                )
            elif message["kind"] == "live_event":
                event = message["event"]
                gpt_live_communication.remember_transcript(event)
                if event.get("type") == "session.delegation.created":
                    gpt_live_communication.enqueue_delegation(event)
            elif message["kind"] == "failed":
                raise RuntimeError("Audio process failed:\n" + message["traceback"])
            elif message["kind"] == "finished":
                return

    async def _run_on_enter_without_voice(self):
        """Without voice interaction, generate and execute Minecraft actions from the current world belief each time Enter is pressed.

        Continue incorporating Minecraft observations into the belief while waiting for input.
        """
        start_generation_action_event = self.state.start_generation_action_event
        async with self._maintain_belief(), self._listen_for_enter():
            while True:
                await start_generation_action_event.wait()
                start_generation_action_event.clear()
                actions = await self._generate_actions_from_belief()
                await self._execute_actions(actions)

    async def _run_on_enter_with_asr_tts(self):
        """Retrieve speech recognition results each time Enter is pressed, then generate and execute actions from the world belief.

        If new utterances are available, wait for them to be incorporated before generation. Also send chat text in the actions as speech.
        """
        start_generation_action_event = self.state.start_generation_action_event
        logger = self.modules.logger
        async with self._maintain_belief() as transfer_task, self._listen_for_enter():
            while True:
                await start_generation_action_event.wait()
                start_generation_action_event.clear()
                transcripts = await self.get_asr_result()
                if transcripts:
                    logger.info("asr_result:%s", transcripts)
                    snapshot = await self._append_asr_and_wait_for_belief(
                        transcripts, transfer_task
                    )
                    actions = await self.generate_action_js(snapshot) or []
                else:
                    actions = await self._generate_actions_from_belief()
                await self._execute_actions(actions)

    async def _run_with_decision_api(self):
        """Ask the API at regular intervals whether action is needed, and generate and execute actions when the probability meets or exceeds the configured threshold.

        Generate actions from the belief used for the decision. Do not start the next decision until execution finishes, then wait for the configured interval.
        """
        logger = self.modules.logger
        question = self.config.prompts["human"]["decision"]
        last_action_tick = None
        last_results = []
        async with self._maintain_belief() as transfer_task:
            while not self.state.closed:
                await asyncio.sleep(self.config.decision_interval_seconds)

                if transfer_task.done():
                    await transfer_task
                    raise RuntimeError("Belief update task stopped")
                snapshot, tick, text = await self._prepare_decision_input(
                    transfer_task, last_action_tick, last_results
                )

                started_at = perf_counter()
                try:
                    result = await asyncio.to_thread(self._request_decision_logged, uuid4().hex, text, question)
                    probability = llm.read_action_probability(result)
                except asyncio.CancelledError:
                    raise
                except (requests.RequestException, ValueError, KeyError) as error:
                    logger.warning("Decisions API failed: %s elapsed=%.3f seconds", error, perf_counter() - started_at)
                else:
                    if self.state.closed:
                        return
                    if probability >= self.config.decision_threshold:
                        # Propagate action errors and stop instead of catching them as API failures.
                        # Generate from the same snapshot used for the decision. Do not decide again during execution.
                        actions = await self.generate_action_js(snapshot) or []
                        last_action_tick = tick
                        last_results = await self._execute_actions(actions)

    async def _run_with_decision_api_debug_log(self):
        """Use the API to decide whether action is needed, generate and execute actions when needed, and log detailed inputs, outputs, and elapsed time for each stage.

        This diagnostic execution loop is not called by the normal run method.
        """
        logger = self.modules.logger
        question = self.config.prompts["human"]["decision"]
        last_action_tick = None
        last_results = []
        async with self._maintain_belief() as transfer_task:
            while not self.state.closed:
                await asyncio.sleep(self.config.decision_interval_seconds)

                if transfer_task.done():
                    await transfer_task
                    raise RuntimeError("Belief update task stopped")
                call_id = uuid4().hex
                prepared_at = perf_counter()
                transcripts = []
                snapshot = await self.read_belief_resource("belief://snapshot")
                if self.config.voice_mode == "asr_tts":
                    transcripts = await self.get_asr_result()
                    if transcripts:
                        after_tick = self.get_belief_store().get_observation_status(self.config.name)["current_state_tick"]
                        await self.enqueue_asr_transcripts(transcripts)
                        snapshot = await self.wait_for_asr_in_belief(transcripts, after_tick, transfer_task)
                tick = snapshot["state_dict"]["globalTick"]
                text = json.dumps({
                    "agent_name": self.config.name,
                    "current_tick": tick,
                    "last_action_generation_tick": last_action_tick,
                    "last_action_results": last_results,
                    "belief": render_belief_snapshot(
                        snapshot, self.config.prompts["human"]["generate_action"],
                        {"self_name": self.config.name, "other_beliefs": {}},
                    ),
                }, ensure_ascii=False, default=str)
                logger.info("Decisions API input model=%s question=%s input=%s", DECISION_MODEL, question, text)
                self._log_decision(
                    call_id, "context", current_tick=tick,
                    last_action_generation_tick=last_action_tick,
                    voice_mode=self.config.voice_mode,
                    interval_seconds=self.config.decision_interval_seconds,
                    threshold=self.config.decision_threshold, asr_transcripts=transcripts,
                    belief_preparation_seconds=perf_counter() - prepared_at,
                    belief_snapshot=snapshot,
                )
                started_at = perf_counter()
                try:
                    result = await asyncio.to_thread(self._request_decision_logged, call_id, text, question)
                    logger.info("Decisions API output=%s elapsed=%.3f seconds", json.dumps(result, ensure_ascii=False), perf_counter() - started_at)
                    probability = llm.read_action_probability(result)
                except asyncio.CancelledError:
                    self._log_decision(call_id, "cancelled", phase="awaiting_decision", elapsed_seconds=perf_counter() - started_at)
                    raise
                except (requests.RequestException, ValueError, KeyError) as error:
                    self._log_decision(call_id, "decision_error", error_type=type(error).__name__, message=str(error), elapsed_seconds=perf_counter() - started_at)
                    logger.warning("Decisions API failed: %s elapsed=%.3f seconds", error, perf_counter() - started_at)
                else:
                    self._log_decision(call_id, "decision", response=result,
                                       probability=probability, threshold=self.config.decision_threshold,
                                       should_generate=probability >= self.config.decision_threshold,
                                       elapsed_seconds=perf_counter() - started_at)
                    if self.state.closed:
                        self._log_decision(call_id, "cancelled", phase="before_generation")
                        return
                    if probability >= self.config.decision_threshold:
                        # Generate from the same snapshot used for the decision. Wait for execution before the next decision.
                        action_started = perf_counter()
                        phase = "generation"
                        try:
                            self._log_decision(call_id, "generation_started", tick=tick)
                            actions = await self.generate_action_js(snapshot) or []
                            self._log_decision(call_id, "generation_completed", actions=actions, elapsed_seconds=perf_counter() - action_started)
                            last_action_tick = tick
                            last_results = []
                            for index, action in enumerate(actions):
                                phase = "execution"
                                execution_started = perf_counter()
                                self._log_decision(call_id, "action_started", index=index, code=action)
                                result = await self.execute_action_js(action)
                                last_results.append({"code": action, "result": result})
                                self._log_decision(call_id, "action_completed", index=index,
                                                   result=result, elapsed_seconds=perf_counter() - execution_started)
                                if self.config.voice_mode == "asr_tts":
                                    phase = "tts"
                                    await self.send_audio_for_minecraft(action)
                                    self._log_decision(call_id, "tts_completed", index=index)
                                phase = "step_wait"
                                await self._wait_between_actions(index, len(actions))
                            self._log_decision(call_id, "actions_completed", elapsed_seconds=perf_counter() - action_started)
                        except asyncio.CancelledError:
                            self._log_decision(call_id, "cancelled", phase=phase)
                            raise
                        except Exception as error:
                            self._log_decision(call_id, "action_error", phase=phase, error_type=type(error).__name__, message=str(error))
                            raise

    async def _run_with_gpt_live(self):
        """Start the integration of GPT-Live voice conversations and action requests, and manage the execution and shutdown of related processing.

        Start a separate audio process and monitor notification reception and action processing. Stop related tasks and the audio process on exit.
        """
        gpt_live_communication, child_settings = self._prepare_gpt_live()
        audio_process, connection, audio_connection = start_audio_process(child_settings)
        send_lock = threading.Lock()

        def send_message(message):
            # Use the same lock to serialize ordinary text and shutdown notifications.
            """Send a message to the separate audio process.

            Use a shared lock to prevent multiple senders from writing to the connection at the same time.
            """
            with send_lock:
                connection.send(message)

        async def send_text(payload):
            """Send text intended for GPT-Live to the separate audio process.

            Send it from a separate thread so that waiting for transmission does not block other asynchronous processing.
            """
            await asyncio.to_thread(send_message, {"kind": "live_send", "payload": payload})

        gpt_live_communication.send_text = send_text
        tasks = []
        audio_bridge = asyncio.create_task(
            serve_audio_mcp(audio_connection, self.modules.mcp_clients.audio),
            name="AudioMCP中継",
        )
        try:
            receiver = asyncio.create_task(self._receive_gpt_live_events(connection, gpt_live_communication, tasks))
            tasks.append(receiver)
            while True:
                for task in [*tasks, audio_bridge]:
                    if task.done():
                        await task
                if receiver.done():
                    break
                await asyncio.sleep(0.05)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                # Keep serving PCM output and end-of-stream during child shutdown.
                await stop_audio_process(audio_process, connection, send_message)
            finally:
                audio_bridge.cancel()
                await asyncio.gather(audio_bridge, return_exceptions=True)

    async def run(self):
        """Start action processing triggered by Enter input, API-based necessity decisions, or GPT-Live requests, according to the settings.

        Give API-based decisions priority; otherwise select the execution loop for the configured voice mode.
        """
        voice_mode, decision_mode = self.config.voice_mode, self.config.decision_mode
        if decision_mode == "decision_api":
            await self._run_with_decision_api()
        elif voice_mode == "gpt_live":
            await self._run_with_gpt_live()
        elif voice_mode == "asr_tts":
            await self._run_on_enter_with_asr_tts()
        else:
            await self._run_on_enter_without_voice()

# GPT-Live conversation context and delegated-action history

class GptLiveAgentCommunication:

    def __init__(
        self,
        *,
        agent,
        enable_observation_updates=True,
        enable_delegated_actions=True,
        observation_interval_seconds=5.0,
        transcript_wait_seconds=0.5,
        transcript_history_limit=400,
        observation_character_limit=4000,
    ):
        """Prepare conversation history, a delegation queue, and action-result history linking the Agent to GPT-Live, and build instructions containing names and the initial policy."""
        name = agent.config.name
        prompts = agent.config.prompts
        other_name = agent.config.other_name
        self.agent = agent
        self.enable_observation_updates = enable_observation_updates
        self.enable_delegated_actions = enable_delegated_actions
        self.observation_interval_seconds = observation_interval_seconds
        self.transcript_wait_seconds = transcript_wait_seconds
        self.observation_character_limit = observation_character_limit
        self.conversation_transcript_events = deque(maxlen=transcript_history_limit)
        self.recent_action_results = deque(maxlen=30)
        self.pending_delegations = asyncio.Queue()
        self.received_delegation_ids = set()
        self.current_delegation_id = None
        self.send_text = None
        self.current_delegation_event = None
        self.gpt_live_instructions = prompts["system"]["gpt_live"].replace(
            "{{ agent_name }}", name
        )

        _, initial_policy = separate_observation_and_policy(
            prompts["human"]["generate_action"]
        )
        if initial_policy:
            self.gpt_live_instructions += "\n\n" + Template(initial_policy).render(
                self_name=name, other_name=other_name
            )

        if not enable_delegated_actions:
            self.gpt_live_instructions += (
                "\n\n現在の設定ではMinecraftの行動実行は無効です。"
                "バックエンドへ行動を委譲せず、実行中・完了と説明しないでください。"
                "行動を依頼された場合は、この設定では実行できないと伝えてください。"
            )

    def remember_transcript(self, live_event):
        """Save user and GPT-Live transcript events in conversation history for subsequent action generation."""
        event_type = live_event.get("type", "")
        if event_type in (
            "session.input_transcript.delta",
            "session.output_transcript.delta",
        ):
            self.conversation_transcript_events.append(dict(live_event))

    def enqueue_delegation(self, live_event):
        """Queue client-directed action requests from GPT-Live.
        
        Ignore requests when delegation is disabled or the same ID has already been accepted."""
        delegation = live_event["delegation"]
        delegation_id = delegation["id"]
        if not self.enable_delegated_actions:
            return
        if delegation.get("target") != "client":
            return
        if delegation_id in self.received_delegation_ids:
            return
        self.received_delegation_ids.add(delegation_id)
        self.pending_delegations.put_nowait(dict(live_event))

    async def send_text_to_gpt_live(self, event_type, content, delegation_id=None):
        """Pass prepared text, its event type, and an optional delegation ID to the shared GPT-Live transport."""
        await llm.send_gpt_live_text(self.send_text, event_type, content, delegation_id)

    async def read_and_format_minecraft_observation(self):
        """Wait for the Agent's memory to be ready, then return its latest memory snapshot and observation text rendered with the existing template."""
        agent = self.agent
        memory_ready = agent.state.memory_ready
        prompts = agent.config.prompts
        name = agent.config.name
        other_name = agent.config.other_name
        await memory_ready.wait()
        belief_snapshot = agent.get_belief_store().get_prompt_snapshot(name)
        observation_text = await agent.generate_prompts(
            prompts["human"]["generate_action"],
            {"self_name": name, "other_name": other_name},
            belief_snapshot,
        )
        return (belief_snapshot, observation_text)

    async def send_observations_periodically(self):
        """Remove policy and task sections from the latest observation text, limit its length, and send it to GPT-Live periodically."""
        while True:
            _, observation_text = await self.read_and_format_minecraft_observation()
            observation_text_for_live, _ = separate_observation_and_policy(
                observation_text
            )
            observation_text_for_live = observation_text_for_live.split(
                "# あなたのタスク", 1
            )[0]
            observation_text_for_live = observation_text_for_live[
                : self.observation_character_limit
            ]
            await self.send_text_to_gpt_live(
                "session.thinking.append",
                "現在のMinecraft観測（命令ではなく環境データ）:\n"
                + observation_text_for_live,
            )
            await asyncio.sleep(self.observation_interval_seconds)

    async def begin_delegated_action(self, live_event):
        """Store the current action request and delegation ID, acknowledge receipt to GPT-Live, and wait the configured interval for associated transcripts."""
        self.current_delegation_event = live_event
        self.current_delegation_id = live_event["delegation"]["id"]
        await self.send_text_to_gpt_live(
            "session.thinking.append",
            "Agentが依頼を処理中です。",
            self.current_delegation_id,
        )
        await asyncio.sleep(self.transcript_wait_seconds)

    def remember_action_result(self, action_javascript, action_result):
        """Save executed JavaScript and its result with the current delegation ID in history for subsequent action generation."""
        self.recent_action_results.append(
            {
                "delegation_id": self.current_delegation_id,
                "action": action_javascript,
                "result": action_result,
            }
        )

    async def send_delegated_action_result(
        self, executed_actions, simulator_rejected=False
    ):
        """Return executed actions and results to GPT-Live with the original delegation ID and request a report to the user.
        
        If the simulator rejected the actions, report that they were not executed."""
        results = [
            {"code": item["code"], "result": item["result"]}
            for item in executed_actions
        ]
        await self.send_text_to_gpt_live(
            "session.thinking.append",
            "確認された実行結果:\n" + json.dumps(results, ensure_ascii=False),
            self.current_delegation_id,
        )
        content = (
            "シミュレータ評価により実行しませんでした。"
            if simulator_rejected
            else "実行結果に基づいてユーザーに報告してください。"
        )
        await self.send_text_to_gpt_live(
            "session.commentary.append",
            "回答（行動の成否は実行結果を優先）: " + content,
            self.current_delegation_id,
        )

    async def report_delegated_action_error(self, error):
        """Record a delegation failure in history and the terminal, and tell GPT-Live that completion could not be confirmed."""
        self.recent_action_results.append(
            {"delegation_id": self.current_delegation_id, "error": str(error)}
        )
        print(
            f"Agent error [{self.current_delegation_id}]: {type(error).__name__}: {error}",
            flush=True,
        )
        await self.send_text_to_gpt_live(
            "session.commentary.append",
            "Agentの処理に失敗しました。完了を確認できません。",
            self.current_delegation_id,
        )


def separate_observation_and_policy(text):
    """Extract the Markdown goal and action-policy sections and return the remaining observation text separately from the policy text."""
    sections = re.compile(
        r"^##[ \t]+(?:目的|行動方針)[ \t]*\r?$.*?(?=^#{1,6}[ \t]+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    return sections.sub("", text), "\n".join(sections.findall(text)).strip()


def format_live_conversation(transcript_events):
    """Group transcript fragments by speaker, join fragments separated by at most one second, and format utterances in start-time order."""
    utterances = []
    for event_type, speaker in (
        ("session.input_transcript.delta", "ユーザー"),
        ("session.output_transcript.delta", "GPT-Live"),
    ):
        current = None
        for event in sorted(
            (event for event in transcript_events if event.get("type") == event_type),
            key=lambda event: event["start_ms"],
        ):
            text = event.get("delta", "")
            if not text:
                continue
            start, end = event["start_ms"], event["end_ms"]
            if current is not None and start - current["end"] <= 1000:
                current["text"] += text
                current["end"] = max(current["end"], end)
            else:
                current = dict(speaker=speaker, start=start, end=end, text=text)
                utterances.append(current)
    utterances.sort(key=lambda utterance: utterance["start"])
    return (
        "\n".join(
            f"[{item['start'] / 1000:.1f}–{item['end'] / 1000:.1f}秒] {item['speaker']}: {item['text'].strip()}"
            for item in utterances
            if item["text"].strip()
        )
        or "会話の記録はありません。"
    )


def format_live_action_results(action_results):
    """Format action code and results or processing errors from history as text for action generation."""
    entries = []
    for index, item in enumerate(action_results, 1):
        if "error" in item:
            entries.append(f"{index}. 処理エラー: {item['error']}")
        else:
            entries.append(
                f"{index}. 行動: {item['action']}\n   実行結果: "
                + json.dumps(item["result"], ensure_ascii=False)
            )
    return "\n".join(entries) or "実行結果の記録はありません。"


# Minecraft audio process and its lifecycle

def start_audio_process(settings):
    """Start the audio subprocess and return the process together with the parent endpoints for events and AudioMCP requests."""
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe()
    parent_audio_connection, child_audio_connection = context.Pipe()
    audio_process = context.Process(
        target=audio_process_entry,
        args=(child_connection, child_audio_connection, settings),
        name="Minecraft-GPT-Live-Audio",
    )
    try:
        audio_process.start()
    except BaseException:
        parent_connection.close()
        parent_audio_connection.close()
        raise
    finally:
        child_connection.close()
        child_audio_connection.close()
    return audio_process, parent_connection, parent_audio_connection


async def stop_audio_process(audio_process, connection, send_message):
    """Request shutdown of the audio subprocess and wait for it to stop.
    
    Terminate it if it does not stop, and close the parent pipe in all cases."""
    try:
        if audio_process.is_alive():
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(send_message, {"kind": "shutdown"}), 2
                )
            except (BrokenPipeError, EOFError, OSError, TimeoutError):
                pass
            await asyncio.to_thread(audio_process.join, 20)
        if audio_process.is_alive():
            audio_process.terminate()
            await asyncio.to_thread(audio_process.join, 5)
    finally:
        connection.close()


async def receive_process_message(connection):
    """Wait asynchronously for a message on the parent-child pipe and receive it.
    
    Poll at short intervals so the wait can be cancelled."""
    while not await asyncio.to_thread(connection.poll, 0.1):
        await asyncio.sleep(0)
    return connection.recv()


class AudioProcessEventRelay:
    """Child: forwards non-audio Live events; applies parent text updates."""

    def __init__(self, connection, instructions):
        """Keep the event queue, parent pipe, and instructions to send to GPT-Live."""
        self.connection = connection
        self.gpt_live_instructions = instructions
        self.pending_events = asyncio.Queue()

    def forward_live_event(self, event):
        """Queue a GPT-Live event for the parent.
        
        Exclude audio payloads because they use a separate path."""
        if event.get("type") != "session.output_audio.delta":
            self.pending_events.put_nowait({"kind": "live_event", "event": event})

    def record_sent_text(self, payload):
        """Queue sent text and its timestamp so the parent Agent can save them in its log."""
        self.pending_events.put_nowait({
            "kind": "live_text_sent",
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        })

    def start_forwarding_tasks(self, websocket):
        """Notify the parent that audio communication is ready, then start event forwarding and parent text reception tasks."""
        self.pending_events.put_nowait({"kind": "ready"})
        return [
            asyncio.create_task(self.send_events_to_agent(), name="会話イベント通知"),
            asyncio.create_task(
                self.receive_agent_updates(websocket), name="行動結果受信"
            ),
        ]

    async def send_events_to_agent(self):
        """Take queued events in order and keep sending them to the parent Agent through the pipe."""
        while True:
            event = await self.pending_events.get()
            await asyncio.to_thread(self.connection.send, event)

    async def receive_agent_updates(self, websocket):
        """Send text received from the parent to GPT-Live and record completion.
        
        Cancel this task on a shutdown request to begin cleanup."""
        while True:
            message = await receive_process_message(self.connection)
            if message["kind"] == "shutdown":
                raise asyncio.CancelledError
            if message["kind"] != "live_send":
                raise ValueError("Unexpected parent process message")
            await websocket.send(message["payload"])
            self.record_sent_text(message["payload"])


async def run_audio_process(connection, audio_connection, settings):
    """Prepare a proxy for the parent's AudioMCP and a GPT-Live connection, then relay audio in both directions with Minecraft.
    
    Do not create or initialize an MCP in the child process."""
    async with AudioMCPProxy(audio_connection) as audio_client:
        async with llm.open_gpt_live_session(settings["api_key"]) as session:
            await relay_audio_between_minecraft_and_gpt_live(
                audio_client,
                session,
                agent_name=settings["agent_name"],
                session_duration_seconds=settings["duration"],
                voice_name=settings["voice"],
                interrupt_on_user_speech=settings["interrupt_on_user_speech"],
                event_relay=AudioProcessEventRelay(
                    connection, settings["instructions"]
                ),
            )


def audio_process_entry(connection, audio_connection, settings):
    """Run asynchronous work at the audio subprocess entry point.
    
    Notify the parent of failure and completion, then close the event and AudioMCP pipes."""
    try:
        asyncio.run(run_audio_process(connection, audio_connection, settings))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except BaseException:
        connection.send({"kind": "failed", "traceback": traceback.format_exc()})
    finally:
        try:
            connection.send({"kind": "finished"})
        except (BrokenPipeError, EOFError, OSError):
            pass
        connection.close()
        audio_connection.close()


async def call_audio_mcp_tool(audio_mcp_client, tool_name, **arguments):
    """Call an audio MCP tool and extract a packet list for audio retrieval or a JSON object for other tools."""
    audio_tool_result = await call_mcp_tool(audio_mcp_client, tool_name, arguments)
    if tool_name in ("get_new_audio", "get_all_audio"):
        return [json.loads(block.text) for block in audio_tool_result.content]
    return extract_json_object_from_tool_result(audio_tool_result, tool_name)


async def relay_audio_between_minecraft_and_gpt_live(
    audio_mcp_client,
    gpt_live_session,
    *,
    agent_name,
    session_duration_seconds,
    voice_name="marin",
    interrupt_on_user_speech=True,
    event_relay=None,
):
    """Start a GPT-Live session and run Minecraft audio input, GPT response reception, and Minecraft audio output concurrently.
    
    Hand off received audio before collecting all tasks on exit.

    Pass instructions and the existing synchronous text-recording callback explicitly to the LLM session.
    """
    start_options = {"voice_name": voice_name}
    if event_relay is not None:
        start_options.update(
            instructions=event_relay.gpt_live_instructions,
            on_text_sent=event_relay.record_sent_text,
        )
    await gpt_live_session.start(**start_options)
    print("GPT-Live ready. Speak in Minecraft. Audio transport is active.", flush=True)
    print(
        "実行時間:",
        session_duration_seconds if session_duration_seconds is not None else "無制限",
        flush=True,
    )
    pending_playback_chunks = asyncio.Queue()
    queued_output_bytes = 0
    live_session_closed = asyncio.Event()
    playback_id = 0

    async def send_minecraft_audio_to_gpt_live():
        """Convert 16 kHz PCM from the audio MCP to 24 kHz and send it to GPT-Live in paced 20 ms frames.
        
        Preserve resampler state across audio chunks."""
        input_resampler_state = None
        pending_input_pcm24k = bytearray()
        next_input_send_s = None
        while True:
            input_audio_packets = await call_audio_mcp_tool(
                audio_mcp_client, "get_new_audio", timeout_sec=0.5, pcm=True
            )
            for input_audio_packet in input_audio_packets:
                pcm_bytes = decode_base64(input_audio_packet["pcm_data_base64"])
                resampled_pcm24k, input_resampler_state = audioop.ratecv(
                    pcm_bytes, 2, 1, 16000, 24000, input_resampler_state
                )
                pending_input_pcm24k.extend(resampled_pcm24k)
                while len(pending_input_pcm24k) >= 960:
                    input_pcm24k_frame = bytes(pending_input_pcm24k[:960])
                    del pending_input_pcm24k[:960]
                    now_s = perf_counter()
                    frame_duration_s = len(input_pcm24k_frame) / (24000 * 2)
                    if (
                        next_input_send_s is None
                        or now_s - next_input_send_s >= frame_duration_s
                    ):
                        next_input_send_s = now_s
                    wait_s = next_input_send_s - now_s
                    if wait_s > 0:
                        await asyncio.sleep(wait_s)
                    await gpt_live_session.send_input_audio(input_pcm24k_frame)
                    next_input_send_s += frame_duration_s
                    await asyncio.sleep(0)

    async def discard_audio_on_user_transcript(live_event):
        """When speech interruption is enabled, discard unsent audio on a user transcript, request an audio MCP stop, and update the playback ID."""
        nonlocal playback_id, queued_output_bytes
        if not interrupt_on_user_speech or not live_event.get("delta", "").strip():
            return
        dropped_bytes = 0
        while not pending_playback_chunks.empty():
            output_pcm_base64, _ = pending_playback_chunks.get_nowait()
            dropped_bytes += len(decode_base64(output_pcm_base64))
            pending_playback_chunks.task_done()
        queued_output_bytes -= dropped_bytes
        audio_tool_result = await call_audio_mcp_tool(
            audio_mcp_client, "stop_pcm_audio_for_minecraft"
        )
        playback_id = audio_tool_result["playback_id"]

    async def receive_gpt_live_events():
        """Receive GPT-Live responses, queue audio for playback, and forward transcripts and delegations.
        
        Also detect session closure and errors."""
        nonlocal queued_output_bytes
        while True:
            live_event = await gpt_live_session.receive_event()
            event_type = live_event["type"]
            if event_relay is not None:
                event_relay.forward_live_event(live_event)
            if event_type == "session.output_audio.delta":
                pcm_bytes = decode_base64(live_event["delta"])
                audio_byte_count = len(pcm_bytes)
                queued_output_bytes += audio_byte_count
                pending_playback_chunks.put_nowait((live_event["delta"], playback_id))
            elif event_type == "session.input_transcript.delta":
                await discard_audio_on_user_transcript(live_event)
            elif event_type == "session.closed":
                live_session_closed.set()
                return
            elif event_type == "error":
                raise RuntimeError(f"GPT-Live error: {live_event.get('error')}")

    async def send_gpt_live_audio_to_minecraft():
        """Take queued GPT audio in order and pass it to the audio MCP's PCM sender with the associated playback ID."""
        nonlocal queued_output_bytes
        while True:
            output_pcm_base64, queued_playback_id = await pending_playback_chunks.get()
            try:
                await call_audio_mcp_tool(
                    audio_mcp_client,
                    "send_pcm_audio_for_minecraft",
                    pcm_base64=output_pcm_base64,
                    playback_id=queued_playback_id,
                )
                queued_output_bytes -= len(decode_base64(output_pcm_base64))
            finally:
                pending_playback_chunks.task_done()

    input_audio_task = asyncio.create_task(
        send_minecraft_audio_to_gpt_live(), name="Minecraft音声取得・GPT入力送信"
    )
    receive_events_task = asyncio.create_task(
        receive_gpt_live_events(), name="GPTイベント受信"
    )
    output_audio_task = asyncio.create_task(
        send_gpt_live_audio_to_minecraft(), name="MCP音声送信"
    )
    audio_tasks = [input_audio_task, receive_events_task, output_audio_task]
    forwarding_tasks = (
        event_relay.start_forwarding_tasks(gpt_live_session)
        if event_relay is not None
        else []
    )
    session_timeout_task = asyncio.create_task(
        asyncio.sleep(session_duration_seconds)
        if session_duration_seconds is not None
        else asyncio.Event().wait()
    )
    session_close_requested = False

    async def close_live_session_and_flush_audio():
        """Stop input audio, close GPT-Live, and hand all received audio to the audio MCP before signaling end of stream.

        After stopping the input audio task, check the event reception and output audio tasks as shutdown proceeds.
        """
        nonlocal session_close_requested
        input_audio_task.cancel()
        await asyncio.gather(input_audio_task, return_exceptions=True)
        if not live_session_closed.is_set() and (not receive_events_task.done()):
            session_close_requested = True
            await gpt_live_session.close()
            await asyncio.wait_for(asyncio.shield(receive_events_task), 15)
        if not live_session_closed.is_set():
            raise RuntimeError(
                "Live closed without session.closed; final usage is unconfirmed"
            )
        while not pending_playback_chunks.empty() or queued_output_bytes:
            if output_audio_task.done():
                await output_audio_task
                raise RuntimeError("Audio output writer stopped")
            await asyncio.sleep(0.02)
        await call_audio_mcp_tool(
            audio_mcp_client,
            "send_pcm_audio_for_minecraft",
            pcm_base64="",
            end_of_stream=True,
            playback_id=playback_id,
        )

    try:
        completed_tasks, _ = await asyncio.wait(
            [*audio_tasks, *forwarding_tasks, session_timeout_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in completed_tasks:
            if task is not session_timeout_task:
                try:
                    await task
                except Exception as exc:
                    print(
                        f"処理失敗: {task.get_name()} / {type(exc).__name__}: {exc}",
                        flush=True,
                    )
                    raise
        shutdown_reason = (
            f"設定時間（{session_duration_seconds}秒）に到達"
            if session_timeout_task in completed_tasks
            else "GPT側からsession.closedを受信"
        )
        print(f"終了理由: {shutdown_reason}", flush=True)
        for task in forwarding_tasks:
            task.cancel()
        await asyncio.gather(*forwarding_tasks, return_exceptions=True)
        await close_live_session_and_flush_audio()
    except asyncio.CancelledError:
        for task in forwarding_tasks:
            task.cancel()
        await asyncio.gather(*forwarding_tasks, return_exceptions=True)
        try:
            await close_live_session_and_flush_audio()
        except Exception as exc:
            print(f"終了処理中のエラー: {type(exc).__name__}: {exc}", flush=True)
        raise
    finally:
        for task in forwarding_tasks:
            task.cancel()
        await asyncio.gather(*forwarding_tasks, return_exceptions=True)
        if not session_close_requested and (not live_session_closed.is_set()):
            try:
                await gpt_live_session.close()
                if not receive_events_task.done():
                    await asyncio.wait_for(asyncio.shield(receive_events_task), 3)
            except Exception as exc:
                print(
                    f"接続終了処理中のエラー: {type(exc).__name__}: {exc}", flush=True
                )
        for task in [*audio_tasks, session_timeout_task]:
            task.cancel()
        await asyncio.gather(*audio_tasks, session_timeout_task, return_exceptions=True)
