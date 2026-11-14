from __future__ import annotations

import logging
from typing import Awaitable, Callable

from mcp import Client

from modules.mcp_client_request import call_mcp_tool, extract_json_object_from_tool_result
from modules.belief_store import BeliefStore


async def transfer_queued_observations(minecraft_client: Client, store: BeliefStore, agent_name: str, timeout_sec: float = 1.0,
                                       *, on_registered: Callable[[], Awaitable[None]] | None = None) -> int:
    """観測MCPのキューから観測を取得し、順番に記憶へ登録する。

    登録があれば通知処理を呼び、処理した件数を返す。
    """
    result = await call_mcp_tool(minecraft_client, "take_queued_observations", {"timeout_sec": timeout_sec})
    observations = extract_json_object_from_tool_result(result, "take_queued_observations")["observations"]
    for observation in observations:
        store.register_observation(agent_name, observation)
    if observations and on_registered is not None:
        await on_registered()
    return len(observations)


async def continuously_transfer_observations_to_belief(minecraft_client: Client, store: BeliefStore, agent_name: str,
                                                     *, on_registered: Callable[[], Awaitable[None]] | None = None) -> None:
    """観測キューから記憶への転送を繰り返す。

    転送に失敗した場合はログへ記録し、例外を呼び出し元へ伝える。
    """
    try:
        while True:
            await transfer_queued_observations(minecraft_client, store, agent_name, on_registered=on_registered)
    except Exception:
        logging.getLogger(__name__).exception("Continuous observation transfer to belief stopped")
        raise
