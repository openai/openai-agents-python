from __future__ import annotations

import json
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
    UserError,
    set_tracing_disabled,
    trace,
)
from agents.items import ToolApprovalItem, ToolCallOutputItem
from agents.run_internal.run_loop import ShellAction, ToolRunShellCall, execute_shell_calls

from .testing_processor import SPAN_PROCESSOR_TESTING
from .utils.hitl import (
    HITL_REJECTION_MSG,
    make_context_wrapper,
    make_model_and_agent,
    make_on_approval_callback,
    make_shell_call,
    reject_tool_call,
    require_approval,
)


def _get_function_span(tool_name: str) -> dict[str, Any]:
    for span in SPAN_PROCESSOR_TESTING.get_ordered_spans(including_empty=True):
        exported = span.export()
        if not exported:
            continue
        span_data = exported.get("span_data")
        if not isinstance(span_data, dict):
            continue
        if span_data.get("type") == "function" and span_data.get("name") == tool_name:
            return exported
    raise AssertionError(f"Function span for tool '{tool_name}' not found")


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


def test_shell_tool_defaults_to_local_environment() -> None:
    shell_tool = ShellTool(executor=lambda request: "ok")

    assert shell_tool.environment == {"type": "local"}
    assert shell_tool.executor is not None


def test_shell_tool_supports_hosted_environment_without_executor() -> None:
    shell_tool = ShellTool(
        environment={
            "type": "container_reference",
            "container_id": "cntr_123",
        }
    )

    assert shell_tool.environment == {"type": "container_reference", "container_id": "cntr_123"}
    assert shell_tool.executor is None


def test_shell_tool_normalizes_container_auto_environment() -> None:
    shell_tool = ShellTool(
        environment={
            "type": "container_auto",
            "file_ids": ["file_123"],
            "memory_limit": "4g",
            "network_policy": {
                "type": "allowlist",
                "allowed_domains": ["example.com"],
                "domain_secrets": [
                    {
                        "domain": "example.com",
                        "name": "API_TOKEN",
                        "value": "secret",
                    }
                ],
            },
            "skills": [
                {"type": "skill_reference", "skill_id": "skill_123", "version": "latest"},
                {
                    "type": "inline",
                    "name": "csv-workbench",
                    "description": "Analyze CSV files.",
                    "source": {
                        "type": "base64",
                        "media_type": "application/zip",
                        "data": "ZmFrZS16aXA=",
                    },
                },
            ],
        }
    )

    assert shell_tool.environment == {
        "type": "container_auto",
        "file_ids": ["file_123"],
        "memory_limit": "4g",
        "network_policy": {
            "type": "allowlist",
            "allowed_domains": ["example.com"],
            "domain_secrets": [
                {
                    "domain": "example.com",
                    "name": "API_TOKEN",
                    "value": "secret",
                }
            ],
        },
        "skills": [
            {"type": "skill_reference", "skill_id": "skill_123", "version": "latest"},
            {
                "type": "inline",
                "name": "csv-workbench",
                "description": "Analyze CSV files.",
                "source": {
                    "type": "base64",
                    "media_type": "application/zip",
                    "data": "ZmFrZS16aXA=",
                },
            },
        ],
    }


def test_shell_tool_rejects_local_mode_without_executor() -> None:
    with pytest.raises(UserError, match="requires an executor"):
        ShellTool()

    with pytest.raises(UserError, match="requires an executor"):
        ShellTool(environment={"type": "local"})


def test_shell_tool_allows_unvalidated_hosted_environment_shapes() -> None:
    shell_tool = ShellTool(environment=cast(Any, {"type": "container_reference"}))
    assert shell_tool.environment == {"type": "container_reference"}

    shell_tool = ShellTool(
        environment=cast(
            Any,
            {
                "type": "container_auto",
                "network_policy": {
                    "type": "future_mode",
                    "allowed_domains": ["example.com"],
                    "some_new_field": True,
                },
                "skills": [{"type": "skill_reference"}],
            },
        )
    )
    assert isinstance(shell_tool.environment, dict)
    assert shell_tool.environment["type"] == "container_auto"


def test_shell_tool_rejects_local_executor_and_approval_for_hosted_environment() -> None:
    with pytest.raises(UserError, match="does not accept an executor"):
        ShellTool(
            executor=lambda request: "ok",
            environment={"type": "container_reference", "container_id": "cntr_123"},
        )

    with pytest.raises(UserError, match="does not support needs_approval or on_approval"):
        ShellTool(
            environment={"type": "container_reference", "container_id": "cntr_123"},
            needs_approval=True,
        )

    with pytest.raises(UserError, match="does not support needs_approval or on_approval"):
        ShellTool(
            environment={"type": "container_reference", "container_id": "cntr_123"},
            on_approval=lambda _context, _item: {"approve": True},
        )


