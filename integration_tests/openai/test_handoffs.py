from __future__ import annotations

import pytest

from agents import (
    Agent,
    RunConfig,
    Runner,
    RunResult,
    RunResultStreaming,
    ToolCallItem,
    ToolCallOutputItem,
    handoff,
)
from agents.decorators import tool
from agents.extensions.handoff_filters import remove_all_tools

pytestmark = pytest.mark.core


@pytest.mark.parametrize("streaming", [False, True], ids=["nonstreaming", "streaming"])
@pytest.mark.parametrize("nested", [False, True], ids=["flat-history", "nested-history"])
async def test_client_side_handoff_preserves_tool_ownership_and_filtered_history(
    integration_model: str, streaming: bool, nested: bool
) -> None:
    calls: list[str] = []

    @tool
    def lookup_ticket(ticket: str) -> str:
        """Return the deterministic status for a support ticket."""
        calls.append(ticket)
        return "resolved"

    specialist = Agent(
        name="Packaged support specialist",
        model=integration_model,
        instructions=(
            "Call lookup_ticket exactly once with ticket='CASE-42', then answer "
            "exactly HANDOFF_RESOLVED."
        ),
        tools=[lookup_ticket],
        model_settings={"max_tokens": 512},
    )
    coordinator = Agent(
        name="Packaged handoff coordinator",
        model=integration_model,
        instructions="Immediately transfer this support ticket to the support specialist.",
        handoffs=[handoff(specialist, input_filter=remove_all_tools)],
        model_settings={"max_tokens": 512},
    )
    config = RunConfig(tracing_disabled=True, nest_handoff_history=nested)
    result: RunResult | RunResultStreaming

    if streaming:
        streamed = Runner.run_streamed(
            coordinator, "Resolve support ticket CASE-42.", run_config=config
        )
        event_types = [event.type async for event in streamed.stream_events()]
        assert "agent_updated_stream_event" in event_types
        result = streamed
    else:
        result = await Runner.run(coordinator, "Resolve support ticket CASE-42.", run_config=config)

    assert calls == ["CASE-42"]
    assert result.final_output == "HANDOFF_RESOLVED"
    assert result.last_agent is specialist
    assert any(
        isinstance(item, ToolCallItem) and item.agent is specialist for item in result.new_items
    )
    assert any(
        isinstance(item, ToolCallOutputItem) and item.agent is specialist
        for item in result.new_items
    )


@pytest.mark.parametrize("streaming", [False, True], ids=["nonstreaming", "streaming"])
async def test_nested_agent_as_tool_runs_against_the_installed_distribution(
    integration_model: str, streaming: bool
) -> None:
    worker = Agent(
        name="Packaged nested worker",
        model=integration_model,
        instructions="Reply with exactly INNER:42.",
        model_settings={"max_tokens": 256},
    )
    coordinator = Agent(
        name="Packaged nested coordinator",
        model=integration_model,
        instructions="Call ask_worker, then reply exactly OUTER:42.",
        model_settings={"max_tokens": 384},
        tools=[
            worker.as_tool(
                tool_name="ask_worker",
                tool_description="Ask the nested worker for the deterministic answer.",
            )
        ],
    )
    config = RunConfig(tracing_disabled=True)
    result: RunResult | RunResultStreaming

    if streaming:
        streamed = Runner.run_streamed(coordinator, "Use the nested worker.", run_config=config)
        async for _event in streamed.stream_events():
            pass
        result = streamed
    else:
        result = await Runner.run(coordinator, "Use the nested worker.", run_config=config)

    assert result.final_output == "OUTER:42"
    assert any(isinstance(item, ToolCallItem) for item in result.new_items)
    assert any(isinstance(item, ToolCallOutputItem) for item in result.new_items)
