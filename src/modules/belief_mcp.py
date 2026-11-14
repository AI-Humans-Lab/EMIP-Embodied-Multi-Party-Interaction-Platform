from __future__ import annotations

from typing import Any, Awaitable, Callable, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from modules.belief import Belief
from modules.belief import render_belief_snapshot
from modules.belief_store import BeliefStore


FrameSection = Literal["agents", "events", "block_observations"]
StateSection = Literal["agents", "blocks", "containers"]
AgentField = Literal["visibility", "position", "velocity", "yaw", "pitch", "equipment", "inventory", "last_seen"]


def build_belief_mcp_server(*, get_store: Callable[[], BeliefStore], observer_name: str, infer_other_belief: Callable[[str], Awaitable[Belief]]) -> MCPServer:
    """自分の記憶と他者について推定した記憶を、MCPのリソース・検索ツールとして公開するサーバーを作る。"""
    mcp = MCPServer("Belief", instructions="Provides this agent's belief and its inferred beliefs about others. belief_of selects whose belief to read; other beliefs are estimates by this agent.")

    async def select_belief(belief_of: str | None) -> str:
        """検索対象の記憶を決める。

        省略時は自分を選び、他者の場合は推定を更新してから選択する。
        未知の名前はツールエラーにする。
        """
        store = get_store()
        if belief_of is None or belief_of == observer_name:
            return observer_name
        if not belief_of or belief_of not in store.get_known_agent_names(observer_name):
            raise ToolError(f"Unknown belief_of: {belief_of!r}")
        await infer_other_belief(belief_of)
        return belief_of

    # [関数説明]
    # 自分が現在知っている人物・ブロック・コンテナの状態をMCPリソースとして返す。
    @mcp.resource("belief://current-state", mime_type="application/json")
    def current_state() -> dict[str, Any]:
        """Current subjective world representation accumulated from registered observations."""
        return get_store().get_current_state(observer_name)

    # [関数説明]
    # 自分の記憶の初期状態・現在状態・履歴をまとめたスナップショットを返す。
    @mcp.resource("belief://snapshot", mime_type="application/json")
    def belief_snapshot() -> dict[str, Any]:
        """Current subjective world representation and saved history for simulation or prompt rendering."""
        return get_store().get_belief_snapshot(observer_name)

    # [関数説明]
    # 自分の記憶に登録された観測のtickや件数を返す。
    @mcp.resource("belief://status", mime_type="application/json")
    def belief_status() -> dict[str, Any]:
        return get_store().get_observation_status(observer_name)

    # [関数説明]
    # 選択した記憶を使ってテンプレートを展開し、プロンプトの文字列を返す。
    @mcp.tool()
    async def render_prompt(template: str, variables: dict[str, Any] | None = None, belief_of: str | None = None) -> dict[str, str]:
        """Render the selected belief; others are estimates by this agent. self_name identifies the selected person and observer_name this agent."""
        selected = await select_belief(belief_of)
        variables = {} if variables is None else dict(variables)
        variables.update(self_name=selected, observer_name=observer_name)
        return {"text": render_belief_snapshot(get_store().get_prompt_snapshot(selected), template, variables)}

    # [関数説明]
    # 次の観測登録時に反映する出来事を予約する。
    # この呼び出しだけでは現在の記憶状態を更新しない。
    @mcp.tool()
    def add_pending_event(event: dict[str, Any]) -> dict[str, Any]:
        """Keep an Agent-supplied event pending until the next new observation is registered; current state and saved history remain unchanged until then."""
        return get_store().add_pending_event(observer_name, event)

    # [関数説明]
    # 選択した記憶の観測履歴を、tick範囲・人物・項目・件数で絞り込んで返す。
    @mcp.tool()
    async def get_observation_history(from_tick: int | None = None, to_tick: int | None = None, include: list[FrameSection] | None = None,
                                agent_names: list[str] | None = None, agent_fields: list[AgentField] | None = None,
                                limit: int = 100, cursor: int | None = None, belief_of: str | None = None) -> dict[str, Any]:
        """Get selected belief observations by tick; others are estimates by this agent. agent_names and agent_fields filter their contents."""
        selected = await select_belief(belief_of)
        return get_store().get_observation_history(selected, from_tick=from_tick, to_tick=to_tick, include=include,
                                                   agent_names=agent_names, agent_fields=agent_fields, limit=limit, cursor=cursor)

    # [関数説明]
    # 選択した記憶の指定tick時点の状態を復元し、指定範囲・項目に絞って返す。
    @mcp.tool()
    async def get_state_at(tick: int, center: list[float] | None = None, radius: float | None = None,
                     include: list[StateSection] | None = None, belief_of: str | None = None) -> dict[str, Any]:
        """Reconstruct selected belief at or before tick; others are estimates by this agent. tick is the actual observation time; without radius return all known blocks and containers."""
        selected = await select_belief(belief_of)
        return get_store().get_state_at(selected, tick, center=center, radius=radius, include=include)

    # [関数説明]
    # 選択した記憶の出来事を、tick範囲・種類・主体で検索し、ページ単位で返す。
    @mcp.tool()
    async def get_events(from_tick: int | None = None, to_tick: int | None = None, event_types: list[str] | None = None,
                   actors: list[str] | None = None, limit: int = 1000, cursor: int = 0, belief_of: str | None = None) -> dict[str, Any]:
        """Get events recorded in the selected belief; others are estimates by this agent. actors filters who performed the events."""
        selected = await select_belief(belief_of)
        return get_store().get_events(selected, from_tick=from_tick, to_tick=to_tick, event_types=event_types, actors=actors, limit=limit, cursor=cursor)

    return mcp