@pytest.mark.asyncio
async def test_execute_shell_calls_surfaces_missing_local_executor() -> None:
    shell_tool = ShellTool(
        environment={
            "type": "container_reference",
            "container_id": "cntr_123",
        }
    )
    tool_run = ToolRunShellCall(tool_call=_shell_call(), shell_tool=shell_tool)
    agent = Agent(name="shell-agent", tools=[shell_tool])
    context_wrapper: RunContextWrapper[Any] = RunContextWrapper(context=None)

    result = await execute_shell_calls(
        agent=agent,
        calls=[tool_run],
        context_wrapper=context_wrapper,
        hooks=RunHooks[Any](),
        config=RunConfig(),
    )

    assert len(result) == 1
    output_item = result[0]
    assert isinstance(output_item, ToolCallOutputItem)
    assert output_item.output == "Shell tool has no local executor configured."
    raw_item = cast(dict[str, Any], output_item.raw_item)
    assert raw_item["type"] == "shell_call_output"
    assert raw_item["call_id"] == "call_shell"
    assert raw_item["status"] == "failed"


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
async def test_shell_tool_emits_function_span() -> None:
    shell_tool = ShellTool(executor=lambda request: "shell span output")
    tool_run = ToolRunShellCall(tool_call=_shell_call("call_shell_trace"), shell_tool=shell_tool)
    agent = Agent(name="shell-agent", tools=[shell_tool])
    context_wrapper: RunContextWrapper[Any] = RunContextWrapper(context=None)

    set_tracing_disabled(False)
    with trace("shell-span-test"):
        result = await ShellAction.execute(
            agent=agent,
            call=tool_run,
            hooks=RunHooks[Any](),
            context_wrapper=context_wrapper,
            config=RunConfig(),
        )

    assert isinstance(result, ToolCallOutputItem)
    function_span = _get_function_span(shell_tool.name)
    span_data = cast(dict[str, Any], function_span["span_data"])
    assert "echo hi" in cast(str, span_data.get("input", ""))
    assert span_data.get("output") == "shell span output"


@pytest.mark.asyncio
async def test_shell_tool_redacts_span_error_when_sensitive_data_disabled() -> None:
    secret_error = "shell secret output"

    class ExplodingExecutor:
        def __call__(self, request):
            raise RuntimeError(secret_error)

    shell_tool = ShellTool(executor=ExplodingExecutor())
    tool_run = ToolRunShellCall(
        tool_call=_shell_call("call_shell_trace_redacted"),
        shell_tool=shell_tool,
    )
    agent = Agent(name="shell-agent", tools=[shell_tool])
    context_wrapper: RunContextWrapper[Any] = RunContextWrapper(context=None)

    set_tracing_disabled(False)
    with trace("shell-span-redaction-test"):
        result = await ShellAction.execute(
            agent=agent,
            call=tool_run,
            hooks=RunHooks[Any](),
            context_wrapper=context_wrapper,
            config=RunConfig(trace_include_sensitive_data=False),
        )

    assert isinstance(result, ToolCallOutputItem)
    function_span = _get_function_span(shell_tool.name)
    assert function_span.get("error") == {
        "message": "Error running tool",
        "data": {
            "tool_name": shell_tool.name,
            "error": "Tool execution failed. Error details are redacted.",
        },
    }
    assert secret_error not in json.dumps(function_span)
    span_data = cast(dict[str, Any], function_span["span_data"])
    assert span_data.get("input") is None
    assert span_data.get("output") is None


