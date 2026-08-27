from __future__ import annotations

import os
from typing import Any

import pytest

from agents import Agent, RunConfig, Runner, RunResult, RunResultStreaming
from agents.decorators import tool
from agents.extensions.experimental.hosted_multi_agent import (
    HostedMultiAgentConfig,
    OpenAIHostedMultiAgentModel,
    get_hosted_agent_metadata,
)
from agents.tool_context import ToolContext

pytestmark = pytest.mark.hosted


@pytest.mark.parametrize("streaming", [False, True], ids=["nonstreaming", "streaming"])
async def test_hosted_multi_agent_preserves_subagent_tool_callers(streaming: bool) -> None:
    model_name = os.environ.get("OPENAI_AGENTS_INTEGRATION_HOSTED_MODEL", "gpt-5.6-sol")
    proposals = {"alpha": 6, "beta": 8}
    callers: set[str] = set()
    call_ids: set[str] = set()

    @tool
    def inspect_proposal(ctx: ToolContext[Any], proposal: str) -> dict[str, object]:
        """Return deterministic details for one proposal."""
        metadata = get_hosted_agent_metadata(ctx)
        callers.add(metadata.agent_name if metadata else "/root")
        call_ids.add(ctx.tool_call_id)
        return {"proposal": proposal, "estimated_weeks": proposals[proposal]}

    agent = Agent(
        name="Packaged hosted coordinator",
        model=OpenAIHostedMultiAgentModel(
            model=model_name,
            config=HostedMultiAgentConfig(max_concurrent_subagents=2),
        ),
        instructions=(
            "Create two subagents. Have one inspect proposal alpha and the other inspect "
            "proposal beta. Each subagent must call inspect_proposal before you compare them."
        ),
        tools=[inspect_proposal],
    )
    result: RunResult | RunResultStreaming
    if streaming:
        streamed = Runner.run_streamed(
            agent,
            "Compare proposal alpha and proposal beta.",
            run_config=RunConfig(tracing_disabled=True),
            max_turns=6,
        )
        event_types = [event.type async for event in streamed.stream_events()]
        assert "raw_response_event" in event_types
        result = streamed
    else:
        result = await Runner.run(
            agent,
            "Compare proposal alpha and proposal beta.",
            run_config=RunConfig(tracing_disabled=True),
            max_turns=6,
        )

    assert result.final_output
    assert len(call_ids) == 2
    assert len(callers) >= 2
    assert "/root" not in callers
