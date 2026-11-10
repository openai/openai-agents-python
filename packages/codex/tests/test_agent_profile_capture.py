"""Native Agent graph capture is immutable, cycle-safe and source-scoped."""

from dataclasses import FrozenInstanceError
from typing import Any

import pytest
from typing_extensions import assert_type

from _agents_codex._agent_profiles import _capture_agent_graph
from agents import Agent, NativeAgents, NativeOutput, NativeRole, PromptedOutput


def test_no_handoff_capture_preserves_native_defaults_and_identity():
    agent = Agent(instructions="Answer", model="model-a", model_settings={"tool_choice": "none"})
    (profile,) = _capture_agent_graph(agent)
    assert profile.agent is agent
    assert profile.declaration is not agent
    assert profile.declaration.instructions == "Answer"
    assert profile.declaration.model == "model-a"
    assert profile.declaration.model_settings == {"tool_choice": "none"}
    assert profile.handoffs == {}
    assert agent.handoffs == ()


def test_handoff_sequence_is_captured_and_output_views_require_compatible_graphs():
    target = Agent(instructions="Billing", name="Billing")
    targets = [target]
    agent = Agent(instructions="Route", handoffs=targets)
    targets.clear()
    assert agent.handoffs == (target,)
    with pytest.raises(ValueError, match="without outgoing handoffs"):
        agent.with_output_type(int)
    assert Agent(instructions="Answer").with_output_type(int).output_type is int
    with pytest.raises(FrozenInstanceError):
        agent.handoffs = ()


def test_handoff_context_types_are_preserved():
    target = Agent[dict[str, str], str](instructions="Billing", name="Billing")
    source = Agent(instructions="Route", handoffs=[target])
    assert_type(source, Agent[dict[str, str], str])


def test_cycles_and_shared_thunks_resolve_once_per_capture():
    calls = []

    def billing():
        calls.append("resolved")
        return second

    first = Agent(instructions="Route", name="Support", handoffs=[billing, billing])
    second = Agent(instructions="Bill", name="Billing", handoffs=[first, billing])
    profiles = _capture_agent_graph(first)
    assert [profile.agent for profile in profiles] == [first, second]
    assert dict(profiles[0].handoffs) == {"transfer_to_billing": profiles[1].id}
    assert dict(profiles[1].handoffs) == {
        "transfer_to_support": profiles[0].id,
        "transfer_to_billing": profiles[1].id,
    }
    assert calls == ["resolved"]
    assert _capture_agent_graph(first)[1].agent is second
    assert calls == ["resolved", "resolved"]
    with pytest.raises(TypeError):
        profiles[0].handoffs["transfer_to_other"] = "agent_2"  # type: ignore[index]


def test_handoff_names_collide_only_within_the_source_profile():
    first = Agent(instructions="One", name="Billing!")
    second = Agent(instructions="Two", name="Billing?")
    with pytest.raises(ValueError, match="Duplicate handoff name"):
        _capture_agent_graph(Agent(instructions="Both", handoffs=[first, second]))
    left = Agent(instructions="Left", name="Left", handoffs=[first])
    right = Agent(instructions="Right", name="Right", handoffs=[second])
    profiles = _capture_agent_graph(Agent(instructions="Root", handoffs=[left, right]))
    assert profiles[1].handoffs.keys() == profiles[2].handoffs.keys()
    assert profiles[3].agent is first
    assert profiles[4].agent is second


@pytest.mark.parametrize("name", [None, "", "   "])
def test_handoff_targets_require_existing_agent_names(name):
    target = Agent(instructions="Target", name=name)
    with pytest.raises(ValueError, match="nonempty Agent.name"):
        _capture_agent_graph(Agent(instructions="Route", handoffs=[target]))


def test_target_resolution_errors_and_invalid_results_fail_capture():
    error = RuntimeError("configuration failure")

    def broken():
        raise error

    with pytest.raises(RuntimeError) as captured:
        _capture_agent_graph(Agent(instructions="Route", handoffs=[broken]))
    assert captured.value is error

    def invalid() -> Any:
        return "not an Agent"

    with pytest.raises(TypeError, match="return a native Agent"):
        _capture_agent_graph(Agent(instructions="Route", handoffs=[invalid]))


def test_async_target_resolver_is_rejected_without_leaving_a_coroutine():
    async def resolve():
        return Agent(instructions="Target", name="Target")

    invalid: Any = resolve
    with pytest.raises(TypeError, match="must be synchronous"):
        _capture_agent_graph(Agent(instructions="Route", handoffs=[invalid]))


def test_invalid_handoff_declaration_is_rejected_at_construction():
    invalid: Any = ["not an Agent or resolver"]
    with pytest.raises(TypeError, match="native Agents or synchronous target resolvers"):
        Agent(instructions="Route", handoffs=invalid)


@pytest.mark.parametrize("output", [NativeOutput(str), PromptedOutput(str)])
def test_graph_rejects_incompatible_output_encoding_before_native_open(output):
    target = Agent(instructions="Target", name="Target", output_type=output)
    with pytest.raises(ValueError, match="output mode, schema and wrapping"):
        _capture_agent_graph(Agent(instructions="Route", handoffs=[target]))


