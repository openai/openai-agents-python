"""Hook lifecycle state must not redirect durable execution or approval owners."""

from typing import Any

import pytest

from agents import Agent, AgentHooks, RunConfig, Runner, RunState, UserError, handoff
from agents.decorators import tool
from agents.run_state import CURRENT_SCHEMA_VERSION
from agents.testing import ScriptedModel
from tests.model_test_helpers import get_exact_output_stream_step
from tests.test_responses import get_function_tool_call, get_text_message


class CountingHooks(AgentHooks[Any]):
    def __init__(self) -> None:
        self.event_counter = 0

    async def on_start(self, context: Any, agent: Agent[Any]) -> None:
        self.event_counter += 1


def build_graph(
    effects: list[str], *, restoring: bool, streamed: bool, unique: bool = False
) -> tuple[Agent[Any], Agent[Any], Agent[Any]]:
    def model(steps: list[Any]) -> ScriptedModel:
        return ScriptedModel(
            steps=[get_exact_output_stream_step(step) for step in steps] if streamed else steps
        )

    def child(owner: str, instructions: str) -> Agent[Any]:
        @tool(name_override="operate", needs_approval=True)
        def operate() -> str:
            effects.append(owner)
            return owner

        steps = [
            [get_function_tool_call("operate", "{}", call_id="follow-up")],
            [get_text_message(f"done-{owner}")],
        ]
        if not restoring:
            steps.insert(0, [get_function_tool_call("operate", "{}", call_id="saved-call")])
        return Agent(
            name=f"sub-{owner}" if unique else "sub",
            instructions=instructions,
            hooks=CountingHooks(),
            tools=[operate],
            model=model(steps),
        )

    x, y = child("x", "b"), child("y", "a")
    targets = [x, y] if not restoring else [y, x]
    root = Agent(
        name="root",
        handoffs=[
            handoff(target, tool_name_override=f"to_{'x' if target is x else 'y'}")
            for target in targets
        ],
        model=model([[get_function_tool_call("to_y", "{}", call_id="handoff")]]),
    )
    return root, x, y


async def run(agent: Agent[Any], value: Any, streamed: bool) -> Any:
    config = RunConfig(tracing_disabled=True)
    if streamed:
        result = Runner.run_streamed(agent, value, run_config=config)
        async for _ in result.stream_events():
            pass
        return result
    return await Runner.run(agent, value, run_config=config)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("always", [False, True])
async def test_hook_reset_preserves_resumed_execution_and_approval_owner(
    streamed: bool, always: bool
) -> None:
    effects: list[str] = []
    root, _, y = build_graph(effects, restoring=False, streamed=streamed)
    first = await run(root, "start", streamed)
    assert first.interruptions[0].agent is y
    assert isinstance(y.hooks, CountingHooks)
    assert y.hooks.event_counter == 1
    state = first.to_state()
    state.approve(first.interruptions[0], always_approve=always)
    payload = state.to_string()

    root, x, y = build_graph(effects, restoring=True, streamed=streamed)
    restored = await RunState.from_string(root, payload)
    resumed = await run(root, restored, streamed)
    assert resumed.last_agent is y
    assert effects == (["y", "y"] if always else ["y"])
    assert isinstance(x.model, ScriptedModel)
    assert not x.model.calls
    if always:
        assert resumed.final_output == "done-y"
        assert resumed.interruptions == []
    else:
        assert len(resumed.interruptions) == 1
        assert resumed.interruptions[0].agent is y
        assert resumed.interruptions[0].raw_item.call_id == "follow-up"


@pytest.mark.asyncio
@pytest.mark.parametrize("unique", [False, True])
@pytest.mark.parametrize("version", ["1.18", "1.20"])
async def test_legacy_hook_identity_restore_requires_unambiguous_names(
    unique: bool, version: str
) -> None:
    effects: list[str] = []
    root, _, _ = build_graph(effects, restoring=False, streamed=False, unique=unique)
    first = await run(root, "start", False)
    state = first.to_state()
    state.approve(first.interruptions[0], always_approve=True)
    payload = state.to_json()
    assert payload["$schemaVersion"] == CURRENT_SCHEMA_VERSION
    payload["$schemaVersion"] = version
    root, _, y = build_graph(effects, restoring=True, streamed=False, unique=unique)
    if unique:
        restored = await RunState.from_json(root, payload)
        resumed = await run(root, restored, False)
        assert resumed.last_agent is y
        assert effects == ["y", "y"]
    else:
        with pytest.raises(UserError, match="legacy duplicate-agent identities with hooks"):
            await RunState.from_json(root, payload)
        assert effects == []


@pytest.mark.asyncio
async def test_legacy_root_priority_does_not_depend_on_hook_state() -> None:
    effects: list[str] = []
    root, x, y = build_graph(effects, restoring=False, streamed=False)
    # Only the root and one non-root agent share a name; root priority cannot flip.
    root.name = y.name
    root.handoffs = [handoff(y, tool_name_override="to_y")]
    first = await run(root, "start", False)
    payload = first.to_state().to_json()
    payload["$schemaVersion"] = "1.18"
    root, x, y = build_graph(effects, restoring=True, streamed=False)
    root.name = y.name
    root.handoffs = [handoff(y, tool_name_override="to_y")]
    restored = await RunState.from_json(root, payload)
    pending = restored.get_interruptions()[0]
    assert pending.agent is y
    restored.approve(pending, always_approve=True)
    resumed = await run(root, restored, False)
    assert resumed.last_agent is y
    assert effects == ["y", "y"]
