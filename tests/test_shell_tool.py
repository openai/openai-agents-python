from __future__ import annotations

from typing import Any, cast

import pytest

from agents import (
    Agent,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    ShellCallOutcome,
    ShellCommandOutput,
    ShellResult,
    ShellTool,
)
from agents._run_impl import ShellAction, ToolRunShellCall
from agents.items import ToolApprovalItem, ToolCallOutputItem

from .utils.hitl import (
    HITL_REJECTION_MSG,
    make_context_wrapper,
    make_model_and_agent,
    make_on_approval_callback,
    make_shell_call,
    reject_tool_call,
    require_approval,
)


def _shell_call(call_id: str = "call_shell") -> dict[str, Any]:
    return cast(
        dict[str, Any],
        make_shell_call(
            call_id,
            id_value="shell_call",
            commands=["echo hi"],
            status="completed",
        ),
    )


@pytest.mark.asyncio
async def test_shell_tool_structured_output_is_rendered() -> None:
    shell_tool = ShellTool(
        executor=lambda request: ShellResult(
            output=[
                ShellCommandOutput(
                    command="echo hi",
                    stdout="hi\n",
                    outcome=ShellCallOutcome(type="exit", exit_code=0),
                ),
                ShellCommandOutput(
                    command="ls",
                    stdout="README.md\nsrc\n",
                    stderr="warning",
                    outcome=ShellCallOutcome(type="exit", exit_code=1),
                ),
            ],
            provider_data={"runner": "demo"},
            max_output_length=4096,
        )
    )

    tool_call = _shell_call()
    tool_call["action"]["commands"] = ["echo hi", "ls"]
    tool_call["action"]["max_output_length"] = 4096

    tool_run = ToolRunShellCall(tool_call=tool_call, shell_tool=shell_tool)
    agent = Agent(name="shell-agent", tools=[shell_tool])
    context_wrapper: RunContextWrapper[Any] = RunContextWrapper(context=None)

    result = await ShellAction.execute(
        agent=agent,
        call=tool_run,
        hooks=RunHooks[Any](),
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )

    assert isinstance(result, ToolCallOutputItem)
    assert "$ echo hi" in result.output
    assert "stderr:\nwarning" in result.output

    raw_item = cast(dict[str, Any], result.raw_item)
    assert raw_item["type"] == "shell_call_output"
    assert raw_item["status"] == "completed"
    assert raw_item["provider_data"]["runner"] == "demo"
    assert raw_item["max_output_length"] == 4096
    shell_output = raw_item["shell_output"]
    assert shell_output[1]["exit_code"] == 1
    assert isinstance(raw_item["output"], list)
    first_output = raw_item["output"][0]
    assert first_output["stdout"].startswith("hi")
    assert first_output["outcome"]["type"] == "exit"
    assert first_output["outcome"]["exit_code"] == 0
    assert "command" not in first_output
    input_payload = result.to_input_item()
    assert isinstance(input_payload, dict)
    payload_dict = cast(dict[str, Any], input_payload)
    assert payload_dict["type"] == "shell_call_output"
    assert "status" not in payload_dict
    assert "shell_output" not in payload_dict
    assert "provider_data" not in payload_dict


@pytest.mark.asyncio
async def test_shell_tool_executor_failure_returns_error() -> None:
    class ExplodingExecutor:
        def __call__(self, request):
            raise RuntimeError("boom")

    shell_tool = ShellTool(executor=ExplodingExecutor())
    tool_call = {
        "type": "shell_call",
        "id": "shell_call_fail",
        "call_id": "call_shell_fail",
        "status": "completed",
        "action": {"commands": ["echo boom"], "timeout_ms": 1000},
    }
    tool_run = ToolRunShellCall(tool_call=tool_call, shell_tool=shell_tool)
    agent = Agent(name="shell-agent", tools=[shell_tool])
    context_wrapper: RunContextWrapper[Any] = RunContextWrapper(context=None)

    result = await ShellAction.execute(
        agent=agent,
        call=tool_run,
        hooks=RunHooks[Any](),
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )

    assert isinstance(result, ToolCallOutputItem)
    assert "boom" in result.output
    raw_item = cast(dict[str, Any], result.raw_item)
    assert raw_item["type"] == "shell_call_output"
    assert raw_item["status"] == "failed"
    assert isinstance(raw_item["output"], list)
    assert "boom" in raw_item["output"][0]["stdout"]
    first_output = raw_item["output"][0]
    assert first_output["outcome"]["type"] == "exit"
    assert first_output["outcome"]["exit_code"] == 1
    assert "command" not in first_output
    assert isinstance(raw_item["output"], list)
    input_payload = result.to_input_item()
    assert isinstance(input_payload, dict)
    payload_dict = cast(dict[str, Any], input_payload)
    assert payload_dict["type"] == "shell_call_output"
    assert "status" not in payload_dict
    assert "shell_output" not in payload_dict
    assert "provider_data" not in payload_dict