def test_graph_preserves_agent_specific_retry_policy_with_shared_output_encoding():
    target = Agent(
        instructions="Target", name="Target", output_type=PromptedOutput(str, max_retries=2)
    )
    root = Agent(
        instructions="Route", output_type=PromptedOutput(str, max_retries=1), handoffs=[target]
    )
    profiles = _capture_agent_graph(root)
    assert profiles[0].declaration.output_type == PromptedOutput(str, max_retries=1)
    assert profiles[1].declaration.output_type == PromptedOutput(str, max_retries=2)


def test_graph_rejects_differing_child_roles_but_accepts_identical_configuration():
    specialist = NativeRole(name="specialist", instructions="Specialize", description="Specialist")
    roles = NativeAgents(additional_roles=[specialist])
    target = Agent(instructions="Target", name="Target", tools=[roles])
    with pytest.raises(ValueError, match="native child-role configuration"):
        _capture_agent_graph(Agent(instructions="Route", handoffs=[target]))
    root = Agent(
        instructions="Route",
        handoffs=[target],
        tools=[NativeAgents(additional_roles=[specialist])],
    )
    assert [profile.agent for profile in _capture_agent_graph(root)] == [root, target]


def test_graph_rejects_different_runtime_provider_and_code_mode():
    from agents import NativeProvider

    shared = NativeProvider(api_key="synthetic")
    other = NativeProvider(api_key="synthetic")
    target = Agent(instructions="B", name="B", provider=other)
    source = Agent(instructions="A", provider=shared, handoffs=[target])
    with pytest.raises(ValueError, match="share the Session provider"):
        _capture_agent_graph(source)
    target = Agent(instructions="B", name="B", programmatic_tool_calling=True)
    with pytest.raises(ValueError, match="programmatic"):
        _capture_agent_graph(Agent(instructions="A", handoffs=[target]))
    target = Agent(instructions="B", name="B")
    assert (
        len(_capture_agent_graph(Agent(instructions="A", provider=shared, handoffs=[target]))) == 2
    )


def test_opening_root_view_does_not_break_cyclic_original_identity():
    from dataclasses import replace

    root = Agent(instructions="Original", name="A", handoffs=[lambda: target])
    target = Agent(instructions="Target", name="B", handoffs=[lambda: root])
    view = replace(root, instructions="Captured override")
    profiles = _capture_agent_graph(root, root_declaration=view)
    assert len(profiles) == 2
    assert profiles[0].agent is root
    assert profiles[0].declaration.instructions == "Captured override"
    assert profiles[1].handoffs == {"transfer_to_a": "agent_0"}


def test_source_agent_lookup_keeps_initial_epoch_and_previous_actor_without_fallback():
    from _agents_codex.session import Session, _capture_profile_runtimes, _capture_tools
    from agents import NativeError

    target = Agent(instructions="B", name="B")
    root = Agent(instructions="A", name="A", handoffs=[target])
    profiles = _capture_agent_graph(root)
    runtime, _ = _capture_profile_runtimes(profiles, _capture_tools(0, (), None), "A", None)
    session = object.__new__(Session)
    session._profiles = runtime
    session._opening_agent = root
    session._profile_current = ("agent_1", 1)
    assert session._agent_for_profile({"profile_id": "agent_0", "epoch": 0}).instructions == "A"
    assert session._agent_for_profile({"profile_id": "agent_1", "epoch": 1}).instructions == "B"
    for invalid in (
        None,
        {},
        {"profile_id": "foreign", "epoch": 0},
        {"profile_id": "agent_0", "epoch": True},
    ):
        with pytest.raises(NativeError):
            session._agent_for_profile(invalid)
    session._profiles = {}
    assert session._agent_for_profile(None) is root
    with pytest.raises(NativeError):
        session._agent_for_profile({"profile_id": "agent_0", "epoch": 0})


async def test_equal_tool_names_do_not_transfer_operation_authority_between_profiles():
    from pydantic import BaseModel

    from _agents_codex.schema import _prepare_spec
    from _agents_codex.session import Session, _Submission
    from agents import FunctionTool
    from agents.tools import ToolRef

    class Args(BaseModel):
        key: str

    left = FunctionTool(name="lookup", description="", input_type=Args, output_type=str)
    right = FunctionTool(name="lookup", description="", input_type=Args, output_type=str)
    target = Agent(instructions="B", name="B", tools=[right])
    source = Agent(instructions="A", tools=[left], handoffs=[target])
    session = object.__new__(Session)
    session._profiles = {"agent_0": object()}
    session._executor = None
    session._native_agents = None
    session._project_guidance = False
    state = _Submission(
        _prepare_spec(str), operation_refs=(left.ref,), operation_declarations=(left,)
    )
    assert session._profile_operation_tools(state, source, source.tools) == {
        ToolRef("lookup", "functions")
    }
    assert session._profile_operation_tools(state, target, target.tools) == set()
    shared = Agent(instructions="Shared", name="Shared", tools=[left])
    assert session._profile_operation_tools(state, shared, shared.tools) == {
        ToolRef("lookup", "functions")
    }
