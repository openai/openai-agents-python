from __future__ import annotations

import pytest

from agents import Agent, Runner, function_tool, tool_namespace
from agents.handoffs import handoff
from agents.testing import ScriptedModel

from .test_responses import get_text_message
from .testing_processor import fetch_ordered_spans


@pytest.mark.asyncio
async def test_turn_span_uses_model_visible_capability_identities() -> None:
    @function_tool
    def lookup_invoice() -> str:
        return "invoice"

    namespaced_tools = tool_namespace(
        name="billing",
        description="Billing lookup tools.",
        tools=[lookup_invoice],
    )
    billing_agent = Agent(name="Billing Review")
    billing_handoff = handoff(
        billing_agent,
        tool_name_override="escalate_to_billing",
    )
    model = ScriptedModel(emit_traces=True)
    model.extend([[get_text_message("done")]])
    agent = Agent(
        name="test_agent",
        model=model,
        tools=namespaced_tools,
        handoffs=[billing_handoff],
    )

    await Runner.run(agent, input="test")

    spans = fetch_ordered_spans()
    turn_spans = [span for span in spans if span.span_data.type == "turn"]
    assert len(turn_spans) == 1
    exported = turn_spans[0].export()
    assert exported is not None
    assert exported["span_data"]["data"]["tools"] == ["billing.lookup_invoice"]
    assert exported["span_data"]["data"]["handoffs"] == ["escalate_to_billing"]

    agent_spans = [span for span in spans if span.span_data.type == "agent"]
    assert len(agent_spans) == 1
    assert agent_spans[0].span_data.tools == ["lookup_invoice"]
    assert agent_spans[0].span_data.handoffs == ["Billing Review"]
