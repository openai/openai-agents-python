import json
from typing import Any

import pytest

from agents import Agent, RunHooks, Runner, function_tool
from agents.run_context import RunContextWrapper
from agents.testing import ScriptedModel
from agents.tool import Tool
from agents.tool_context import ToolContext

from .test_responses import get_function_tool_call, get_text_message


def _tool_context(
    *,
    agent_name: str = "Verifier",
    tool_name: str = "lookup",
    tool_arguments: str = '{"account": 7, "active": true}',
    tool_namespace: str | None = None,
) -> ToolContext[dict[str, object]]:
    return ToolContext(
        context={},
        tool_name=tool_name,
        tool_call_id="call-local",
        tool_arguments=tool_arguments,
        tool_namespace=tool_namespace,
        agent=Agent(name=agent_name),
    )


def test_action_ref_is_stable_for_equivalent_json_arguments() -> None:
    first = _tool_context(tool_arguments='{"account":7,"active":true}')
    second = _tool_context(tool_arguments='{ "active": true, "account": 7 }')

    assert first.action_ref is not None
    assert first.action_ref.startswith("act_v1_")
    assert first.action_ref == second.action_ref


def test_action_ref_handles_json_surrogate_escape() -> None:
    first = _tool_context(tool_arguments='{"x":"\\ud800"}')
    second = _tool_context(tool_arguments='{ "x": "\\ud800" }')

    assert first.action_ref is not None
    assert first.action_ref == second.action_ref


def test_action_ref_commits_agent_tool_and_request() -> None:
    baseline = _tool_context().action_ref

    assert baseline is not None
    assert _tool_context(agent_name="Other verifier").action_ref != baseline
    assert _tool_context(tool_name="update").action_ref != baseline
    assert _tool_context(tool_arguments='{"account": 8, "active": true}').action_ref != baseline
    assert _tool_context(tool_namespace="billing").action_ref != baseline


def test_action_ref_distinguishes_bare_and_deferred_tool_identity() -> None:
    bare = _tool_context(tool_name="lookup")
    deferred = _tool_context(tool_name="lookup", tool_namespace="lookup")

    assert bare.qualified_tool_name == deferred.qualified_tool_name == "lookup"
    assert bare.action_ref is not None
    assert deferred.action_ref is not None
    assert bare.action_ref != deferred.action_ref


def test_action_ref_does_not_depend_on_opaque_tool_call_id() -> None:
    agent = Agent(name="Verifier")
    first: ToolContext[dict[str, object]] = ToolContext(
        context={},
        tool_name="lookup",
        tool_call_id="call-1",
        tool_arguments='{"account": 7}',
        agent=agent,
    )
    second: ToolContext[dict[str, object]] = ToolContext(
        context={},
        tool_name="lookup",
        tool_call_id="call-2",
        tool_arguments='{"account": 7}',
        agent=agent,
    )

    assert first.action_ref == second.action_ref


def test_action_ref_is_none_without_agent_metadata() -> None:
    context: ToolContext[dict[str, object]] = ToolContext(
        context={},
        tool_name="lookup",
        tool_call_id="call-1",
        tool_arguments="{}",
    )

    assert context.action_ref is None


class CaptureActionRefHooks(RunHooks[Any]):
    def __init__(self) -> None:
        self.action_refs: list[str | None] = []

    async def on_tool_start(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        tool: Tool,
    ) -> None:
        assert isinstance(context, ToolContext)
        self.action_refs.append(context.action_ref)

    async def on_tool_end(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        tool: Tool,
        result: object,
    ) -> None:
        assert isinstance(context, ToolContext)
        self.action_refs.append(context.action_ref)


@pytest.mark.asyncio
async def test_action_ref_is_unchanged_across_tool_hooks() -> None:
    @function_tool
    def echo(value: str) -> str:
        return value

    hooks = CaptureActionRefHooks()
    model = ScriptedModel()
    model.extend(
        [
            [get_function_tool_call("echo", json.dumps({"value": "hello"}))],
            [get_text_message("done")],
        ]
    )
    agent = Agent(name="Hook verifier", model=model, tools=[echo])

    await Runner.run(agent, input="call the echo tool", hooks=hooks)

    assert len(hooks.action_refs) == 2
    assert hooks.action_refs[0] is not None
    assert hooks.action_refs[0] == hooks.action_refs[1]
