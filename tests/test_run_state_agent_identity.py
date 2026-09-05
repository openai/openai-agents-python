"""Agent graph identities and durable RunState ownership across save and restore."""

import json
from collections.abc import Mapping
from typing import Any, cast

import pytest

from agents import (
    Agent,
    Handoff,
    ModelSettings,
    RunContextWrapper,
    Runner,
    RunState,
    UserError,
    handoff,
)
from agents._run_state_agent_identity import (
    _build_agent_identity_map,
    _build_agent_map,
    _capability_identity_signature,
)
from agents.guardrail import GuardrailFunctionOutput, OutputGuardrail, OutputGuardrailResult
from agents.items import HandoffOutputItem, ToolCallItem
from agents.sandbox import Manifest
from agents.sandbox.capabilities.capability import Capability
from agents.testing import ScriptedModel, scripted_sandbox_session
from agents.tool import function_tool

from .test_responses import get_function_tool_call, get_handoff_tool_call
from .utils.factories import make_run_state as make_state


class _IdentityCapability(Capability):
    type: str = "identity"
    setting: str

    def __init__(self, *, setting: str) -> None:
        super().__init__(type="identity", **cast(Any, {"setting": setting}))


class TestRunState:
    @pytest.mark.asyncio
    async def test_from_json_restores_duplicate_name_current_agent_by_identity(self):
        """Duplicate agent names should round-trip through the serialized identity key."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        second = Agent(name="duplicate")
        first = Agent(name="duplicate", handoffs=[second])
        second.handoffs = [first]
        state = make_state(first, context=context, original_input="input1", max_turns=2)
        state._current_agent = second

        json_data = state.to_json()
        assert json_data["current_agent"] == {"name": "duplicate", "identity": "duplicate#2"}

        restored = await RunState.from_json(first, json_data)
        assert restored._current_agent is second

    def test_build_agent_identity_map_avoids_literal_suffix_collisions(self) -> None:
        """Literal `#<n>` names should not collide with generated duplicate identities."""
        first = Agent(name="sandbox")
        literal_suffix = Agent(name="sandbox#2")
        second = Agent(name="sandbox")
        first.handoffs = [literal_suffix, second]
        literal_suffix.handoffs = [first, second]
        second.handoffs = [first, literal_suffix]

        identity_map = _build_agent_identity_map(first)

        assert identity_map == {
            "sandbox": first,
            "sandbox#2": literal_suffix,
            "sandbox#3": second,
        }

    def test_build_agent_identity_map_is_stable_across_reordered_duplicate_agents(self) -> None:
        """Duplicate-name identities should not change when reachable order changes."""

        @function_tool(name_override="alpha_tool")
        def alpha_tool() -> str:
            return "alpha"

        @function_tool(name_override="beta_tool")
        def beta_tool() -> str:
            return "beta"

        def _identity_for(
            identity_map: Mapping[str, Agent[Any]],
            target: Agent[Any],
        ) -> str:
            return next(identity for identity, agent in identity_map.items() if agent is target)

        first_alpha = Agent(name="sandbox", instructions="Alpha", tools=[alpha_tool])
        first_beta = Agent(name="sandbox", instructions="Beta", tools=[beta_tool])
        first_root = Agent(name="triage", handoffs=[first_beta, first_alpha])
        first_alpha.handoffs = [first_root]
        first_beta.handoffs = [first_root]

        second_alpha = Agent(name="sandbox", instructions="Alpha", tools=[alpha_tool])
        second_beta = Agent(name="sandbox", instructions="Beta", tools=[beta_tool])
        second_root = Agent(name="triage", handoffs=[second_alpha, second_beta])
        second_alpha.handoffs = [second_root]
        second_beta.handoffs = [second_root]

        first_identity_map = _build_agent_identity_map(first_root)
        second_identity_map = _build_agent_identity_map(second_root)

        assert _identity_for(first_identity_map, first_alpha) == _identity_for(
            second_identity_map, second_alpha
        )
        assert _identity_for(first_identity_map, first_beta) == _identity_for(
            second_identity_map, second_beta
        )

    @pytest.mark.asyncio
    async def test_from_json_restores_duplicate_name_current_agent_with_reordered_graph(self):
        """Restore should keep the same logical duplicate agent after graph reordering."""

        @function_tool(name_override="alpha_tool")
        def alpha_tool() -> str:
            return "alpha"

        @function_tool(name_override="beta_tool")
        def beta_tool() -> str:
            return "beta"

        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        first_alpha = Agent(name="sandbox", instructions="Alpha", tools=[alpha_tool])
        first_beta = Agent(name="sandbox", instructions="Beta", tools=[beta_tool])
        first_root = Agent(name="triage", handoffs=[first_beta, first_alpha])
        first_alpha.handoffs = [first_root]
        first_beta.handoffs = [first_root]

        state = make_state(first_root, context=context, original_input="input1", max_turns=2)
        state._current_agent = first_beta
        json_data = state.to_json()

        restored_alpha = Agent(name="sandbox", instructions="Alpha", tools=[alpha_tool])
        restored_beta = Agent(name="sandbox", instructions="Beta", tools=[beta_tool])
        restored_root = Agent(name="triage", handoffs=[restored_alpha, restored_beta])
        restored_alpha.handoffs = [restored_root]
        restored_beta.handoffs = [restored_root]

        restored = await RunState.from_json(restored_root, json_data)
        assert restored._current_agent is restored_beta

    @pytest.mark.asyncio
    async def test_from_json_restores_bare_duplicate_name_current_agent_via_identity_map(self):
        """Bare duplicate names should resolve through the identity map, not traversal order."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        first = Agent(name="duplicate", instructions="zeta")
        second = Agent(name="duplicate", instructions="alpha")
        root = Agent(name="triage", handoffs=[first, second])
        first.handoffs = [root]
        second.handoffs = [root]

        state = make_state(root, context=context, original_input="input1", max_turns=2)
        state._current_agent = second

        json_data = state.to_json()
        assert json_data["current_agent"] == {"name": "duplicate"}

        restored = await RunState.from_json(root, json_data)
        assert restored._current_agent is second

    @pytest.mark.asyncio
    async def test_from_json_restores_falsy_current_agent_via_identity_map(self):
        class FalsyAgent(Agent[Any]):
            def __bool__(self) -> bool:
                return False

        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        first = Agent(name="duplicate", instructions="zeta")
        second = FalsyAgent(name="duplicate", instructions="alpha")
        root = Agent(name="triage", handoffs=[first, second])
        first.handoffs = [root]
        second.handoffs = [root]

        state = make_state(root, context=context, original_input="input1", max_turns=2)
        state._current_agent = second

        json_data = state.to_json()
        assert json_data["current_agent"] == {
            "name": "duplicate",
            "identity": "duplicate#2",
        }

        restored = await RunState.from_json(root, json_data)
        assert restored._current_agent is second

    def test_build_agent_identity_map_uses_tool_use_behavior_for_duplicate_names(self) -> None:
        """Duplicate-name identities should stay stable when only tool_use_behavior differs."""

        def _identity_for(
            identity_map: Mapping[str, Agent[Any]],
            target: Agent[Any],
        ) -> str:
            return next(identity for identity, agent in identity_map.items() if agent is target)

        first_default = Agent(
            name="sandbox",
            instructions="Shared instructions.",
            tool_use_behavior="run_llm_again",
        )
        first_stop = Agent(
            name="sandbox",
            instructions="Shared instructions.",
            tool_use_behavior="stop_on_first_tool",
        )
        first_root = Agent(name="triage", handoffs=[first_default, first_stop])
        first_default.handoffs = [first_root]
        first_stop.handoffs = [first_root]

        second_default = Agent(
            name="sandbox",
            instructions="Shared instructions.",
            tool_use_behavior="run_llm_again",
        )
        second_stop = Agent(
            name="sandbox",
            instructions="Shared instructions.",
            tool_use_behavior="stop_on_first_tool",
        )
        second_root = Agent(name="triage", handoffs=[second_stop, second_default])
        second_default.handoffs = [second_root]
        second_stop.handoffs = [second_root]

        first_identity_map = _build_agent_identity_map(first_root)
        second_identity_map = _build_agent_identity_map(second_root)

        assert _identity_for(first_identity_map, first_default) == _identity_for(
            second_identity_map, second_default
        )
        assert _identity_for(first_identity_map, first_stop) == _identity_for(
            second_identity_map, second_stop
        )

    def test_capability_identity_uses_config_but_not_bound_session(self) -> None:
        """Capability identity should consider config and ignore bound sessions."""

        first_alpha_capability = _IdentityCapability(setting="alpha")
        first_beta_capability = _IdentityCapability(setting="beta")
        first_alpha_capability.bind(
            scripted_sandbox_session(manifest=Manifest(root="/workspace/first-alpha"))
        )
        first_beta_capability.bind(
            scripted_sandbox_session(manifest=Manifest(root="/workspace/first-beta"))
        )

        second_alpha_capability = _IdentityCapability(setting="alpha")
        second_beta_capability = _IdentityCapability(setting="beta")
        second_alpha_capability.bind(
            scripted_sandbox_session(manifest=Manifest(root="/workspace/second-alpha"))
        )
        second_beta_capability.bind(
            scripted_sandbox_session(manifest=Manifest(root="/workspace/second-beta"))
        )

        first_alpha_signature = _capability_identity_signature(first_alpha_capability)
        first_beta_signature = _capability_identity_signature(first_beta_capability)
        second_alpha_signature = _capability_identity_signature(second_alpha_capability)
        second_beta_signature = _capability_identity_signature(second_beta_capability)

        assert first_alpha_signature == second_alpha_signature
        assert first_beta_signature == second_beta_signature
        assert first_alpha_signature != first_beta_signature

    @pytest.mark.asyncio
    async def test_from_json_restores_duplicate_name_current_agent_when_tool_use_behavior_differs(
        self,
    ) -> None:
        """Duplicate-name restore should stay stable when tool_use_behavior is the only delta."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        first_default = Agent(
            name="sandbox",
            instructions="Shared instructions.",
            tool_use_behavior="run_llm_again",
        )
        first_stop = Agent(
            name="sandbox",
            instructions="Shared instructions.",
            tool_use_behavior="stop_on_first_tool",
        )
        first_root = Agent(name="triage", handoffs=[first_default, first_stop])
        first_default.handoffs = [first_root]
        first_stop.handoffs = [first_root]

        state = make_state(first_root, context=context, original_input="input1", max_turns=2)
        state._current_agent = first_stop
        json_data = state.to_json()

        restored_default = Agent(
            name="sandbox",
            instructions="Shared instructions.",
            tool_use_behavior="run_llm_again",
        )
        restored_stop = Agent(
            name="sandbox",
            instructions="Shared instructions.",
            tool_use_behavior="stop_on_first_tool",
        )
        restored_root = Agent(name="triage", handoffs=[restored_stop, restored_default])
        restored_default.handoffs = [restored_root]
        restored_stop.handoffs = [restored_root]

        restored = await RunState.from_json(restored_root, json_data)
        assert restored._current_agent is restored_stop

    @pytest.mark.asyncio
    async def test_from_json_rejects_missing_saved_duplicate_identity(self):
        """Identity-aware snapshots should fail when the saved duplicate no longer exists."""
        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        second = Agent(name="duplicate", instructions="Second")
        first = Agent(name="duplicate", instructions="First", handoffs=[second])
        second.handoffs = [first]
        state = make_state(first, context=context, original_input="input1", max_turns=2)
        state._current_agent = second

        json_data = state.to_json()
        restored_root = Agent(name="duplicate", instructions="First")

        with pytest.raises(UserError, match="agent identity"):
            await RunState.from_json(restored_root, json_data)

    @pytest.mark.asyncio
    async def test_result_to_state_preserves_duplicate_name_root_and_owned_state(self):
        """RunResult.to_state should keep the root graph while preserving the active duplicate."""

        @function_tool(name_override="approval_tool", needs_approval=True)
        def approval_tool() -> str:
            return "approved"

        first_model = ScriptedModel()
        second_model = ScriptedModel()
        first = Agent(name="duplicate", model=first_model)
        second = Agent(
            name="duplicate",
            model=second_model,
            tools=[approval_tool],
            model_settings=ModelSettings(tool_choice="required"),
        )
        first.handoffs = [second]
        second.handoffs = [first]

        first_model.extend([[get_handoff_tool_call(second)]])
        second_model.extend(
            [[get_function_tool_call("approval_tool", json.dumps({}), call_id="call_approval")]]
        )

        result = await Runner.run(first, "start")
        assert result.interruptions

        state = result.to_state()
        assert state._starting_agent is first
        assert state._current_agent is second

        json_data = state.to_json()
        assert json_data["current_agent"] == {"name": "duplicate", "identity": "duplicate#2"}
        assert json_data["tool_use_tracker"]["duplicate#2"] == ["approval_tool"]
        assert json_data["current_step"] is not None
        assert json_data["current_step"]["data"]["interruptions"][0]["agent"] == {
            "name": "duplicate",
            "identity": "duplicate#2",
        }

        approval_tool_items = [
            item
            for item in json_data["generated_items"]
            if item["type"] == "tool_call_item"
            and item["raw_item"].get("call_id") == "call_approval"
        ]
        assert len(approval_tool_items) == 1
        assert approval_tool_items[0]["agent"] == {
            "name": "duplicate",
            "identity": "duplicate#2",
        }
        assert approval_tool_items[0]["raw_item"] == {
            "arguments": "{}",
            "call_id": "call_approval",
            "id": "1",
            "name": "approval_tool",
            "type": "function_call",
        }

        restored = await RunState.from_json(first, json_data)
        assert restored._starting_agent is first
        assert restored._current_agent is second
        assert restored.get_interruptions()[0].agent is second
        assert any(
            isinstance(item, ToolCallItem)
            and item.agent is second
            and getattr(item.raw_item, "call_id", None) == "call_approval"
            for item in restored._generated_items
        )


class TestBuildAgentMap:
    """Test agent map building for handoff resolution."""

    def test_build_agent_map_collects_agents_without_looping(self):
        """Test that buildAgentMap handles circular handoff references."""
        agent_a = Agent(name="AgentA")
        agent_b = Agent(name="AgentB")

        # Create a cycle A -> B -> A.
        agent_a.handoffs = [agent_b]
        agent_b.handoffs = [agent_a]

        agent_map = _build_agent_map(agent_a)

        assert agent_map.get("AgentA") is not None
        assert agent_map.get("AgentB") is not None
        assert agent_map.get("AgentA").name == agent_a.name  # type: ignore[union-attr]
        assert agent_map.get("AgentB").name == agent_b.name  # type: ignore[union-attr]
        assert sorted(agent_map.keys()) == ["AgentA", "AgentB"]

    def test_build_agent_map_handles_complex_handoff_graphs(self):
        """Test that buildAgentMap handles complex handoff graphs."""
        agent_a = Agent(name="A")
        agent_b = Agent(name="B")
        agent_c = Agent(name="C")
        agent_d = Agent(name="D")

        # Create a graph: A -> B, C; B -> D; C -> D.
        agent_a.handoffs = [agent_b, agent_c]
        agent_b.handoffs = [agent_d]
        agent_c.handoffs = [agent_d]

        agent_map = _build_agent_map(agent_a)

        assert len(agent_map) == 4
        assert all(agent_map.get(name) is not None for name in ["A", "B", "C", "D"])

    def test_build_agent_map_handles_handoff_objects(self):
        """Test that buildAgentMap resolves handoff() objects via weak references."""
        agent_a = Agent(name="AgentA")
        agent_b = Agent(name="AgentB")
        agent_a.handoffs = [handoff(agent_b)]

        agent_map = _build_agent_map(agent_a)

        assert sorted(agent_map.keys()) == ["AgentA", "AgentB"]

    def test_build_agent_map_supports_legacy_handoff_agent_attribute(self):
        """Test that buildAgentMap keeps legacy custom handoffs with `.agent` targets working."""
        agent_a = Agent(name="AgentA")
        agent_b = Agent(name="AgentB")

        class LegacyHandoff(Handoff):
            def __init__(self, target: Agent[Any]):
                # Legacy custom handoff shape supported only for backward compatibility.
                self.agent = target
                self.agent_name = target.name
                self.name = "legacy_handoff"

        agent_a.handoffs = [LegacyHandoff(agent_b)]

        agent_map = _build_agent_map(agent_a)

        assert sorted(agent_map.keys()) == ["AgentA", "AgentB"]

    def test_build_agent_map_supports_legacy_non_handoff_agent_wrapper(self):
        """Test that buildAgentMap supports legacy non-Handoff wrappers with `.agent` targets."""
        agent_a = Agent(name="AgentA")
        agent_b = Agent(name="AgentB")

        class LegacyWrapper:
            def __init__(self, target: Agent[Any]):
                self.agent = target

        agent_a.handoffs = [LegacyWrapper(agent_b)]  # type: ignore[list-item]

        agent_map = _build_agent_map(agent_a)

        assert sorted(agent_map.keys()) == ["AgentA", "AgentB"]

    def test_build_agent_map_skips_unresolved_handoff_objects(self):
        """Test that buildAgentMap skips custom handoffs without target agent references."""
        agent_a = Agent(name="AgentA")
        agent_b = Agent(name="AgentB")

        async def _invoke_handoff(_ctx: RunContextWrapper[Any], _input: str) -> Agent[Any]:
            return agent_b

        detached_handoff = Handoff(
            tool_name="transfer_to_agent_b",
            tool_description="Transfer to AgentB.",
            input_json_schema={},
            on_invoke_handoff=_invoke_handoff,
            agent_name=agent_b.name,
        )
        agent_a.handoffs = [detached_handoff]

        agent_map = _build_agent_map(agent_a)

        assert sorted(agent_map.keys()) == ["AgentA"]


class TestDeserializeHelpers:
    @pytest.mark.asyncio
    async def test_serialization_uses_duplicate_identities_for_handoff_and_output_guardrails(self):
        """Duplicate-name item ownership should round-trip with identity keys."""
        first = Agent(name="duplicate")
        second = Agent(name="duplicate")
        third = Agent(name="duplicate")
        first.handoffs = [second, third]
        second.handoffs = [third]
        third.handoffs = [first]

        context: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
        state = make_state(first, context=context, original_input="test handoff", max_turns=2)
        state._current_agent = second
        state._generated_items = [
            HandoffOutputItem(
                agent=second,
                raw_item={"type": "handoff_output", "status": "completed"},  # type: ignore[arg-type]
                source_agent=second,
                target_agent=third,
            )
        ]

        output_guardrail = OutputGuardrail(
            guardrail_function=lambda _ctx, _agent, _output: GuardrailFunctionOutput(
                output_info={"guardrail": "ok"},
                tripwire_triggered=False,
            ),
            name="duplicate_output_guardrail",
        )
        state._output_guardrail_results = [
            OutputGuardrailResult(
                guardrail=output_guardrail,
                agent_output="done",
                agent=third,
                output=GuardrailFunctionOutput(
                    output_info={"guardrail": "ok"},
                    tripwire_triggered=False,
                ),
            )
        ]

        json_data = state.to_json()
        item_data = json_data["generated_items"][0]
        assert item_data["agent"] == {"name": "duplicate", "identity": "duplicate#2"}
        assert item_data["source_agent"] == {"name": "duplicate", "identity": "duplicate#2"}
        assert item_data["target_agent"] == {"name": "duplicate", "identity": "duplicate#3"}
        assert json_data["output_guardrail_results"][0]["agent"] == {
            "name": "duplicate",
            "identity": "duplicate#3",
        }

        restored = await RunState.from_json(first, json_data)
        restored_item = cast(HandoffOutputItem, restored._generated_items[0])
        assert restored_item.agent is second
        assert restored_item.source_agent is second
        assert restored_item.target_agent is third
        assert restored._output_guardrail_results[0].agent is third
