"""Captured routing declarations have been retaining only their named target set."""

import pytest

from _agents_codex._agent_profiles import _capture_agent_graph
from agents import Agent, DecisionHandoff, Handoff, function_tool
from agents.realtime import RealtimeAgentConfig


@pytest.mark.parametrize("callback_handoff", [False, True])
def test_decision_handoff_captures_named_targets_separately_from_static_handoffs(callback_handoff):
    billing = Agent(name="billing", instructions="Billing.")
    technical = Agent(name="technical", instructions="Technical.")
    targets = {"billing": billing, "technical": technical}
    route = DecisionHandoff(
        name="delegate", description="Transfer when needed.", question="Choose.", targets=targets
    )

    @function_tool
    def transfer(input: str) -> str:
        return input

    static = Handoff(technical, transfer) if callback_handoff else technical
    source = Agent(
        instructions="Orchestrate.",
        tools=[transfer] if callback_handoff else [],
        handoffs=[route, static],
    )
    targets.clear()
    profiles = _capture_agent_graph(source)
    assert profiles[0].handoffs == {
        "transfer" if callback_handoff else "transfer_to_technical": "agent_2"
    }
    assert profiles[0].handoff_callbacks == ({"transfer": 1} if callback_handoff else {})
    assert profiles[0].decision_handoffs == {
        "delegate": {"slot": 0, "targets": ("agent_1", "agent_2")}
    }
    assert [profile.agent for profile in profiles] == [source, billing, technical]
    assert profiles[0].declaration.tools[0].name == "delegate"


@pytest.mark.parametrize(
    "field,value", [("name", "bad name"), ("description", ""), ("question", ""), ("targets", {})]
)
def test_invalid_declaration_fails_before_native_open(field, value):
    values = {
        "name": "delegate",
        "description": "Transfer.",
        "question": "Choose.",
        "targets": {"target": Agent(instructions="Target.")},
    }
    values[field] = value
    with pytest.raises(ValueError):
        DecisionHandoff(**values)


async def test_realtime_configuration_rejects_decision_handoff_before_native_open():
    target = Agent(instructions="Target.")
    route = DecisionHandoff(
        name="delegate", description="Transfer.", question="Choose.", targets={"target": target}
    )
    source = Agent(instructions="Source.", handoffs=[route], realtime=RealtimeAgentConfig())
    with pytest.raises(ValueError, match="Realtime"):
        _capture_agent_graph(source)
