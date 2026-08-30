from __future__ import annotations

import sys
from typing import Any

import pytest

from agents import Runner, _tool_invocation
from agents.tool import FunctionTool
from agents.util import _approvals

from .utils.hitl import make_function_tool_call, make_model_and_agent


def test_parse_function_tool_arguments_treats_recursion_error_as_uninspectable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_recursion_error(*_args: object, **_kwargs: object) -> object:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(_approvals.json, "loads", raise_recursion_error)

    assert _approvals.parse_function_tool_arguments('{"value": 1}') is None


def test_tool_invocation_identity_tolerates_json_recursion_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_recursion_error(*_args: object, **_kwargs: object) -> object:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(_tool_invocation.json, "loads", raise_recursion_error)

    identity = _tool_invocation.tool_invocation_identity(
        {
            "type": "function_call",
            "name": "send_email",
            "call_id": "call-deep",
            "arguments": '{"value": 1}',
        }
    )

    assert identity is not None


@pytest.mark.asyncio
async def test_runner_function_approval_fails_closed_for_json_recursion_depth() -> None:
    approval_inputs: list[dict[str, Any]] = []
    tool_inputs: list[str] = []

    async def needs_approval(_ctx: Any, params: dict[str, Any], _call_id: str) -> bool:
        approval_inputs.append(params)
        return False

    async def invoke_tool(_ctx: Any, raw_arguments: str) -> str:
        tool_inputs.append(raw_arguments)
        return "sent"

    tool = FunctionTool(
        name="send_email",
        description="Send an email.",
        params_json_schema={"type": "object", "properties": {}},
        on_invoke_tool=invoke_tool,
        needs_approval=needs_approval,
    )
    depth = sys.getrecursionlimit() * 2
    arguments = '{"value":' * depth + "0" + "}" * depth
    model, agent = make_model_and_agent(tools=[tool])
    model.enqueue([make_function_tool_call(tool.name, arguments=arguments, call_id="call-deep")])

    result = await Runner.run(agent, "send an email")

    assert len(result.interruptions) == 1
    assert result.interruptions[0].tool_name == tool.name
    assert approval_inputs == []
    assert tool_inputs == []
