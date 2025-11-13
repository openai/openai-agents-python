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
from agents.items import ToolCallOutputItem


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

    tool_call = {
        "type": "shell_call",
        "id": "shell_call",
        "call_id": "call_shell",
        "status": "completed",
        "action": {
            "commands": ["echo hi", "ls"],
            "timeout_ms": 1000,
            "max_output_length": 4096,
        },
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
