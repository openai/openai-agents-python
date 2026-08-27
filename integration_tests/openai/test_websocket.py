from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from agents import (
    Agent,
    ModelSettings,
    ToolCallOutputItem,
    responses_websocket_session,
)
from agents.decorators import tool
from agents.models.openai_responses import OpenAIResponsesWSModel

pytestmark = [pytest.mark.core, pytest.mark.nightly]


async def test_responses_websocket_session_reuses_a_connection_across_tool_turns(
    integration_model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    opened_connections: list[Any] = []
    original_open = OpenAIResponsesWSModel._open_websocket_connection

    async def capture_connection(
        model: OpenAIResponsesWSModel,
        url: str,
        headers: Mapping[str, str],
        *,
        connect_timeout: float | None,
    ) -> Any:
        connection = await original_open(model, url, headers, connect_timeout=connect_timeout)
        opened_connections.append(connection)
        return connection

    monkeypatch.setattr(OpenAIResponsesWSModel, "_open_websocket_connection", capture_connection)

    @tool
    def lookup_checkpoint(name: str) -> str:
        """Return a deterministic websocket checkpoint."""
        calls.append(name)
        return "WEBSOCKET:42"

    agent = Agent(
        name="Packaged Responses websocket agent",
        model=integration_model,
        instructions=(
            "When asked to check a checkpoint, call lookup_checkpoint with name='release'. "
            "For a confirmation request, reply exactly WEBSOCKET_CONFIRMED."
        ),
        tools=[lookup_checkpoint],
        model_settings=ModelSettings(max_tokens=384),
    )

    async with responses_websocket_session() as session:
        first = await session.run(
            agent,
            "Check the checkpoint and include WEBSOCKET:42 in your answer.",
        )
        second = session.run_streamed(agent, "Reply with exactly WEBSOCKET_CONFIRMED.")
        event_types = [event.type async for event in second.stream_events()]

    assert calls == ["release"]
    assert "WEBSOCKET:42" in str(first.final_output)
    assert any(isinstance(item, ToolCallOutputItem) for item in first.new_items)
    assert second.final_output == "WEBSOCKET_CONFIRMED"
    assert "raw_response_event" in event_types
    assert first.context_wrapper.usage.total_tokens > 0
    assert second.context_wrapper.usage.total_tokens > 0
    assert len(opened_connections) == 1