@pytest.mark.asyncio
async def test_shell_tool_executor_failure_returns_error() -> None:
    class ExplodingExecutor:
        def __call__(self, request):
            raise RuntimeError("boom" * 10)

    shell_tool = ShellTool(executor=ExplodingExecutor())
    tool_call = {
        "type": "shell_call",
        "id": "shell_call_fail",
        "call_id": "call_shell_fail",
        "status": "completed",
        "action": {
            "commands": ["echo boom"],
            "timeout_ms": 1000,
            "max_output_length": 6,
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
    assert result.output == "boombo"
    raw_item = cast(dict[str, Any], result.raw_item)
    assert raw_item["type"] == "shell_call_output"
    assert raw_item["status"] == "failed"
    assert raw_item["max_output_length"] == 6
    assert isinstance(raw_item["output"], list)
    assert raw_item["output"][0]["stdout"] == "boombo"
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
async def test_shell_tool_output_respects_max_output_length() -> None:
    shell_tool = ShellTool(
        executor=lambda request: ShellResult(
            output=[
                ShellCommandOutput(
                    stdout="0123456789",
                    stderr="abcdef",
                    outcome=ShellCallOutcome(type="exit", exit_code=0),
                )
            ],
        )
    )

    tool_call = {
        "type": "shell_call",
        "id": "shell_call",
        "call_id": "call_shell",
        "status": "completed",
        "action": {
            "commands": ["echo hi"],
            "timeout_ms": 1000,
            "max_output_length": 6,
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
    assert result.output == "012345"
    raw_item = cast(dict[str, Any], result.raw_item)
    assert raw_item["max_output_length"] == 6
    assert raw_item["output"][0]["stdout"] == "012345"
    assert raw_item["output"][0]["stderr"] == ""


@pytest.mark.asyncio
async def test_shell_tool_uses_smaller_max_output_length() -> None:
    shell_tool = ShellTool(
        executor=lambda request: ShellResult(
            output=[
                ShellCommandOutput(
                    stdout="0123456789",
                    stderr="abcdef",
                    outcome=ShellCallOutcome(type="exit", exit_code=0),
                )
            ],
            max_output_length=8,
        )
    )

    tool_call = {
        "type": "shell_call",
        "id": "shell_call",
        "call_id": "call_shell",
        "status": "completed",
        "action": {
            "commands": ["echo hi"],
            "timeout_ms": 1000,
            "max_output_length": 6,
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
    assert result.output == "012345"
    raw_item = cast(dict[str, Any], result.raw_item)
    assert raw_item["max_output_length"] == 6
    assert raw_item["output"][0]["stdout"] == "012345"
    assert raw_item["output"][0]["stderr"] == ""


@pytest.mark.asyncio
async def test_shell_tool_executor_can_override_max_output_length_to_zero() -> None:
    shell_tool = ShellTool(
        executor=lambda request: ShellResult(
            output=[
                ShellCommandOutput(
                    stdout="0123456789",
                    stderr="abcdef",
                    outcome=ShellCallOutcome(type="exit", exit_code=0),
                )
            ],
            max_output_length=0,
        )
    )

    tool_call = {
        "type": "shell_call",
        "id": "shell_call",
        "call_id": "call_shell",
        "status": "completed",
        "action": {
            "commands": ["echo hi"],
            "timeout_ms": 1000,
            "max_output_length": 6,
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
    assert result.output == ""
    raw_item = cast(dict[str, Any], result.raw_item)
    assert raw_item["max_output_length"] == 0
    assert raw_item["output"][0]["stdout"] == ""
    assert raw_item["output"][0]["stderr"] == ""


@pytest.mark.asyncio
async def test_shell_tool_action_can_request_zero_max_output_length() -> None:
    shell_tool = ShellTool(
        executor=lambda request: ShellResult(
            output=[
                ShellCommandOutput(
                    stdout="0123456789",
                    stderr="abcdef",
                    outcome=ShellCallOutcome(type="exit", exit_code=0),
                )
            ],
        )
    )

    tool_call = {
        "type": "shell_call",
        "id": "shell_call",
        "call_id": "call_shell",
        "status": "completed",
        "action": {
            "commands": ["echo hi"],
            "timeout_ms": 1000,
            "max_output_length": 0,
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
    assert result.output == ""
    raw_item = cast(dict[str, Any], result.raw_item)
    assert raw_item["max_output_length"] == 0
    assert raw_item["output"][0]["stdout"] == ""
    assert raw_item["output"][0]["stderr"] == ""


@pytest.mark.asyncio
async def test_shell_tool_action_negative_max_output_length_clamps_to_zero() -> None:
    shell_tool = ShellTool(
        executor=lambda request: ShellResult(
            output=[
                ShellCommandOutput(
                    stdout="0123456789",
                    stderr="abcdef",
                    outcome=ShellCallOutcome(type="exit", exit_code=0),
                )
            ],
        )
    )

    tool_call = {
        "type": "shell_call",
        "id": "shell_call",
        "call_id": "call_shell",
        "status": "completed",
        "action": {
            "commands": ["echo hi"],
            "timeout_ms": 1000,
            "max_output_length": -5,
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
    assert result.output == ""
    raw_item = cast(dict[str, Any], result.raw_item)
    assert raw_item["max_output_length"] == 0
    assert raw_item["output"][0]["stdout"] == ""
    assert raw_item["output"][0]["stderr"] == ""


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
async def test_shell_tool_rejection_uses_run_level_formatter() -> None:
    """Shell approval rejection should use the run-level formatter message."""

    shell_tool = ShellTool(
        executor=lambda request: "output",
        needs_approval=require_approval,
    )

    tool_call = _shell_call()
    tool_run = ToolRunShellCall(tool_call=tool_call, shell_tool=shell_tool)
    _, agent = make_model_and_agent(tools=[shell_tool], name="shell-agent")
    context_wrapper = make_context_wrapper()

    reject_tool_call(context_wrapper, agent, tool_call, "shell")

    result = await ShellAction.execute(
        agent=agent,
        call=tool_run,
        hooks=RunHooks[Any](),
        context_wrapper=context_wrapper,
        config=RunConfig(
            tool_error_formatter=lambda args: f"{args.tool_name} denied ({args.call_id})"
        ),
    )

    assert isinstance(result, ToolCallOutputItem)
    assert result.output == "shell denied (call_shell)"
    raw_item = cast(dict[str, Any], result.raw_item)
    assert raw_item["output"][0]["stderr"] == "shell denied (call_shell)"


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
