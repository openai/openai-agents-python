from dataclasses import FrozenInstanceError

import pytest

from _agents_codex._agent_profiles import _capture_agent_graph
from _agents_codex.session import _split_tools
from agents import Agent, Handoff, PromptedOutput, ToolRef, function_tool


def test_callback_handoff_captures_an_unnamed_target_and_one_callback():
    @function_tool(name="route_issue", description="Issue routing")
    def route(ticket: int) -> str:
        return f"ticket={ticket}"

    target = Agent(instructions="Resolve the issue")
    transfer = Handoff(target, route)
    source = Agent(instructions="Route", tools=[route], handoffs=[transfer, transfer])
    profiles = _capture_agent_graph(source)
    assert source.tools == (route,)
    assert len(profiles) == 2
    assert profiles[0].handoffs == {"route_issue": "agent_1"}
    assert profiles[0].handoff_callbacks == {"route_issue": 0}
    assert profiles[0].declaration.tools == (route,)
    assert profiles[1].agent is target
    assert route.description == "Issue routing"
    with pytest.raises(FrozenInstanceError):
        transfer.target = source


def test_handoff_reference_keeps_its_registered_function_binding():
    @function_tool
    def route(ticket: int) -> str:
        return str(ticket)

    source = Agent(instructions="Route", handoffs=[Handoff(Agent(instructions="Resolve"), route)])
    (profile, _) = _capture_agent_graph(source)
    functions, servers, roles = _split_tools(
        [ToolRef("route")], available=profile.declaration.tools, handoffs=profile.handoffs
    )
    assert functions == (route,)
    assert servers == ()
    assert roles is None


@pytest.mark.parametrize(
    "options, message",
    [
        ({"needs_approval": True}, "without a managed approval"),
        ({"finish_turn": True}, "cannot finish the turn"),
        ({"namespace": "other"}, "default function namespace"),
    ],
)
def test_transfer_callback_rejects_incompatible_execution_modes(options, message):
    @function_tool(**options)
    def route(ticket: int) -> str:
        return str(ticket)

    with pytest.raises(ValueError, match=message):
        Handoff(Agent(instructions="Resolve"), route)


def test_callback_handoff_rejects_a_mixed_prompted_output_mode():
    @function_tool
    def route() -> str:
        return "routed"

    target = Agent(instructions="Number", output_type=PromptedOutput(int))
    source = Agent(instructions="Text", handoffs=[Handoff(target, route)])
    with pytest.raises(ValueError, match="share output mode"):
        _capture_agent_graph(source)


def test_plain_handoff_retains_its_generated_name_without_a_callback():
    source = Agent(instructions="Route", handoffs=[Agent(instructions="Resolve", name="Billing")])
    (profile, _) = _capture_agent_graph(source)
    assert profile.handoffs == {"transfer_to_billing": "agent_1"}
    assert profile.handoff_callbacks == {}
    assert source.tools == ()