@pytest.mark.asyncio
async def test_shell_tool_needs_approval_returns_approval_item() -> None:
    """Test that shell tool with needs_approval=True returns ToolApprovalItem."""

    shell_tool = ShellTool(
        executor=lambda request: "output",
        needs_approval=require_approval,
    )

    tool_run = ToolRunShellCall(tool_call=_shell_call(), shell_tool=shell_tool)
    _, agent = make_model_and_agent(tools=[shell_tool], name="shell-agent")
    context_wrapper = make_context_wrapper()

    result = await ShellAction.execute(
        agent=agent,
        call=tool_run,
        hooks=RunHooks[Any](),
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )

    assert isinstance(result, ToolApprovalItem)
    assert result.tool_name == "shell"
    assert result.name == "shell"


@pytest.mark.asyncio
async def test_shell_tool_needs_approval_rejected_returns_rejection() -> None:
    """Test that shell tool with needs_approval that is rejected returns rejection output."""

    shell_tool = ShellTool(
        executor=lambda request: "output",
        needs_approval=require_approval,
    )

    tool_call = _shell_call()
    tool_run = ToolRunShellCall(tool_call=tool_call, shell_tool=shell_tool)
    _, agent = make_model_and_agent(tools=[shell_tool], name="shell-agent")
    context_wrapper = make_context_wrapper()

    # Pre-reject the tool call
    reject_tool_call(context_wrapper, agent, tool_call, "shell")

    result = await ShellAction.execute(
        agent=agent,
        call=tool_run,
        hooks=RunHooks[Any](),
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )

    assert isinstance(result, ToolCallOutputItem)
    assert HITL_REJECTION_MSG in result.output
    raw_item = cast(dict[str, Any], result.raw_item)
    assert raw_item["type"] == "shell_call_output"
    assert len(raw_item["output"]) == 1
    assert raw_item["output"][0]["stderr"] == HITL_REJECTION_MSG


@pytest.mark.asyncio
async def test_shell_tool_on_approval_callback_auto_approves() -> None:
    """Test that shell tool on_approval callback can auto-approve."""

    shell_tool = ShellTool(
        executor=lambda request: "output",
        needs_approval=require_approval,
        on_approval=make_on_approval_callback(approve=True),
    )

    tool_run = ToolRunShellCall(tool_call=_shell_call(), shell_tool=shell_tool)
    _, agent = make_model_and_agent(tools=[shell_tool], name="shell-agent")
    context_wrapper = make_context_wrapper()

    result = await ShellAction.execute(
        agent=agent,
        call=tool_run,
        hooks=RunHooks[Any](),
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )

    # Should execute normally since on_approval auto-approved
    assert isinstance(result, ToolCallOutputItem)
    assert result.output == "output"


@pytest.mark.asyncio
async def test_shell_tool_on_approval_callback_auto_rejects() -> None:
    """Test that shell tool on_approval callback can auto-reject."""

    shell_tool = ShellTool(
        executor=lambda request: "output",
        needs_approval=require_approval,
        on_approval=make_on_approval_callback(approve=False, reason="Not allowed"),
    )

    tool_run = ToolRunShellCall(tool_call=_shell_call(), shell_tool=shell_tool)
    agent = Agent(name="shell-agent", tools=[shell_tool])
    context_wrapper: RunContextWrapper[Any] = make_context_wrapper()

    result = await ShellAction.execute(
        agent=agent,
        call=tool_run,
        hooks=RunHooks[Any](),
        context_wrapper=context_wrapper,
        config=RunConfig(),
    )

    # Should return rejection output
    assert isinstance(result, ToolCallOutputItem)
    assert HITL_REJECTION_MSG in result.output
