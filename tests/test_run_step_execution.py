from __future__ import annotations

import asyncio
import copy
import dataclasses
import gc
import json
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, cast

import pytest
from openai.types.responses import ResponseFunctionToolCall
from openai.types.responses.response_output_item import McpApprovalRequest
from openai.types.responses.response_output_message import ResponseOutputMessage
from openai.types.responses.response_output_refusal import ResponseOutputRefusal
from pydantic import BaseModel

from agents import (
    Agent,
    AgentBase,
    ApplyPatchTool,
    FunctionTool,
    HostedMCPTool,
    MCPApprovalRequestItem,
    MCPApprovalResponseItem,
    MessageOutputItem,
    ModelBehaviorError,
    ModelRefusalError,
    ModelResponse,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    RunItem,
    ShellTool,
    ToolApprovalItem,
    ToolCallItem,
    ToolCallOutputItem,
    ToolExecutionConfig,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrail,
    ToolOutputGuardrailData,
    ToolOutputGuardrailTripwireTriggered,
    ToolTimeoutError,
    TResponseInputItem,
    Usage,
    UserError,
    tool_namespace,
    tool_output_guardrail,
    trace,
)
from agents._public_agent import set_public_agent
from agents.run_internal import run_loop, turn_resolution
from agents.run_internal.agent_bindings import bind_execution_agent, bind_public_agent
from agents.run_internal.run_loop import (
    NextStepFinalOutput,
    NextStepHandoff,
    NextStepInterruption,
    NextStepRunAgain,
    ProcessedResponse,
    SingleStepResult,
    ToolRunApplyPatchCall,
    ToolRunComputerAction,
    ToolRunFunction,
    ToolRunHandoff,
    ToolRunLocalShellCall,
    ToolRunMCPApprovalRequest,
    ToolRunShellCall,
    get_handoffs,
    get_output_schema,
)
from agents.run_internal.tool_execution import execute_function_tool_calls
from agents.tool import function_tool
from agents.tool_context import ToolContext

from .test_responses import (
    get_final_output_message,
    get_function_tool,
    get_function_tool_call,
    get_handoff_tool_call,
    get_text_input_item,
    get_text_message,
)
from .testing_processor import SPAN_PROCESSOR_TESTING
from .utils.hitl import (
    RecordingEditor,
    assert_single_approval_interruption,
    make_agent,
    make_apply_patch_dict,
    make_context_wrapper,
    make_function_tool_call,
    make_shell_call,
    reject_tool_call,
)


def _function_spans() -> list[dict[str, Any]]:
    function_spans: list[dict[str, Any]] = []
    for span in SPAN_PROCESSOR_TESTING.get_ordered_spans(including_empty=True):
        exported = span.export()
        if not exported:
            continue
        span_data = exported.get("span_data")
        if not isinstance(span_data, dict):
            continue
        if span_data.get("type") != "function":
            continue
        function_spans.append(exported)
    return function_spans


def _function_span_names() -> list[str]:
    names: list[str] = []
    for exported in _function_spans():
        span_data = exported.get("span_data")
        if not isinstance(span_data, dict):
            continue
        name = span_data.get("name")
        if isinstance(name, str):
            names.append(name)
    return names


def _bind_agent(agent: Agent[Any]):
    public_agent = getattr(agent, "_agents_public_agent", None)
    if isinstance(public_agent, Agent):
        return bind_execution_agent(public_agent=public_agent, execution_agent=agent)
    return bind_public_agent(agent)


@pytest.mark.asyncio
async def test_empty_response_is_final_output():
    agent = Agent[None](name="test")
    response = ModelResponse(
        output=[],
        usage=Usage(),
        response_id=None,
    )
    result = await get_execute_result(agent, response)

    assert result.original_input == "hello"
    assert result.generated_items == []
    assert isinstance(result.next_step, NextStepFinalOutput)
    assert result.next_step.output == ""


@pytest.mark.asyncio
async def test_plaintext_agent_no_tool_calls_is_final_output():
    agent = Agent(name="test")
    response = ModelResponse(
        output=[get_text_message("hello_world")],
        usage=Usage(),
        response_id=None,
    )
    result = await get_execute_result(agent, response)

    assert result.original_input == "hello"
    assert len(result.generated_items) == 1
    assert_item_is_message(result.generated_items[0], "hello_world")
    assert isinstance(result.next_step, NextStepFinalOutput)
    assert result.next_step.output == "hello_world"


@pytest.mark.asyncio
async def test_plaintext_agent_no_tool_calls_multiple_messages_is_final_output():
    agent = Agent(name="test")
    response = ModelResponse(
        output=[
            get_text_message("hello_world"),
            get_text_message("bye"),
        ],
        usage=Usage(),
        response_id=None,
    )
    result = await get_execute_result(
        agent,
        response,
        original_input=[
            get_text_input_item("test"),
            get_text_input_item("test2"),
        ],
    )

    assert len(result.original_input) == 2
    assert len(result.generated_items) == 2
    assert_item_is_message(result.generated_items[0], "hello_world")
    assert_item_is_message(result.generated_items[1], "bye")

    assert isinstance(result.next_step, NextStepFinalOutput)
    assert result.next_step.output == "bye"


@pytest.mark.asyncio
async def test_execute_tools_allows_unhashable_tool_call_arguments():
    agent = make_agent()
    response = ModelResponse(output=[], usage=Usage(), response_id="resp")
    raw_tool_call = {
        "type": "function_call",
        "call_id": "call-1",
        "name": "tool",
        "arguments": {"key": "value"},
    }
    pre_step_items: list[RunItem] = [ToolCallItem(agent=agent, raw_item=raw_tool_call)]

    result = await get_execute_result(agent, response, generated_items=pre_step_items)

    assert len(result.generated_items) == 1
    assert isinstance(result.next_step, NextStepFinalOutput)


@pytest.mark.asyncio
async def test_plaintext_agent_with_tool_call_is_run_again():
    agent = Agent(name="test", tools=[get_function_tool(name="test", return_value="123")])
    response = ModelResponse(
        output=[get_text_message("hello_world"), get_function_tool_call("test", "")],
        usage=Usage(),
        response_id=None,
    )
    result = await get_execute_result(agent, response)

    assert result.original_input == "hello"

    # 3 items: new message, tool call, tool result
    assert len(result.generated_items) == 3
    assert isinstance(result.next_step, NextStepRunAgain)

    items = result.generated_items
    assert_item_is_message(items[0], "hello_world")
    assert_item_is_function_tool_call(items[1], "test", None)
    assert_item_is_function_tool_call_output(items[2], "123")

    assert isinstance(result.next_step, NextStepRunAgain)


@pytest.mark.asyncio
async def test_function_tool_concurrency_default_starts_all_calls():
    active_count = 0
    max_seen_count = 0

    async def tracked_tool(value: int) -> str:
        nonlocal active_count, max_seen_count
        active_count += 1
        max_seen_count = max(max_seen_count, active_count)
        try:
            await asyncio.sleep(0.01)
            return f"ok-{value}"
        finally:
            active_count -= 1

    tool = function_tool(tracked_tool, name_override="tracked_tool")
    agent = Agent(name="test", tools=[tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("tracked_tool", json.dumps({"value": 1}), call_id="call_1"),
            get_function_tool_call("tracked_tool", json.dumps({"value": 2}), call_id="call_2"),
            get_function_tool_call("tracked_tool", json.dumps({"value": 3}), call_id="call_3"),
        ],
        usage=Usage(),
        response_id="resp",
    )

    result = await get_execute_result(agent, response)

    assert active_count == 0
    assert max_seen_count == 3
    assert_item_is_function_tool_call_output(result.generated_items[3], "ok-1")
    assert_item_is_function_tool_call_output(result.generated_items[4], "ok-2")
    assert_item_is_function_tool_call_output(result.generated_items[5], "ok-3")


@pytest.mark.asyncio
async def test_function_tool_concurrency_cap_limits_calls_and_preserves_output_order():
    active_count = 0
    max_seen_count = 0

    async def tracked_tool(value: int) -> str:
        nonlocal active_count, max_seen_count
        active_count += 1
        max_seen_count = max(max_seen_count, active_count)
        try:
            await asyncio.sleep(0.03 if value == 1 else 0.001)
            return f"ok-{value}"
        finally:
            active_count -= 1

    tool = function_tool(tracked_tool, name_override="tracked_tool")
    agent = Agent(name="test", tools=[tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("tracked_tool", json.dumps({"value": 1}), call_id="call_1"),
            get_function_tool_call("tracked_tool", json.dumps({"value": 2}), call_id="call_2"),
            get_function_tool_call("tracked_tool", json.dumps({"value": 3}), call_id="call_3"),
        ],
        usage=Usage(),
        response_id="resp",
    )

    result = await get_execute_result(
        agent,
        response,
        run_config=RunConfig(tool_execution=ToolExecutionConfig(max_function_tool_concurrency=2)),
    )

    assert active_count == 0
    assert max_seen_count == 2
    assert_item_is_function_tool_call_output(result.generated_items[3], "ok-1")
    assert_item_is_function_tool_call_output(result.generated_items[4], "ok-2")
    assert_item_is_function_tool_call_output(result.generated_items[5], "ok-3")


@pytest.mark.asyncio
async def test_function_tool_concurrency_cap_leaves_queued_calls_unstarted_after_failure():
    started_tools: list[str] = []

    async def failing_tool() -> str:
        started_tools.append("failing_tool")
        raise RuntimeError("boom")

    async def queued_tool() -> str:
        started_tools.append("queued_tool")
        return "should-not-run"

    failing = function_tool(
        failing_tool,
        name_override="failing_tool",
        failure_error_function=None,
    )
    queued = function_tool(queued_tool, name_override="queued_tool")
    agent = Agent(name="test", tools=[failing, queued])
    response = ModelResponse(
        output=[
            get_function_tool_call("failing_tool", "{}", call_id="call_1"),
            get_function_tool_call("queued_tool", "{}", call_id="call_2"),
        ],
        usage=Usage(),
        response_id="resp",
    )

    with pytest.raises(UserError, match="Error running tool failing_tool: boom"):
        await get_execute_result(
            agent,
            response,
            run_config=RunConfig(
                tool_execution=ToolExecutionConfig(max_function_tool_concurrency=1)
            ),
        )

    assert started_tools == ["failing_tool"]


@pytest.mark.asyncio
async def test_plaintext_agent_hosted_shell_items_without_message_runs_again():
    shell_tool = ShellTool(environment={"type": "container_auto"})
    agent = Agent(name="test", tools=[shell_tool])
    response = ModelResponse(
        output=[
            make_shell_call(
                "call_shell_hosted", id_value="shell_call_hosted", commands=["echo hi"]
            ),
            cast(
                Any,
                {
                    "type": "shell_call_output",
                    "id": "sh_out_hosted",
                    "call_id": "call_shell_hosted",
                    "status": "completed",
                    "output": [
                        {
                            "stdout": "hi\n",
                            "stderr": "",
                            "outcome": {"type": "exit", "exit_code": 0},
                        }
                    ],
                },
            ),
        ],
        usage=Usage(),
        response_id=None,
    )

    result = await get_execute_result(agent, response)

    assert len(result.generated_items) == 2
    assert isinstance(result.generated_items[0], ToolCallItem)
    assert isinstance(result.generated_items[1], ToolCallOutputItem)
    assert isinstance(result.next_step, NextStepRunAgain)


@pytest.mark.asyncio
async def test_plaintext_agent_shell_output_only_without_message_runs_again():
    agent = Agent(name="test")
    response = ModelResponse(
        output=[
            cast(
                Any,
                {
                    "type": "shell_call_output",
                    "id": "sh_out_only",
                    "call_id": "call_shell_only",
                    "status": "completed",
                    "output": [
                        {
                            "stdout": "hi\n",
                            "stderr": "",
                            "outcome": {"type": "exit", "exit_code": 0},
                        }
                    ],
                },
            ),
        ],
        usage=Usage(),
        response_id=None,
    )

    result = await get_execute_result(agent, response)

    assert len(result.generated_items) == 1
    assert isinstance(result.generated_items[0], ToolCallOutputItem)
    assert isinstance(result.next_step, NextStepRunAgain)


@pytest.mark.asyncio
async def test_plaintext_agent_tool_search_only_without_message_runs_again():
    agent = Agent(name="test")
    response = ModelResponse(output=[], usage=Usage(), response_id=None)
    response.output = cast(
        Any,
        [
            {
                "type": "tool_search_call",
                "id": "tsc_step",
                "arguments": {"paths": ["crm"], "query": "profile"},
                "execution": "server",
                "status": "completed",
            },
            {
                "type": "tool_search_output",
                "id": "tso_step",
                "execution": "server",
                "status": "completed",
                "tools": [
                    {
                        "type": "function",
                        "name": "lookup_account",
                        "description": "Look up a CRM account.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "account_id": {
                                    "type": "string",
                                }
                            },
                            "required": ["account_id"],
                        },
                        "defer_loading": True,
                    }
                ],
            },
        ],
    )

    result = await get_execute_result(agent, response)

    assert len(result.generated_items) == 2
    assert getattr(result.generated_items[0].raw_item, "type", None) == "tool_search_call"
    raw_output = result.generated_items[1].raw_item
    assert getattr(raw_output, "type", None) == "tool_search_output"
    assert isinstance(result.next_step, NextStepRunAgain)


@pytest.mark.asyncio
async def test_plaintext_agent_client_tool_search_requires_manual_handling() -> None:
    agent = Agent(name="test")
    response = ModelResponse(output=[], usage=Usage(), response_id=None)
    response.output = cast(
        Any,
        [
            {
                "type": "tool_search_call",
                "id": "tsc_client_step",
                "call_id": "call_tool_search_client",
                "arguments": {"paths": ["crm"], "query": "profile"},
                "execution": "client",
                "status": "completed",
            }
        ],
    )

    with pytest.raises(ModelBehaviorError, match="Client-executed tool_search calls"):
        await get_execute_result(agent, response)


@pytest.mark.asyncio
async def test_plaintext_agent_hosted_shell_with_refusal_message_raises_refusal_error():
    shell_tool = ShellTool(environment={"type": "container_auto"})
    agent = Agent(name="test", tools=[shell_tool])
    refusal_message = ResponseOutputMessage(
        id="msg_refusal",
        type="message",
        role="assistant",
        content=[ResponseOutputRefusal(type="refusal", refusal="I cannot help with that.")],
        status="completed",
    )
    response = ModelResponse(
        output=[
            make_shell_call(
                "call_shell_hosted_refusal",
                id_value="shell_call_hosted_refusal",
                commands=["echo hi"],
            ),
            cast(
                Any,
                {
                    "type": "shell_call_output",
                    "id": "sh_out_hosted_refusal",
                    "call_id": "call_shell_hosted_refusal",
                    "status": "completed",
                    "output": [
                        {
                            "stdout": "hi\n",
                            "stderr": "",
                            "outcome": {"type": "exit", "exit_code": 0},
                        }
                    ],
                },
            ),
            refusal_message,
        ],
        usage=Usage(),
        response_id=None,
    )

    with pytest.raises(ModelRefusalError) as exc_info:
        await get_execute_result(agent, response)

    assert exc_info.value.refusal == "I cannot help with that."


@pytest.mark.asyncio
async def test_multiple_tool_calls():
    agent = Agent(
        name="test",
        tools=[
            get_function_tool(name="test_1", return_value="123"),
            get_function_tool(name="test_2", return_value="456"),
            get_function_tool(name="test_3", return_value="789"),
        ],
    )
    response = ModelResponse(
        output=[
            get_text_message("Hello, world!"),
            get_function_tool_call("test_1"),
            get_function_tool_call("test_2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    result = await get_execute_result(agent, response)
    assert result.original_input == "hello"

    # 5 items: new message, 2 tool calls, 2 tool call outputs
    assert len(result.generated_items) == 5
    assert isinstance(result.next_step, NextStepRunAgain)

    items = result.generated_items
    assert_item_is_message(items[0], "Hello, world!")
    assert_item_is_function_tool_call(items[1], "test_1", None)
    assert_item_is_function_tool_call(items[2], "test_2", None)

    assert isinstance(result.next_step, NextStepRunAgain)


@pytest.mark.asyncio
async def test_multiple_tool_calls_with_tool_context():
    async def _fake_tool(context: ToolContext[str], value: str) -> str:
        return f"{value}-{context.tool_call_id}"

    tool = function_tool(_fake_tool, name_override="fake_tool", failure_error_function=None)

    agent = Agent(
        name="test",
        tools=[tool],
    )
    response = ModelResponse(
        output=[
            get_function_tool_call("fake_tool", json.dumps({"value": "123"}), call_id="1"),
            get_function_tool_call("fake_tool", json.dumps({"value": "456"}), call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    result = await get_execute_result(agent, response)
    assert result.original_input == "hello"

    # 4 items: new message, 2 tool calls, 2 tool call outputs
    assert len(result.generated_items) == 4
    assert isinstance(result.next_step, NextStepRunAgain)

    items = result.generated_items
    assert_item_is_function_tool_call(items[0], "fake_tool", json.dumps({"value": "123"}))
    assert_item_is_function_tool_call(items[1], "fake_tool", json.dumps({"value": "456"}))
    assert_item_is_function_tool_call_output(items[2], "123-1")
    assert_item_is_function_tool_call_output(items[3], "456-2")

    assert isinstance(result.next_step, NextStepRunAgain)


@pytest.mark.asyncio
async def test_multiple_tool_calls_still_raise_when_sibling_failure_error_function_none():
    async def _ok_tool() -> str:
        return "ok"

    async def _error_tool() -> str:
        raise ValueError("boom")

    ok_tool = function_tool(_ok_tool, name_override="ok_tool", failure_error_function=None)
    error_tool = function_tool(
        _error_tool,
        name_override="error_tool",
        failure_error_function=None,
    )

    agent = Agent(name="test", tools=[ok_tool, error_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("ok_tool", "{}", call_id="1"),
            get_function_tool_call("error_tool", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    with pytest.raises(UserError, match="Error running tool error_tool: boom"):
        await get_execute_result(agent, response)


@pytest.mark.asyncio
async def test_function_tool_error_trace_respects_sensitive_data_setting():
    async def _error_tool() -> str:
        raise ValueError("secret-token-123")

    error_tool = function_tool(
        _error_tool,
        name_override="error_tool",
        failure_error_function=None,
    )
    agent = Agent(name="test", tools=[error_tool])
    response = ModelResponse(
        output=[get_function_tool_call("error_tool", "{}", call_id="1")],
        usage=Usage(),
        response_id=None,
    )

    with trace("test"):
        with pytest.raises(UserError, match="Error running tool error_tool: secret-token-123"):
            await get_execute_result(
                agent,
                response,
                run_config=RunConfig(trace_include_sensitive_data=False),
            )

    function_spans = _function_spans()

    assert len(function_spans) == 1
    error = function_spans[0]["error"]
    assert error["message"] == "Error running tool"
    assert error["data"]["tool_name"] == "error_tool"
    assert error["data"]["error"] == "Tool execution failed. Error details are redacted."
    assert "secret-token-123" not in str(error)


@pytest.mark.asyncio
async def test_default_function_tool_error_trace_respects_sensitive_data_setting():
    async def _error_tool() -> str:
        raise ValueError("secret-token-123")

    error_tool = function_tool(_error_tool, name_override="error_tool")
    agent = Agent(name="test", tools=[error_tool])
    response = ModelResponse(
        output=[get_function_tool_call("error_tool", "{}", call_id="1")],
        usage=Usage(),
        response_id=None,
    )

    with trace("test"):
        result = await get_execute_result(
            agent,
            response,
            run_config=RunConfig(trace_include_sensitive_data=False),
        )

    assert len(result.generated_items) == 2
    assert isinstance(result.next_step, NextStepRunAgain)
    assert_item_is_function_tool_call_output(
        result.generated_items[1],
        "An error occurred while running the tool. Please try again. Error: secret-token-123",
    )

    function_spans = _function_spans()

    assert len(function_spans) == 1
    error = function_spans[0]["error"]
    assert error["message"] == "Error running tool (non-fatal)"
    assert error["data"]["tool_name"] == "error_tool"
    assert error["data"]["error"] == "Tool execution failed. Error details are redacted."
    assert "secret-token-123" not in str(error)


@pytest.mark.asyncio
async def test_multiple_tool_calls_still_raise_when_sibling_cancelled():
    async def _ok_tool() -> str:
        return "ok"

    async def _cancel_tool() -> str:
        raise asyncio.CancelledError("tool-cancelled")

    ok_tool = function_tool(_ok_tool, name_override="ok_tool", failure_error_function=None)
    cancel_tool = function_tool(
        _cancel_tool,
        name_override="cancel_tool",
        failure_error_function=None,
    )

    agent = Agent(name="test", tools=[ok_tool, cancel_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("ok_tool", "{}", call_id="1"),
            get_function_tool_call("cancel_tool", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    with pytest.raises(asyncio.CancelledError):
        await get_execute_result(agent, response)


@pytest.mark.asyncio
async def test_multiple_tool_calls_cancel_sibling_when_tool_raises_cancelled_error():
    started = asyncio.Event()
    cancellation_started = asyncio.Event()
    cancellation_finished = asyncio.Event()
    allow_cancellation_exit = asyncio.Event()

    async def _waiting_tool() -> str:
        started.set()
        try:
            await asyncio.Future()
            return "unreachable"
        except asyncio.CancelledError:
            cancellation_started.set()
            await allow_cancellation_exit.wait()
            cancellation_finished.set()
            raise

    async def _cancel_tool() -> str:
        await started.wait()
        raise asyncio.CancelledError("tool-cancelled")

    waiting_tool = function_tool(
        _waiting_tool,
        name_override="waiting_tool",
        failure_error_function=None,
    )
    cancel_tool = function_tool(
        _cancel_tool,
        name_override="cancel_tool",
        failure_error_function=None,
    )

    agent = Agent(name="test", tools=[waiting_tool, cancel_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("waiting_tool", "{}", call_id="1"),
            get_function_tool_call("cancel_tool", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    execution_task = asyncio.create_task(get_execute_result(agent, response))

    await asyncio.wait_for(started.wait(), timeout=0.2)
    await asyncio.wait_for(cancellation_started.wait(), timeout=0.2)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execution_task, timeout=0.2)

    assert not cancellation_finished.is_set()

    allow_cancellation_exit.set()
    await asyncio.wait_for(cancellation_finished.wait(), timeout=0.2)
    assert cancellation_finished.is_set()


@pytest.mark.asyncio
async def test_multiple_tool_calls_use_custom_failure_error_function_for_cancelled_tool():
    async def _ok_tool() -> str:
        return "ok"

    async def _cancel_tool() -> str:
        raise asyncio.CancelledError("tool-cancelled")

    seen_error: Exception | None = None

    def _custom_failure_error(_context: RunContextWrapper[Any], _error: Exception) -> str:
        nonlocal seen_error
        assert isinstance(_error, Exception)
        assert not isinstance(_error, asyncio.CancelledError)
        seen_error = _error
        return "custom-cancel-msg"

    ok_tool = function_tool(_ok_tool, name_override="ok_tool", failure_error_function=None)
    cancel_tool = function_tool(
        _cancel_tool,
        name_override="cancel_tool",
        failure_error_function=_custom_failure_error,
    )

    agent = Agent(name="test", tools=[ok_tool, cancel_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("ok_tool", "{}", call_id="1"),
            get_function_tool_call("cancel_tool", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    result = await get_execute_result(agent, response)

    assert len(result.generated_items) == 4
    assert isinstance(result.next_step, NextStepRunAgain)
    assert_item_is_function_tool_call_output(result.generated_items[2], "ok")
    assert_item_is_function_tool_call_output(result.generated_items[3], "custom-cancel-msg")
    assert seen_error is not None
    assert str(seen_error) == "tool-cancelled"


@pytest.mark.asyncio
async def test_multiple_tool_calls_use_custom_failure_error_function_for_replaced_cancelled_tool():
    async def _ok_tool() -> str:
        return "ok"

    async def _cancel_tool() -> str:
        raise asyncio.CancelledError("tool-cancelled")

    def _custom_failure_error(_context: RunContextWrapper[Any], _error: Exception) -> str:
        return "custom-cancel-msg"

    ok_tool = function_tool(_ok_tool, name_override="ok_tool", failure_error_function=None)
    cancel_tool = dataclasses.replace(
        function_tool(
            _cancel_tool,
            name_override="cancel_tool",
            failure_error_function=_custom_failure_error,
        ),
        name="cancel_tool",
    )

    agent = Agent(name="test", tools=[ok_tool, cancel_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("ok_tool", "{}", call_id="1"),
            get_function_tool_call("cancel_tool", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    result = await get_execute_result(agent, response)

    assert len(result.generated_items) == 4
    assert isinstance(result.next_step, NextStepRunAgain)
    assert_item_is_function_tool_call_output(result.generated_items[2], "ok")
    assert_item_is_function_tool_call_output(result.generated_items[3], "custom-cancel-msg")


@pytest.mark.asyncio
async def test_multiple_tool_calls_use_default_failure_error_function_for_copied_cancelled_tool():
    async def _ok_tool() -> str:
        return "ok"

    async def _cancel_tool() -> str:
        raise asyncio.CancelledError("tool-cancelled")

    ok_tool = function_tool(_ok_tool, name_override="ok_tool", failure_error_function=None)
    cancel_tool = copy.deepcopy(function_tool(_cancel_tool, name_override="cancel_tool"))

    agent = Agent(name="test", tools=[ok_tool, cancel_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("ok_tool", "{}", call_id="1"),
            get_function_tool_call("cancel_tool", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    result = await get_execute_result(agent, response)

    assert len(result.generated_items) == 4
    assert isinstance(result.next_step, NextStepRunAgain)
    assert_item_is_function_tool_call_output(result.generated_items[2], "ok")
    assert_item_is_function_tool_call_output(
        result.generated_items[3],
        "An error occurred while running the tool. Please try again. Error: tool-cancelled",
    )


@pytest.mark.asyncio
async def test_multiple_tool_calls_use_default_failure_error_function_for_manual_cancelled_tool():
    async def _ok_tool() -> str:
        return "ok"

    async def _manual_on_invoke_tool(_ctx: ToolContext[Any], _args: str) -> str:
        raise asyncio.CancelledError("manual-tool-cancelled")

    ok_tool = function_tool(_ok_tool, name_override="ok_tool", failure_error_function=None)
    manual_tool = FunctionTool(
        name="manual_cancel_tool",
        description="manual cancel",
        params_json_schema={},
        on_invoke_tool=_manual_on_invoke_tool,
    )

    agent = Agent(name="test", tools=[ok_tool, manual_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("ok_tool", "{}", call_id="1"),
            get_function_tool_call("manual_cancel_tool", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    result = await get_execute_result(agent, response)

    assert len(result.generated_items) == 4
    assert isinstance(result.next_step, NextStepRunAgain)
    assert_item_is_function_tool_call_output(result.generated_items[2], "ok")
    assert_item_is_function_tool_call_output(
        result.generated_items[3],
        "An error occurred while running the tool. Please try again. Error: manual-tool-cancelled",
    )


@pytest.mark.asyncio
async def test_single_tool_call_uses_default_failure_error_function_for_cancelled_tool():
    async def _cancel_tool() -> str:
        raise asyncio.CancelledError("tool-cancelled")

    cancel_tool = function_tool(_cancel_tool, name_override="cancel_tool")
    agent = Agent(name="test", tools=[cancel_tool])
    response = ModelResponse(
        output=[get_function_tool_call("cancel_tool", "{}", call_id="1")],
        usage=Usage(),
        response_id=None,
    )

    result = await get_execute_result(agent, response)

    assert len(result.generated_items) == 2
    assert isinstance(result.next_step, NextStepRunAgain)
    assert_item_is_function_tool_call_output(
        result.generated_items[1],
        "An error occurred while running the tool. Please try again. Error: tool-cancelled",
    )


@pytest.mark.asyncio
async def test_cancelled_function_tool_error_trace_respects_sensitive_data_setting():
    async def _cancel_tool() -> str:
        raise asyncio.CancelledError("secret-token-123")

    cancel_tool = function_tool(_cancel_tool, name_override="cancel_tool")
    agent = Agent(name="test", tools=[cancel_tool])
    response = ModelResponse(
        output=[get_function_tool_call("cancel_tool", "{}", call_id="1")],
        usage=Usage(),
        response_id=None,
    )

    with trace("test"):
        result = await get_execute_result(
            agent,
            response,
            run_config=RunConfig(trace_include_sensitive_data=False),
        )

    assert len(result.generated_items) == 2
    assert isinstance(result.next_step, NextStepRunAgain)
    assert_item_is_function_tool_call_output(
        result.generated_items[1],
        "An error occurred while running the tool. Please try again. Error: secret-token-123",
    )

    function_spans = _function_spans()

    assert len(function_spans) == 1
    error = function_spans[0]["error"]
    assert error["message"] == "Tool execution cancelled"
    assert error["data"]["tool_name"] == "cancel_tool"
    assert error["data"]["error"] == "Tool execution failed. Error details are redacted."
    assert "secret-token-123" not in str(error)


@pytest.mark.asyncio
async def test_multiple_tool_calls_surface_hook_failure_over_sibling_cancellation():
    hook_started = asyncio.Event()

    class FailingHooks(RunHooks[Any]):
        async def on_tool_end(
            self,
            context: RunContextWrapper[Any],
            agent: Agent[Any],
            tool,
            result: str,
        ) -> None:
            if tool.name != "ok_tool":
                return

            hook_started.set()
            raise ValueError("hook boom")

    async def _ok_tool() -> str:
        return "ok"

    async def _cancel_tool() -> str:
        await hook_started.wait()
        raise asyncio.CancelledError("tool-cancelled")

    hooks = FailingHooks()
    ok_tool = function_tool(_ok_tool, name_override="ok_tool", failure_error_function=None)
    cancel_tool = function_tool(
        _cancel_tool,
        name_override="cancel_tool",
        failure_error_function=None,
    )

    agent = Agent(name="test", tools=[ok_tool, cancel_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("ok_tool", "{}", call_id="1"),
            get_function_tool_call("cancel_tool", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    with pytest.raises(UserError, match="Error running tool ok_tool: hook boom"):
        await get_execute_result(agent, response, hooks=hooks)


@pytest.mark.asyncio
async def test_multiple_tool_calls_surface_output_guardrail_failure_over_sibling_cancellation():
    guardrail_started = asyncio.Event()

    @tool_output_guardrail
    async def tripwire_guardrail(
        data: ToolOutputGuardrailData,
    ) -> ToolGuardrailFunctionOutput:
        guardrail_started.set()
        return ToolGuardrailFunctionOutput.raise_exception(
            output_info={"tool": data.context.tool_name}
        )

    async def _ok_tool() -> str:
        return "ok"

    async def _cancel_tool() -> str:
        await guardrail_started.wait()
        raise asyncio.CancelledError("tool-cancelled")

    ok_tool = function_tool(
        _ok_tool,
        name_override="ok_tool",
        failure_error_function=None,
        tool_output_guardrails=[tripwire_guardrail],
    )
    cancel_tool = function_tool(
        _cancel_tool,
        name_override="cancel_tool",
        failure_error_function=None,
    )

    agent = Agent(name="test", tools=[ok_tool, cancel_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("ok_tool", "{}", call_id="1"),
            get_function_tool_call("cancel_tool", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    with pytest.raises(ToolOutputGuardrailTripwireTriggered):
        await get_execute_result(agent, response)


@pytest.mark.asyncio
async def test_function_tool_preserves_contextvar_from_tool_body_to_post_invoke_hooks():
    tool_state: ContextVar[str] = ContextVar("tool_state", default="unset")
    seen_values: list[tuple[str, str]] = []

    @tool_output_guardrail
    async def record_guardrail(_data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        seen_values.append(("guardrail", tool_state.get()))
        return ToolGuardrailFunctionOutput.allow(output_info="checked")

    class RecordingHooks(RunHooks[Any]):
        async def on_tool_end(
            self,
            context: RunContextWrapper[Any],
            agent: Agent[Any],
            tool,
            result: str,
        ) -> None:
            seen_values.append(("hook", tool_state.get()))

    async def _context_tool() -> str:
        tool_state.set("from-tool")
        return "ok"

    hooks = RecordingHooks()
    context_tool = function_tool(
        _context_tool,
        name_override="context_tool",
        tool_output_guardrails=[record_guardrail],
    )
    agent = Agent(name="test", tools=[context_tool])
    response = ModelResponse(
        output=[get_function_tool_call("context_tool", "{}", call_id="1")],
        usage=Usage(),
        response_id=None,
    )

    result = await get_execute_result(agent, response, hooks=hooks)

    assert isinstance(result.next_step, NextStepRunAgain)
    assert_item_is_function_tool_call_output(result.generated_items[1], "ok")
    assert seen_values == [("guardrail", "from-tool"), ("hook", "from-tool")]
    assert tool_state.get() == "unset"


@pytest.mark.asyncio
async def test_mixed_tool_calls_preserve_shell_output_when_function_tool_cancelled():
    async def _cancel_tool() -> str:
        raise asyncio.CancelledError("tool-cancelled")

    cancel_tool = function_tool(_cancel_tool, name_override="cancel_tool")
    shell_tool = ShellTool(executor=lambda _request: "shell ok")
    agent = Agent(name="test", tools=[cancel_tool, shell_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("cancel_tool", "{}", call_id="fn-1"),
            make_shell_call("shell-1"),
        ],
        usage=Usage(),
        response_id=None,
    )

    result = await get_execute_result(agent, response)

    assert len(result.generated_items) == 4
    assert isinstance(result.next_step, NextStepRunAgain)
    assert_item_is_function_tool_call_output(
        result.generated_items[2],
        "An error occurred while running the tool. Please try again. Error: tool-cancelled",
    )
    shell_output = cast(ToolCallOutputItem, result.generated_items[3])
    assert shell_output.output == "shell ok"
    assert cast(dict[str, Any], shell_output.raw_item)["type"] == "shell_call_output"


@pytest.mark.asyncio
async def test_multiple_tool_calls_still_raise_tool_timeout_error():
    async def _ok_tool() -> str:
        return "ok"

    async def _slow_tool() -> str:
        await asyncio.sleep(0.2)
        return "slow"

    ok_tool = function_tool(_ok_tool, name_override="ok_tool", failure_error_function=None)
    slow_tool = function_tool(
        _slow_tool,
        name_override="slow_tool",
        timeout=0.01,
        timeout_behavior="raise_exception",
    )

    agent = Agent(name="test", tools=[ok_tool, slow_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("ok_tool", "{}", call_id="1"),
            get_function_tool_call("slow_tool", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    with pytest.raises(ToolTimeoutError, match="timed out"):
        await get_execute_result(agent, response)


@pytest.mark.asyncio
async def test_multiple_tool_calls_still_raise_model_behavior_error_when_failure_error_none():
    async def _ok_tool() -> str:
        return "ok"

    def _echo(value: str) -> str:
        return value

    ok_tool = function_tool(_ok_tool, name_override="ok_tool", failure_error_function=None)
    guarded_tool = function_tool(
        _echo,
        name_override="guarded_tool",
        failure_error_function=None,
    )

    agent = Agent(name="test", tools=[ok_tool, guarded_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("ok_tool", "{}", call_id="1"),
            get_function_tool_call("guarded_tool", "bad_json", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    with pytest.raises(ModelBehaviorError, match="Invalid JSON input for tool guarded_tool"):
        await get_execute_result(agent, response)


@pytest.mark.asyncio
async def test_multiple_tool_calls_do_not_run_on_tool_end_for_cancelled_tool():
    ok_tool_end_called = asyncio.Event()

    class RecordingHooks(RunHooks[Any]):
        def __init__(self):
            self.results: dict[str, str] = {}

        async def on_tool_end(
            self,
            context: RunContextWrapper[Any],
            agent: Agent[Any],
            tool,
            result: str,
        ) -> None:
            self.results[tool.name] = result
            if tool.name == "ok_tool":
                ok_tool_end_called.set()

    async def _ok_tool() -> str:
        return "ok"

    async def _cancel_tool() -> str:
        await ok_tool_end_called.wait()
        raise asyncio.CancelledError("tool-cancelled")

    hooks = RecordingHooks()
    ok_tool = function_tool(_ok_tool, name_override="ok_tool", failure_error_function=None)
    cancel_tool = function_tool(
        _cancel_tool,
        name_override="cancel_tool",
        failure_error_function=None,
    )

    agent = Agent(name="test", tools=[ok_tool, cancel_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("ok_tool", "{}", call_id="1"),
            get_function_tool_call("cancel_tool", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    with pytest.raises(asyncio.CancelledError):
        await get_execute_result(agent, response, hooks=hooks)

    assert hooks.results == {
        "ok_tool": "ok",
    }


@pytest.mark.asyncio
async def test_multiple_tool_calls_skip_post_invoke_work_for_cancelled_sibling_teardown():
    waiting_tool_started = asyncio.Event()
    failure_handler_called = asyncio.Event()
    output_guardrail_called = asyncio.Event()
    on_tool_end_called = asyncio.Event()

    @tool_output_guardrail
    async def allow_output_guardrail(
        data: ToolOutputGuardrailData,
    ) -> ToolGuardrailFunctionOutput:
        output_guardrail_called.set()
        return ToolGuardrailFunctionOutput.allow(output_info={"echo": data.output})

    class RecordingHooks(RunHooks[Any]):
        async def on_tool_end(
            self,
            context: RunContextWrapper[Any],
            agent: Agent[Any],
            tool,
            result: str,
        ) -> None:
            if tool.name == "waiting_tool":
                on_tool_end_called.set()

    async def _waiting_tool() -> str:
        waiting_tool_started.set()
        await asyncio.Future()
        return "unreachable"

    async def _error_tool() -> str:
        await waiting_tool_started.wait()
        raise ValueError("boom")

    def _failure_handler(_ctx: RunContextWrapper[Any], error: Exception) -> str:
        failure_handler_called.set()
        return f"handled:{error}"

    waiting_tool = function_tool(
        _waiting_tool,
        name_override="waiting_tool",
        failure_error_function=_failure_handler,
        tool_output_guardrails=[allow_output_guardrail],
    )
    error_tool = function_tool(
        _error_tool,
        name_override="error_tool",
        failure_error_function=None,
    )

    agent = Agent(name="test", tools=[waiting_tool, error_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("waiting_tool", "{}", call_id="1"),
            get_function_tool_call("error_tool", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    with pytest.raises(UserError, match="Error running tool error_tool: boom"):
        await get_execute_result(agent, response, hooks=RecordingHooks())

    await asyncio.sleep(0)

    assert not failure_handler_called.is_set()
    assert not output_guardrail_called.is_set()
    assert not on_tool_end_called.is_set()


@pytest.mark.asyncio
async def test_execute_function_tool_calls_parent_cancellation_skips_post_invoke_work():
    tool_started = asyncio.Event()
    failure_handler_called = asyncio.Event()
    output_guardrail_called = asyncio.Event()
    on_tool_end_called = asyncio.Event()

    @tool_output_guardrail
    async def allow_output_guardrail(
        data: ToolOutputGuardrailData,
    ) -> ToolGuardrailFunctionOutput:
        output_guardrail_called.set()
        return ToolGuardrailFunctionOutput.allow(output_info={"echo": data.output})

    class RecordingHooks(RunHooks[Any]):
        async def on_tool_end(
            self,
            context: RunContextWrapper[Any],
            agent: Agent[Any],
            tool,
            result: str,
        ) -> None:
            on_tool_end_called.set()

    async def _waiting_tool() -> str:
        tool_started.set()
        await asyncio.Future()
        return "unreachable"

    def _failure_handler(_ctx: RunContextWrapper[Any], error: Exception) -> str:
        failure_handler_called.set()
        return f"handled:{error}"

    tool = function_tool(
        _waiting_tool,
        name_override="waiting_tool",
        failure_error_function=_failure_handler,
        tool_output_guardrails=[allow_output_guardrail],
    )
    agent = Agent(name="test", tools=[tool])
    tool_runs = [
        ToolRunFunction(
            tool_call=cast(
                ResponseFunctionToolCall,
                get_function_tool_call("waiting_tool", "{}", call_id="1"),
            ),
            function_tool=tool,
        )
    ]

    execution_task = asyncio.create_task(
        execute_function_tool_calls(
            bindings=bind_public_agent(agent),
            tool_runs=tool_runs,
            hooks=RecordingHooks(),
            context_wrapper=RunContextWrapper(None),
            config=RunConfig(),
            isolate_parallel_failures=True,
        )
    )
    await asyncio.wait_for(tool_started.wait(), timeout=0.2)

    execution_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execution_task, timeout=0.1)

    await asyncio.sleep(0)

    assert not failure_handler_called.is_set()
    assert not output_guardrail_called.is_set()
    assert not on_tool_end_called.is_set()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not hasattr(asyncio, "eager_task_factory"),
    reason="eager_task_factory requires Python 3.12+",
)
async def test_execute_function_tool_calls_eager_task_factory_tracks_state_safely():
    async def _first_tool() -> str:
        return "first"

    async def _second_tool() -> str:
        return "second"

    first_tool = function_tool(_first_tool, name_override="first_tool")
    second_tool = function_tool(_second_tool, name_override="second_tool")
    tool_runs = [
        ToolRunFunction(
            tool_call=cast(
                ResponseFunctionToolCall,
                get_function_tool_call("first_tool", "{}", call_id="call-1"),
            ),
            function_tool=first_tool,
        ),
        ToolRunFunction(
            tool_call=cast(
                ResponseFunctionToolCall,
                get_function_tool_call("second_tool", "{}", call_id="call-2"),
            ),
            function_tool=second_tool,
        ),
    ]
    loop = asyncio.get_running_loop()
    previous_task_factory = loop.get_task_factory()
    eager_task_factory = cast(Any, asyncio.eager_task_factory)
    loop.set_task_factory(eager_task_factory)

    try:
        (
            function_results,
            input_guardrail_results,
            output_guardrail_results,
        ) = await execute_function_tool_calls(
            bindings=bind_public_agent(Agent(name="test", tools=[first_tool, second_tool])),
            tool_runs=tool_runs,
            hooks=RunHooks(),
            context_wrapper=RunContextWrapper(None),
            config=RunConfig(),
        )
    finally:
        loop.set_task_factory(previous_task_factory)

    assert [result.output for result in function_results] == ["first", "second"]
    assert input_guardrail_results == []
    assert output_guardrail_results == []


@pytest.mark.asyncio
async def test_function_tool_disabled_before_execution_fails_before_starting_siblings() -> None:
    enabled_checks: list[bool] = []
    disabled_tool_invocations = 0
    sibling_tool_invocations = 0

    def _is_lookup_enabled(_ctx: RunContextWrapper[Any], _agent: AgentBase[Any]) -> bool:
        enabled = not enabled_checks
        enabled_checks.append(enabled)
        return enabled

    @function_tool(name_override="lookup_secret", is_enabled=_is_lookup_enabled)
    def lookup_secret() -> str:
        nonlocal disabled_tool_invocations
        disabled_tool_invocations += 1
        return "secret"

    @function_tool(name_override="record_side_effect")
    def record_side_effect() -> str:
        nonlocal sibling_tool_invocations
        sibling_tool_invocations += 1
        return "recorded"

    agent = Agent(name="test", tools=[lookup_secret, record_side_effect])
    response = ModelResponse(
        output=[
            get_function_tool_call("lookup_secret", "{}", call_id="call-1"),
            get_function_tool_call("record_side_effect", "{}", call_id="call-2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    with pytest.raises(ModelBehaviorError, match="lookup_secret is currently disabled"):
        await get_execute_result(agent, response)

    assert enabled_checks == [True, False]
    assert disabled_tool_invocations == 0
    assert sibling_tool_invocations == 0


@pytest.mark.asyncio
async def test_execute_function_tool_calls_allows_non_agent_function_tool() -> None:
    @function_tool(name_override="synthetic_tool")
    def synthetic_tool() -> str:
        return "synthetic-result"

    tool_run = ToolRunFunction(
        tool_call=cast(
            ResponseFunctionToolCall,
            get_function_tool_call("synthetic_tool", "{}", call_id="call-1"),
        ),
        function_tool=synthetic_tool,
    )

    (
        function_results,
        input_guardrail_results,
        output_guardrail_results,
    ) = await execute_function_tool_calls(
        bindings=bind_public_agent(Agent(name="test", tools=[])),
        tool_runs=[tool_run],
        hooks=RunHooks(),
        context_wrapper=RunContextWrapper(None),
        config=RunConfig(),
    )

    assert [result.output for result in function_results] == ["synthetic-result"]
    assert input_guardrail_results == []
    assert output_guardrail_results == []


@pytest.mark.asyncio
async def test_execute_function_tool_calls_collapse_trace_name_for_top_level_deferred_tools():
    async def _shipping_eta(tracking_number: str) -> str:
        return f"eta:{tracking_number}"

    tool = function_tool(
        _shipping_eta,
        name_override="get_shipping_eta",
        defer_loading=True,
    )
    tool_run = ToolRunFunction(
        tool_call=cast(
            ResponseFunctionToolCall,
            get_function_tool_call(
                "get_shipping_eta",
                '{"tracking_number":"ZX-123"}',
                call_id="call-1",
                namespace="get_shipping_eta",
            ),
        ),
        function_tool=tool,
    )

    with trace("test_execute_function_tool_calls_collapse_trace_name_for_top_level_deferred_tools"):
        await execute_function_tool_calls(
            bindings=bind_public_agent(Agent(name="test", tools=[tool])),
            tool_runs=[tool_run],
            hooks=RunHooks(),
            context_wrapper=RunContextWrapper(None),
            config=RunConfig(),
        )

    assert "get_shipping_eta" in _function_span_names()
    assert "get_shipping_eta.get_shipping_eta" not in _function_span_names()


@pytest.mark.asyncio
async def test_execute_function_tool_calls_preserve_trace_name_for_explicit_namespace():
    async def _shipping_eta(tracking_number: str) -> str:
        return f"eta:{tracking_number}"

    tool = tool_namespace(
        name="shipping",
        description="Shipping tools",
        tools=[
            function_tool(
                _shipping_eta,
                name_override="get_shipping_eta",
                defer_loading=True,
            )
        ],
    )[0]
    tool_run = ToolRunFunction(
        tool_call=cast(
            ResponseFunctionToolCall,
            get_function_tool_call(
                "get_shipping_eta",
                '{"tracking_number":"ZX-123"}',
                call_id="call-1",
                namespace="shipping",
            ),
        ),
        function_tool=tool,
    )

    with trace("test_execute_function_tool_calls_preserve_trace_name_for_explicit_namespace"):
        await execute_function_tool_calls(
            bindings=bind_public_agent(Agent(name="test", tools=[tool])),
            tool_runs=[tool_run],
            hooks=RunHooks(),
            context_wrapper=RunContextWrapper(None),
            config=RunConfig(),
        )

    assert "shipping.get_shipping_eta" in _function_span_names()
    assert "get_shipping_eta" not in _function_span_names()


@pytest.mark.asyncio
async def test_execute_function_tool_calls_rejects_reserved_same_name_namespace_shape():
    async def _lookup_account(customer_id: str) -> str:
        return f"account:{customer_id}"

    with pytest.raises(UserError, match="synthetic namespace `lookup_account.lookup_account`"):
        tool_namespace(
            name="lookup_account",
            description="Same-name namespace",
            tools=[
                function_tool(
                    _lookup_account,
                    name_override="lookup_account",
                    defer_loading=True,
                )
            ],
        )


@pytest.mark.asyncio
async def test_single_tool_call_still_raises_normal_exception():
    async def _error_tool() -> str:
        raise ValueError("boom")

    error_tool = function_tool(
        _error_tool,
        name_override="error_tool",
        failure_error_function=None,
    )

    agent = Agent(name="test", tools=[error_tool])
    response = ModelResponse(
        output=[get_function_tool_call("error_tool", "{}", call_id="1")],
        usage=Usage(),
        response_id=None,
    )

    with pytest.raises(UserError, match="Error running tool error_tool: boom"):
        await get_execute_result(agent, response)


@pytest.mark.asyncio
async def test_single_tool_call_still_raises_cancelled_error():
    async def _cancel_tool() -> str:
        raise asyncio.CancelledError("solo-cancel")

    cancel_tool = function_tool(
        _cancel_tool,
        name_override="cancel_tool",
        failure_error_function=None,
    )

    agent = Agent(name="test", tools=[cancel_tool])
    response = ModelResponse(
        output=[get_function_tool_call("cancel_tool", "{}", call_id="1")],
        usage=Usage(),
        response_id=None,
    )

    with pytest.raises(asyncio.CancelledError):
        await get_execute_result(agent, response)


@pytest.mark.asyncio
async def test_multiple_tool_calls_allow_exception_objects_as_tool_outputs():
    async def _returns_exception() -> ValueError:
        return ValueError("as data")

    async def _ok_tool() -> str:
        return "ok"

    returning_tool = function_tool(
        _returns_exception,
        name_override="returns_exception",
        failure_error_function=None,
    )
    ok_tool = function_tool(_ok_tool, name_override="ok_tool", failure_error_function=None)

    agent = Agent(name="test", tools=[returning_tool, ok_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("returns_exception", "{}", call_id="1"),
            get_function_tool_call("ok_tool", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    result = await get_execute_result(agent, response)

    assert len(result.generated_items) == 4
    assert isinstance(result.next_step, NextStepRunAgain)
    assert_item_is_function_tool_call_output(result.generated_items[2], "as data")
    assert_item_is_function_tool_call_output(result.generated_items[3], "ok")


@pytest.mark.asyncio
async def test_multiple_tool_calls_still_raise_non_cancellation_base_exceptions():
    class ToolAborted(BaseException):
        pass

    async def _ok_tool() -> str:
        return "ok"

    async def _aborting_tool() -> str:
        raise ToolAborted()

    ok_tool = function_tool(_ok_tool, name_override="ok_tool", failure_error_function=None)
    aborting_tool = function_tool(
        _aborting_tool,
        name_override="aborting_tool",
        failure_error_function=None,
    )

    agent = Agent(name="test", tools=[ok_tool, aborting_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("ok_tool", "{}", call_id="1"),
            get_function_tool_call("aborting_tool", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    with pytest.raises(ToolAborted):
        await get_execute_result(agent, response)


@pytest.mark.asyncio
async def test_multiple_tool_calls_prioritize_fatal_base_exception_over_user_error(
    monkeypatch: pytest.MonkeyPatch,
):
    class ToolAborted(BaseException):
        pass

    async def _user_error_tool() -> str:
        raise UserError("non-fatal")

    async def _fatal_tool() -> str:
        raise ToolAborted("fatal")

    user_error_tool = function_tool(
        _user_error_tool,
        name_override="user_error_tool",
        failure_error_function=None,
    )
    fatal_tool = function_tool(
        _fatal_tool,
        name_override="fatal_tool",
        failure_error_function=None,
    )

    original_wait = asyncio.wait

    async def _wait_with_non_fatal_task_first(*args: Any, **kwargs: Any) -> tuple[Any, Any]:
        kwargs = dict(kwargs)
        kwargs["return_when"] = asyncio.ALL_COMPLETED
        done_tasks, pending_tasks = await original_wait(*args, **kwargs)
        ordered_done_tasks = sorted(
            done_tasks,
            key=lambda task: 0 if isinstance(task.exception(), UserError) else 1,
        )
        return ordered_done_tasks, pending_tasks

    monkeypatch.setattr(asyncio, "wait", _wait_with_non_fatal_task_first)

    agent = Agent(name="test", tools=[user_error_tool, fatal_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("user_error_tool", "{}", call_id="1"),
            get_function_tool_call("fatal_tool", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    with pytest.raises(ToolAborted, match="fatal"):
        await get_execute_result(agent, response)


@pytest.mark.asyncio
async def test_multiple_tool_calls_prioritize_tool_error_over_same_batch_cancelled_error(
    monkeypatch: pytest.MonkeyPatch,
):
    async def _cancel_tool() -> str:
        raise asyncio.CancelledError("tool-cancelled")

    async def _error_tool() -> str:
        raise ValueError("boom")

    cancel_tool = function_tool(
        _cancel_tool,
        name_override="cancel_tool",
        failure_error_function=None,
    )
    error_tool = function_tool(
        _error_tool,
        name_override="error_tool",
        failure_error_function=None,
    )

    original_wait = asyncio.wait

    async def _wait_with_cancelled_task_first(*args: Any, **kwargs: Any) -> tuple[Any, Any]:
        kwargs = dict(kwargs)
        kwargs["return_when"] = asyncio.ALL_COMPLETED
        done_tasks, pending_tasks = await original_wait(*args, **kwargs)
        ordered_done_tasks = sorted(
            done_tasks,
            key=lambda task: 0 if task.cancelled() else 1,
        )
        return ordered_done_tasks, pending_tasks

    monkeypatch.setattr(asyncio, "wait", _wait_with_cancelled_task_first)

    agent = Agent(name="test", tools=[cancel_tool, error_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("cancel_tool", "{}", call_id="1"),
            get_function_tool_call("error_tool", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    with pytest.raises(UserError, match="Error running tool error_tool: boom"):
        await get_execute_result(agent, response)


@pytest.mark.asyncio
async def test_multiple_tool_calls_preserve_tool_call_order_for_same_batch_failures(
    monkeypatch: pytest.MonkeyPatch,
):
    async def _error_tool_1() -> str:
        raise ValueError("boom-1")

    async def _error_tool_2() -> str:
        raise ValueError("boom-2")

    tool_1 = function_tool(
        _error_tool_1,
        name_override="error_tool_1",
        failure_error_function=None,
    )
    tool_2 = function_tool(
        _error_tool_2,
        name_override="error_tool_2",
        failure_error_function=None,
    )

    original_wait = asyncio.wait

    async def _wait_with_reversed_done_order(*args: Any, **kwargs: Any) -> tuple[Any, Any]:
        kwargs = dict(kwargs)
        kwargs["return_when"] = asyncio.ALL_COMPLETED
        done_tasks, pending_tasks = await original_wait(*args, **kwargs)
        return list(reversed(list(done_tasks))), pending_tasks

    monkeypatch.setattr(asyncio, "wait", _wait_with_reversed_done_order)

    agent = Agent(name="test", tools=[tool_1, tool_2])
    response = ModelResponse(
        output=[
            get_function_tool_call("error_tool_1", "{}", call_id="1"),
            get_function_tool_call("error_tool_2", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    with pytest.raises(UserError, match="Error running tool error_tool_1: boom-1"):
        await get_execute_result(agent, response)


@pytest.mark.asyncio
async def test_multiple_tool_calls_allow_successful_sibling_on_tool_end_to_finish():
    cleanup_started = asyncio.Event()
    cleanup_finished = asyncio.Event()
    cleanup_release = asyncio.Event()

    class RecordingHooks(RunHooks[Any]):
        async def on_tool_end(
            self,
            context: RunContextWrapper[Any],
            agent: Agent[Any],
            tool,
            result: str,
        ) -> None:
            if tool.name != "ok_tool":
                return

            cleanup_started.set()
            await cleanup_release.wait()
            cleanup_finished.set()

    async def _ok_tool() -> str:
        return "ok"

    async def _error_tool() -> str:
        await cleanup_started.wait()
        raise ValueError("boom")

    hooks = RecordingHooks()
    ok_tool = function_tool(_ok_tool, name_override="ok_tool", failure_error_function=None)
    error_tool = function_tool(
        _error_tool,
        name_override="error_tool",
        failure_error_function=None,
    )

    agent = Agent(name="test", tools=[ok_tool, error_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("ok_tool", "{}", call_id="1"),
            get_function_tool_call("error_tool", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    execution_task = asyncio.create_task(get_execute_result(agent, response, hooks=hooks))
    await asyncio.wait_for(cleanup_started.wait(), timeout=0.2)

    with pytest.raises(UserError, match="Error running tool error_tool: boom"):
        await asyncio.wait_for(execution_task, timeout=0.2)

    assert not cleanup_finished.is_set()
    cleanup_release.set()
    await asyncio.wait_for(cleanup_finished.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_multiple_tool_calls_surface_post_invoke_failure_unblocked_during_settle_turns():
    loop = asyncio.get_running_loop()
    original_handler = loop.get_exception_handler()
    unhandled_contexts: list[dict[str, Any]] = []
    guardrail_started = asyncio.Event()
    release_guardrail = asyncio.Event()

    def _exception_handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        unhandled_contexts.append(context)

    @tool_output_guardrail
    async def externally_released_tripwire_guardrail(
        _data: ToolOutputGuardrailData,
    ) -> ToolGuardrailFunctionOutput:
        guardrail_started.set()
        await release_guardrail.wait()
        return ToolGuardrailFunctionOutput.raise_exception(output_info={"status": "late-tripwire"})

    async def _ok_tool() -> str:
        return "ok"

    async def _error_tool() -> str:
        await guardrail_started.wait()

        async def _release_guardrail_later() -> None:
            await asyncio.sleep(0)
            release_guardrail.set()

        asyncio.create_task(_release_guardrail_later())
        raise ValueError("boom")

    ok_tool = function_tool(
        _ok_tool,
        name_override="ok_tool",
        failure_error_function=None,
        tool_output_guardrails=[externally_released_tripwire_guardrail],
    )
    error_tool = function_tool(
        _error_tool,
        name_override="error_tool",
        failure_error_function=None,
    )

    agent = Agent(name="test", tools=[ok_tool, error_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("ok_tool", "{}", call_id="1"),
            get_function_tool_call("error_tool", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    loop.set_exception_handler(_exception_handler)
    try:
        with pytest.raises(ToolOutputGuardrailTripwireTriggered):
            await asyncio.wait_for(get_execute_result(agent, response), timeout=0.2)
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(original_handler)

    assert not any(
        context.get("message")
        == "Background function tool post-invoke task raised after failure propagation."
        for context in unhandled_contexts
    )


@pytest.mark.asyncio
async def test_multiple_tool_calls_surface_sleeping_post_invoke_failure_before_sibling_error():
    loop = asyncio.get_running_loop()
    original_handler = loop.get_exception_handler()
    unhandled_contexts: list[dict[str, Any]] = []

    @tool_output_guardrail
    async def sleeping_tripwire_guardrail(
        _data: ToolOutputGuardrailData,
    ) -> ToolGuardrailFunctionOutput:
        await asyncio.sleep(0.05)
        return ToolGuardrailFunctionOutput.raise_exception(output_info={"status": "sleep-tripwire"})

    async def _ok_tool() -> str:
        return "ok"

    async def _error_tool() -> str:
        raise ValueError("boom")

    ok_tool = function_tool(
        _ok_tool,
        name_override="ok_tool",
        failure_error_function=None,
        tool_output_guardrails=[sleeping_tripwire_guardrail],
    )
    error_tool = function_tool(
        _error_tool,
        name_override="error_tool",
        failure_error_function=None,
    )

    agent = Agent(name="test", tools=[ok_tool, error_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("ok_tool", "{}", call_id="1"),
            get_function_tool_call("error_tool", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    def _exception_handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        unhandled_contexts.append(context)

    loop.set_exception_handler(_exception_handler)
    try:
        with pytest.raises(ToolOutputGuardrailTripwireTriggered):
            await asyncio.wait_for(get_execute_result(agent, response), timeout=0.2)
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(original_handler)

    assert not any(
        context.get("message")
        == "Background function tool post-invoke task raised after failure propagation."
        for context in unhandled_contexts
    )


@pytest.mark.asyncio
async def test_multiple_tool_calls_do_not_wait_indefinitely_for_sleeping_post_invoke_sibling():
    guardrail_finished = asyncio.Event()

    @tool_output_guardrail
    async def long_sleeping_guardrail(
        _data: ToolOutputGuardrailData,
    ) -> ToolGuardrailFunctionOutput:
        await asyncio.sleep(0.3)
        guardrail_finished.set()
        return ToolGuardrailFunctionOutput.allow(output_info="done")

    async def _ok_tool() -> str:
        return "ok"

    async def _error_tool() -> str:
        raise ValueError("boom")

    ok_tool = function_tool(
        _ok_tool,
        name_override="ok_tool",
        failure_error_function=None,
        tool_output_guardrails=[long_sleeping_guardrail],
    )
    error_tool = function_tool(
        _error_tool,
        name_override="error_tool",
        failure_error_function=None,
    )

    agent = Agent(name="test", tools=[ok_tool, error_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("ok_tool", "{}", call_id="1"),
            get_function_tool_call("error_tool", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    with pytest.raises(UserError, match="Error running tool error_tool: boom"):
        await asyncio.wait_for(get_execute_result(agent, response), timeout=0.2)

    await asyncio.wait_for(guardrail_finished.wait(), timeout=0.5)


@pytest.mark.asyncio
async def test_multiple_tool_calls_do_not_wait_for_cancelled_sibling_tool_before_raising():
    started = asyncio.Event()
    cancellation_started = asyncio.Event()
    cancellation_finished = asyncio.Event()
    allow_cancellation_exit = asyncio.Event()

    async def _ok_tool() -> str:
        started.set()
        try:
            await asyncio.Future()
            return "unreachable"
        except asyncio.CancelledError:
            cancellation_started.set()
            await allow_cancellation_exit.wait()
            cancellation_finished.set()
            raise

    async def _error_tool() -> str:
        await started.wait()
        raise ValueError("boom")

    ok_tool = function_tool(_ok_tool, name_override="ok_tool", failure_error_function=None)
    error_tool = function_tool(
        _error_tool,
        name_override="error_tool",
        failure_error_function=None,
    )

    agent = Agent(name="test", tools=[ok_tool, error_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("ok_tool", "{}", call_id="1"),
            get_function_tool_call("error_tool", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    execution_task = asyncio.create_task(get_execute_result(agent, response))
    await asyncio.wait_for(started.wait(), timeout=0.2)
    await asyncio.wait_for(cancellation_started.wait(), timeout=0.2)

    with pytest.raises(UserError, match="Error running tool error_tool: boom"):
        await asyncio.wait_for(execution_task, timeout=0.2)

    assert not cancellation_finished.is_set()

    allow_cancellation_exit.set()
    await asyncio.wait_for(cancellation_finished.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_multiple_tool_calls_bound_cancelled_sibling_self_rescheduling_cleanup():
    sibling_ready = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_finished = asyncio.Event()
    stop_cleanup = asyncio.Event()

    async def _looping_cleanup_tool() -> str:
        try:
            sibling_ready.set()
            await asyncio.Future()
            return "unreachable"
        except asyncio.CancelledError:
            cleanup_started.set()
            while not stop_cleanup.is_set():
                await asyncio.sleep(0)
            cleanup_finished.set()
            raise

    async def _error_tool() -> str:
        await sibling_ready.wait()
        raise ValueError("boom")

    looping_cleanup_tool = function_tool(
        _looping_cleanup_tool,
        name_override="looping_cleanup_tool",
        failure_error_function=None,
    )
    error_tool = function_tool(
        _error_tool,
        name_override="error_tool",
        failure_error_function=None,
    )

    agent = Agent(name="test", tools=[looping_cleanup_tool, error_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("looping_cleanup_tool", "{}", call_id="1"),
            get_function_tool_call("error_tool", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    with pytest.raises(UserError, match="Error running tool error_tool: boom"):
        await asyncio.wait_for(get_execute_result(agent, response), timeout=0.2)

    assert cleanup_started.is_set()

    stop_cleanup.set()
    await asyncio.wait_for(cleanup_finished.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_multiple_tool_calls_drain_completed_fatal_failures_before_raising():
    class ToolAborted(BaseException):
        pass

    loop = asyncio.get_running_loop()
    original_handler = loop.get_exception_handler()
    unhandled_contexts: list[dict[str, Any]] = []

    def _exception_handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        unhandled_contexts.append(context)

    async def _error_tool_1() -> str:
        raise ToolAborted("boom-1")

    async def _error_tool_2() -> str:
        raise ToolAborted("boom-2")

    tool_1 = function_tool(
        _error_tool_1,
        name_override="error_tool_1",
        failure_error_function=None,
    )
    tool_2 = function_tool(
        _error_tool_2,
        name_override="error_tool_2",
        failure_error_function=None,
    )

    agent = Agent(name="test", tools=[tool_1, tool_2])
    response = ModelResponse(
        output=[
            get_function_tool_call("error_tool_1", "{}", call_id="1"),
            get_function_tool_call("error_tool_2", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    loop.set_exception_handler(_exception_handler)
    try:
        with pytest.raises(ToolAborted):
            await get_execute_result(agent, response)
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(original_handler)

    assert not any(
        context.get("message") == "Task exception was never retrieved"
        for context in unhandled_contexts
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("delay_ticks", [1, 6, 20])
async def test_multiple_tool_calls_raise_late_fatal_sibling_exception_after_cancellation(
    delay_ticks: int,
):
    class ToolAborted(BaseException):
        pass

    sibling_ready = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    async def _error_tool_1() -> str:
        await sibling_ready.wait()
        raise ValueError("boom-1")

    async def _error_tool_2() -> str:
        try:
            sibling_ready.set()
            await asyncio.Future()
            return "unreachable"
        except asyncio.CancelledError as cancel_exc:
            sibling_cancelled.set()
            for _ in range(delay_ticks):
                await asyncio.sleep(0)
            raise ToolAborted(f"boom-{delay_ticks}") from cancel_exc

    tool_1 = function_tool(
        _error_tool_1,
        name_override="error_tool_1",
        failure_error_function=None,
    )
    tool_2 = function_tool(
        _error_tool_2,
        name_override="error_tool_2",
        failure_error_function=None,
    )

    agent = Agent(name="test", tools=[tool_1, tool_2])
    response = ModelResponse(
        output=[
            get_function_tool_call("error_tool_1", "{}", call_id="1"),
            get_function_tool_call("error_tool_2", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    with pytest.raises(ToolAborted, match=f"boom-{delay_ticks}"):
        await asyncio.wait_for(get_execute_result(agent, response), timeout=0.2)

    assert sibling_cancelled.is_set()


@pytest.mark.asyncio
async def test_multiple_tool_calls_preserve_triggering_error_over_cancelled_sibling_cleanup_error():
    sibling_ready = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    async def _cleanup_tool() -> str:
        try:
            sibling_ready.set()
            await asyncio.Future()
            return "unreachable"
        except asyncio.CancelledError as cancel_exc:
            sibling_cancelled.set()
            raise ValueError("cleanup") from cancel_exc

    async def _error_tool() -> str:
        await sibling_ready.wait()
        raise ValueError("boom")

    cleanup_tool = function_tool(
        _cleanup_tool,
        name_override="cleanup_tool",
        failure_error_function=None,
    )
    error_tool = function_tool(
        _error_tool,
        name_override="error_tool",
        failure_error_function=None,
    )

    agent = Agent(name="test", tools=[cleanup_tool, error_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("cleanup_tool", "{}", call_id="1"),
            get_function_tool_call("error_tool", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    with pytest.raises(UserError, match="Error running tool error_tool: boom"):
        await asyncio.wait_for(get_execute_result(agent, response), timeout=0.2)

    assert sibling_cancelled.is_set()


@pytest.mark.asyncio
async def test_multiple_tool_calls_report_late_cleanup_exception_from_cancelled_sibling():
    loop = asyncio.get_running_loop()
    original_handler = loop.get_exception_handler()
    reported_contexts: list[dict[str, Any]] = []
    late_cleanup_reported = asyncio.Event()
    sibling_ready = asyncio.Event()
    cleanup_blocked = asyncio.Event()
    cleanup_finished = asyncio.Event()
    release_cleanup = asyncio.Event()

    def _exception_handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        reported_contexts.append(context)
        if context.get("message") == (
            "Background function tool task raised during cancellation cleanup after failure "
            "propagation."
        ) and isinstance(context.get("exception"), UserError):
            late_cleanup_reported.set()

    async def _error_tool() -> str:
        await sibling_ready.wait()
        raise ValueError("boom")

    async def _cleanup_tool() -> str:
        try:
            sibling_ready.set()
            await asyncio.Future()
            return "unreachable"
        except asyncio.CancelledError as cancel_exc:
            cleanup_blocked.set()
            try:
                await release_cleanup.wait()
            finally:
                cleanup_finished.set()
            raise RuntimeError("late-cleanup-boom") from cancel_exc

    error_tool = function_tool(
        _error_tool,
        name_override="error_tool",
        failure_error_function=None,
    )
    cleanup_tool = function_tool(
        _cleanup_tool,
        name_override="cleanup_tool",
        failure_error_function=None,
    )

    agent = Agent(name="test", tools=[cleanup_tool, error_tool])
    response = ModelResponse(
        output=[
            get_function_tool_call("cleanup_tool", "{}", call_id="1"),
            get_function_tool_call("error_tool", "{}", call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    loop.set_exception_handler(_exception_handler)
    try:
        with pytest.raises(UserError, match="Error running tool error_tool: boom"):
            await asyncio.wait_for(get_execute_result(agent, response), timeout=0.2)

        assert cleanup_blocked.is_set()
        release_cleanup.set()
        await asyncio.wait_for(cleanup_finished.wait(), timeout=0.2)
        await asyncio.wait_for(late_cleanup_reported.wait(), timeout=0.5)
    finally:
        loop.set_exception_handler(original_handler)

    matching_contexts = [
        context
        for context in reported_contexts
        if context.get("message")
        == "Background function tool task raised during cancellation cleanup after failure "
        "propagation."
    ]
    assert any(
        isinstance(context.get("exception"), UserError)
        and str(context["exception"]) == "Error running tool cleanup_tool: late-cleanup-boom"
        for context in matching_contexts
    )


@pytest.mark.asyncio
async def test_multiple_tool_calls_cancel_pending_tasks_when_parent_cancelled():
    tool_1_started = asyncio.Event()
    tool_2_started = asyncio.Event()
    cancelled_tools: list[str] = []

    async def _waiting_tool(name: str) -> str:
        try:
            if name == "tool_1":
                tool_1_started.set()
            else:
                tool_2_started.set()
            await asyncio.Future()
            return "unreachable"
        except asyncio.CancelledError:
            cancelled_tools.append(name)
            raise

    tool_1 = function_tool(
        _waiting_tool,
        name_override="tool_1",
        failure_error_function=None,
    )
    tool_2 = function_tool(
        _waiting_tool,
        name_override="tool_2",
        failure_error_function=None,
    )

    agent = Agent(name="test", tools=[tool_1, tool_2])
    response = ModelResponse(
        output=[
            get_function_tool_call("tool_1", json.dumps({"name": "tool_1"}), call_id="1"),
            get_function_tool_call("tool_2", json.dumps({"name": "tool_2"}), call_id="2"),
        ],
        usage=Usage(),
        response_id=None,
    )

    execution_task = asyncio.create_task(get_execute_result(agent, response))
    await asyncio.wait_for(tool_1_started.wait(), timeout=0.2)
    await asyncio.wait_for(tool_2_started.wait(), timeout=0.2)

    execution_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution_task

    assert sorted(cancelled_tools) == ["tool_1", "tool_2"]


@pytest.mark.asyncio
async def test_parent_cancellation_does_not_wait_for_tool_cleanup():
    tool_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_finished = asyncio.Event()
    allow_cleanup_exit = asyncio.Event()

    async def _slow_cancel_tool() -> str:
        tool_started.set()
        try:
            await asyncio.Future()
            return "unreachable"
        except asyncio.CancelledError:
            cleanup_started.set()
            await allow_cleanup_exit.wait()
            cleanup_finished.set()
            raise

    tool = function_tool(
        _slow_cancel_tool,
        name_override="slow_cancel_tool",
        failure_error_function=None,
    )

    agent = Agent(name="test", tools=[tool])
    response = ModelResponse(
        output=[get_function_tool_call("slow_cancel_tool", "{}", call_id="1")],
        usage=Usage(),
        response_id=None,
    )

    execution_task = asyncio.create_task(get_execute_result(agent, response))
    await asyncio.wait_for(tool_started.wait(), timeout=0.2)

    execution_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execution_task, timeout=0.1)

    await asyncio.wait_for(cleanup_started.wait(), timeout=0.2)
    allow_cleanup_exit.set()
    await asyncio.wait_for(cleanup_finished.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_parent_cancellation_wins_when_shield_raises_after_tool_finishes(
    monkeypatch: pytest.MonkeyPatch,
):
    async def _ok_tool() -> str:
        return "ok"

    tool = function_tool(_ok_tool, name_override="ok_tool", failure_error_function=None)
    agent = Agent(name="test", tools=[tool])
    response = ModelResponse(
        output=[get_function_tool_call("ok_tool", "{}", call_id="1")],
        usage=Usage(),
        response_id=None,
    )

    original_shield = asyncio.shield

    async def _shield_then_cancel(task: asyncio.Task[Any]) -> Any:
        result = await original_shield(task)
        raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(asyncio, "shield", _shield_then_cancel)

    with pytest.raises(asyncio.CancelledError):
        await get_execute_result(agent, response)


@pytest.mark.asyncio
async def test_parent_cancellation_does_not_report_tool_failure_as_background_error():
    loop = asyncio.get_running_loop()
    original_handler = loop.get_exception_handler()
    reported_contexts: list[dict[str, Any]] = []
    tool_started = asyncio.Event()

    def _exception_handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        reported_contexts.append(context)

    async def _failing_tool() -> str:
        tool_started.set()
        await asyncio.sleep(0)
        raise ValueError("boom")

    tool = function_tool(
        _failing_tool,
        name_override="failing_tool",
        failure_error_function=None,
    )
    agent = Agent(name="test", tools=[tool])
    response = ModelResponse(
        output=[get_function_tool_call("failing_tool", "{}", call_id="1")],
        usage=Usage(),
        response_id=None,
    )

    loop.set_exception_handler(_exception_handler)
    try:
        execution_task = asyncio.create_task(get_execute_result(agent, response))
        await asyncio.wait_for(tool_started.wait(), timeout=0.2)

        execution_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution_task

        await asyncio.sleep(0)
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(original_handler)

    assert not any(
        context.get("message")
        == "Background function tool task raised during cancellation cleanup after failure "
        "propagation."
        and isinstance(context.get("exception"), UserError)
        and str(context["exception"]) == "Error running tool failing_tool: boom"
        for context in reported_contexts
    )


@pytest.mark.asyncio
async def test_function_tool_context_includes_run_config() -> None:
    async def _tool_with_run_config(context: ToolContext[str]) -> str:
        assert context.run_config is not None
        return str(context.run_config.model)

    tool = function_tool(
        _tool_with_run_config,
        name_override="tool_with_run_config",
        failure_error_function=None,
    )
    agent = Agent(name="test", tools=[tool])
    response = ModelResponse(
        output=[get_function_tool_call("tool_with_run_config", "{}", call_id="call-1")],
        usage=Usage(),
        response_id=None,
    )
    run_config = RunConfig(model="gpt-4.1-mini")

    result = await get_execute_result(agent, response, run_config=run_config)

    assert len(result.generated_items) == 2
    assert_item_is_function_tool_call_output(result.generated_items[1], "gpt-4.1-mini")
    assert isinstance(result.next_step, NextStepRunAgain)


@pytest.mark.asyncio
async def test_deferred_function_tool_context_preserves_search_loaded_namespace() -> None:
    async def _tool_with_namespace(context: ToolContext[str]) -> str:
        tool_call_namespace = getattr(context.tool_call, "namespace", None)
        return json.dumps(
            {
                "tool_call_namespace": tool_call_namespace,
                "tool_namespace": context.tool_namespace,
            },
            sort_keys=True,
        )

    tool = function_tool(
        _tool_with_namespace,
        name_override="get_weather",
        defer_loading=True,
        failure_error_function=None,
    )
    agent = Agent(name="test", tools=[tool])
    response = ModelResponse(
        output=[
            get_function_tool_call(
                "get_weather",
                "{}",
                call_id="call-1",
                namespace="get_weather",
            )
        ],
        usage=Usage(),
        response_id=None,
    )

    result = await get_execute_result(agent, response)

    assert len(result.generated_items) == 2
    assert_item_is_function_tool_call_output(
        result.generated_items[1],
        '{"tool_call_namespace": "get_weather", "tool_namespace": "get_weather"}',
    )
    assert isinstance(result.next_step, NextStepRunAgain)


@pytest.mark.asyncio
async def test_handoff_output_leads_to_handoff_next_step():
    agent_1 = Agent(name="test_1")
    agent_2 = Agent(name="test_2")
    agent_3 = Agent(name="test_3", handoffs=[agent_1, agent_2])
    response = ModelResponse(
        output=[get_text_message("Hello, world!"), get_handoff_tool_call(agent_1)],
        usage=Usage(),
        response_id=None,
    )
    result = await get_execute_result(agent_3, response)

    assert isinstance(result.next_step, NextStepHandoff)
    assert result.next_step.new_agent == agent_1

    assert len(result.generated_items) == 3


class Foo(BaseModel):
    bar: str


@pytest.mark.asyncio
async def test_final_output_without_tool_runs_again():
    agent = Agent(name="test", output_type=Foo, tools=[get_function_tool("tool_1", "result")])
    response = ModelResponse(
        output=[get_function_tool_call("tool_1")],
        usage=Usage(),
        response_id=None,
    )
    result = await get_execute_result(agent, response)

    assert isinstance(result.next_step, NextStepRunAgain)
    assert len(result.generated_items) == 2, "expected 2 items: tool call, tool call output"


@pytest.mark.asyncio
async def test_final_output_leads_to_final_output_next_step():
    agent = Agent(name="test", output_type=Foo)
    response = ModelResponse(
        output=[
            get_text_message("Hello, world!"),
            get_final_output_message(Foo(bar="123").model_dump_json()),
        ],
        usage=Usage(),
        response_id=None,
    )
    result = await get_execute_result(agent, response)

    assert isinstance(result.next_step, NextStepFinalOutput)
    assert result.next_step.output == Foo(bar="123")


@pytest.mark.asyncio
async def test_handoff_and_final_output_leads_to_handoff_next_step():
    agent_1 = Agent(name="test_1")
    agent_2 = Agent(name="test_2")
    agent_3 = Agent(name="test_3", handoffs=[agent_1, agent_2], output_type=Foo)
    response = ModelResponse(
        output=[
            get_final_output_message(Foo(bar="123").model_dump_json()),
            get_handoff_tool_call(agent_1),
        ],
        usage=Usage(),
        response_id=None,
    )
    result = await get_execute_result(agent_3, response)

    assert isinstance(result.next_step, NextStepHandoff)
    assert result.next_step.new_agent == agent_1


@pytest.mark.asyncio
async def test_multiple_final_output_leads_to_final_output_next_step():
    agent_1 = Agent(name="test_1")
    agent_2 = Agent(name="test_2")
    agent_3 = Agent(name="test_3", handoffs=[agent_1, agent_2], output_type=Foo)
    response = ModelResponse(
        output=[
            get_final_output_message(Foo(bar="123").model_dump_json()),
            get_final_output_message(Foo(bar="456").model_dump_json()),
        ],
        usage=Usage(),
        response_id=None,
    )
    result = await get_execute_result(agent_3, response)

    assert isinstance(result.next_step, NextStepFinalOutput)
    assert result.next_step.output == Foo(bar="456")


@pytest.mark.asyncio
async def test_input_guardrail_runs_on_invalid_json():
    guardrail_calls: list[str] = []

    def guardrail(data) -> ToolGuardrailFunctionOutput:
        guardrail_calls.append(data.context.tool_arguments)
        return ToolGuardrailFunctionOutput.allow(output_info="checked")

    guardrail_obj: ToolInputGuardrail[Any] = ToolInputGuardrail(guardrail_function=guardrail)

    def _echo(value: str) -> str:
        return value

    tool = function_tool(
        _echo,
        name_override="guarded",
        tool_input_guardrails=[guardrail_obj],
    )
    agent = Agent(name="test", tools=[tool])
    response = ModelResponse(
        output=[get_function_tool_call("guarded", "bad_json")],
        usage=Usage(),
        response_id=None,
    )

    result = await get_execute_result(agent, response)

    assert guardrail_calls == ["bad_json"]
    assert result.tool_input_guardrail_results
    assert result.tool_input_guardrail_results[0].output.output_info == "checked"

    output_item = next(
        item for item in result.generated_items if isinstance(item, ToolCallOutputItem)
    )
    assert "An error occurred while parsing tool arguments" in str(output_item.output)


@pytest.mark.asyncio
async def test_invalid_json_raises_with_failure_error_function_none():
    def _echo(value: str) -> str:
        return value

    tool = function_tool(
        _echo,
        name_override="guarded",
        failure_error_function=None,
    )
    agent = Agent(name="test", tools=[tool])
    response = ModelResponse(
        output=[get_function_tool_call("guarded", "bad_json")],
        usage=Usage(),
        response_id=None,
    )

    with pytest.raises(ModelBehaviorError, match="Invalid JSON input for tool"):
        await get_execute_result(agent, response)


# === Helpers ===


def assert_item_is_message(item: RunItem, text: str) -> None:
    assert isinstance(item, MessageOutputItem)
    assert item.raw_item.type == "message"
    assert item.raw_item.role == "assistant"
    assert item.raw_item.content[0].type == "output_text"
    assert item.raw_item.content[0].text == text


def assert_item_is_function_tool_call(
    item: RunItem, name: str, arguments: str | None = None
) -> None:
    assert isinstance(item, ToolCallItem)
    raw_item = getattr(item, "raw_item", None)
    assert getattr(raw_item, "type", None) == "function_call"
    assert getattr(raw_item, "name", None) == name
    if arguments:
        assert getattr(raw_item, "arguments", None) == arguments


def assert_item_is_function_tool_call_output(item: RunItem, output: str) -> None:
    assert isinstance(item, ToolCallOutputItem)
    raw_item = cast(dict[str, Any], item.raw_item)
    assert raw_item["type"] == "function_call_output"
    assert raw_item["output"] == output


def make_processed_response(
    *,
    new_items: list[RunItem] | None = None,
    handoffs: list[ToolRunHandoff] | None = None,
    functions: list[ToolRunFunction] | None = None,
    computer_actions: list[ToolRunComputerAction] | None = None,
    local_shell_calls: list[ToolRunLocalShellCall] | None = None,
    shell_calls: list[ToolRunShellCall] | None = None,
    apply_patch_calls: list[ToolRunApplyPatchCall] | None = None,
    mcp_approval_requests: list[ToolRunMCPApprovalRequest] | None = None,
    tools_used: list[str] | None = None,
    interruptions: list[ToolApprovalItem] | None = None,
) -> ProcessedResponse:
    """Build a ProcessedResponse with empty collections by default."""

    return ProcessedResponse(
        new_items=new_items or [],
        handoffs=handoffs or [],
        functions=functions or [],
        computer_actions=computer_actions or [],
        local_shell_calls=local_shell_calls or [],
        shell_calls=shell_calls or [],
        apply_patch_calls=apply_patch_calls or [],
        mcp_approval_requests=mcp_approval_requests or [],
        tools_used=tools_used or [],
        interruptions=interruptions or [],
    )


async def get_execute_result(
    agent: Agent[Any],
    response: ModelResponse,
    *,
    original_input: str | list[TResponseInputItem] | None = None,
    generated_items: list[RunItem] | None = None,
    hooks: RunHooks[Any] | None = None,
    context_wrapper: RunContextWrapper[Any] | None = None,
    run_config: RunConfig | None = None,
) -> SingleStepResult:
    output_schema = get_output_schema(agent)
    handoffs = await get_handoffs(agent, context_wrapper or RunContextWrapper(None))

    processed_response = run_loop.process_model_response(
        agent=agent,
        all_tools=await agent.get_all_tools(context_wrapper or RunContextWrapper(None)),
        response=response,
        output_schema=output_schema,
        handoffs=handoffs,
    )
    return await run_loop.execute_tools_and_side_effects(
        bindings=_bind_agent(agent),
        original_input=original_input or "hello",
        new_response=response,
        pre_step_items=generated_items or [],
        processed_response=processed_response,
        output_schema=output_schema,
        hooks=hooks or RunHooks(),
        context_wrapper=context_wrapper or RunContextWrapper(None),
        run_config=run_config or RunConfig(),
    )


async def run_execute_with_processed_response(
    agent: Agent[Any], processed_response: ProcessedResponse
) -> SingleStepResult:
    """Execute tools for a pre-constructed ProcessedResponse."""

    return await run_loop.execute_tools_and_side_effects(
        bindings=_bind_agent(agent),
        original_input="test",
        pre_step_items=[],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        output_schema=None,
        hooks=RunHooks(),
        context_wrapper=make_context_wrapper(),
        run_config=RunConfig(),
    )


@dataclass
class ToolApprovalRun:
    agent: Agent[Any]
    processed_response: ProcessedResponse
    expected_tool_name: str


def _function_tool_approval_run() -> ToolApprovalRun:
    async def _test_tool() -> str:
        return "tool_result"

    tool = function_tool(_test_tool, name_override="test_tool", needs_approval=True)
    agent = make_agent(tools=[tool])
    tool_call = make_function_tool_call("test_tool", arguments="{}")
    tool_run = ToolRunFunction(function_tool=tool, tool_call=tool_call)
    processed_response = make_processed_response(functions=[tool_run])
    return ToolApprovalRun(
        agent=agent,
        processed_response=processed_response,
        expected_tool_name="test_tool",
    )


def _shell_tool_approval_run() -> ToolApprovalRun:
    shell_tool = ShellTool(executor=lambda request: "output", needs_approval=True)
    agent = make_agent(tools=[shell_tool])
    tool_call = make_shell_call(
        "call_shell", id_value="shell_call", commands=["echo hi"], status="completed"
    )
    tool_run = ToolRunShellCall(tool_call=tool_call, shell_tool=shell_tool)
    processed_response = make_processed_response(shell_calls=[tool_run])
    return ToolApprovalRun(
        agent=agent,
        processed_response=processed_response,
        expected_tool_name="shell",
    )


def _apply_patch_tool_approval_run() -> ToolApprovalRun:
    editor = RecordingEditor()
    apply_patch_tool = ApplyPatchTool(editor=editor, needs_approval=True)
    agent = make_agent(tools=[apply_patch_tool])
    tool_call = make_apply_patch_dict("call_apply")
    tool_run = ToolRunApplyPatchCall(tool_call=tool_call, apply_patch_tool=apply_patch_tool)
    processed_response = make_processed_response(apply_patch_calls=[tool_run])
    return ToolApprovalRun(
        agent=agent,
        processed_response=processed_response,
        expected_tool_name="apply_patch",
    )


@pytest.mark.parametrize(
    "setup_fn",
    [
        _function_tool_approval_run,
        _shell_tool_approval_run,
        _apply_patch_tool_approval_run,
    ],
    ids=["function_tool", "shell_tool", "apply_patch_tool"],
)
@pytest.mark.asyncio
async def test_execute_tools_handles_tool_approval_items(
    setup_fn: Callable[[], ToolApprovalRun],
) -> None:
    """Tool approvals should surface as interruptions across tool types."""
    scenario = setup_fn()
    result = await run_execute_with_processed_response(scenario.agent, scenario.processed_response)

    assert_single_approval_interruption(result, tool_name=scenario.expected_tool_name)


@pytest.mark.asyncio
async def test_execute_tools_preserves_synthetic_namespace_for_deferred_top_level_approval() -> (
    None
):
    async def _deferred_weather() -> str:
        return "tool_result"

    tool = function_tool(
        _deferred_weather,
        name_override="get_weather",
        defer_loading=True,
        needs_approval=True,
    )
    agent = make_agent(tools=[tool])
    tool_call = cast(
        ResponseFunctionToolCall,
        get_function_tool_call("get_weather", "{}", namespace="get_weather"),
    )
    tool_run = ToolRunFunction(function_tool=tool, tool_call=tool_call)
    processed_response = make_processed_response(functions=[tool_run])

    result = await run_execute_with_processed_response(agent, processed_response)
    interruption = assert_single_approval_interruption(result, tool_name="get_weather")

    assert interruption.tool_namespace == "get_weather"
    assert getattr(interruption.raw_item, "namespace", None) == "get_weather"


@pytest.mark.asyncio
async def test_deferred_tool_approval_allows_bare_alias_when_visible_peer_is_disabled() -> None:
    async def _visible_weather() -> str:
        return "visible"

    async def _deferred_weather() -> str:
        return "deferred"

    visible_tool = function_tool(
        _visible_weather,
        name_override="get_weather",
        needs_approval=True,
        is_enabled=False,
    )
    deferred_tool = function_tool(
        _deferred_weather,
        name_override="get_weather",
        defer_loading=True,
        needs_approval=True,
    )
    agent = make_agent(tools=[visible_tool, deferred_tool])
    tool_call = cast(
        ResponseFunctionToolCall,
        get_function_tool_call("get_weather", "{}", namespace="get_weather"),
    )
    tool_run = ToolRunFunction(function_tool=deferred_tool, tool_call=tool_call)
    processed_response = make_processed_response(functions=[tool_run])

    result = await run_execute_with_processed_response(agent, processed_response)
    interruption = assert_single_approval_interruption(result, tool_name="get_weather")

    assert interruption.tool_namespace == "get_weather"
    assert interruption._allow_bare_name_alias is True


@pytest.mark.asyncio
async def test_execute_tools_runs_hosted_mcp_callback_when_present():
    """Hosted MCP approvals should invoke on_approval_request callbacks."""

    mcp_tool = HostedMCPTool(
        tool_config={
            "type": "mcp",
            "server_label": "test_mcp_server",
            "server_url": "https://example.com",
            "require_approval": "always",
        },
        on_approval_request=lambda request: {"approve": True},
    )
    agent = make_agent(tools=[mcp_tool])
    request_item = McpApprovalRequest(
        id="mcp-approval-1",
        type="mcp_approval_request",
        server_label="test_mcp_server",
        arguments="{}",
        name="list_repo_languages",
    )
    processed_response = make_processed_response(
        new_items=[MCPApprovalRequestItem(raw_item=request_item, agent=agent)],
        mcp_approval_requests=[
            ToolRunMCPApprovalRequest(
                request_item=request_item,
                mcp_tool=mcp_tool,
            )
        ],
    )

    result = await run_execute_with_processed_response(agent, processed_response)

    assert not isinstance(result.next_step, NextStepInterruption)
    assert any(isinstance(item, MCPApprovalResponseItem) for item in result.new_step_items)
    assert not result.processed_response or not result.processed_response.interruptions


@pytest.mark.asyncio
async def test_execute_tools_uses_public_agent_for_hosted_mcp_callback_results():
    """Hosted MCP callback responses should expose the public agent when execution uses a clone."""

    mcp_tool = HostedMCPTool(
        tool_config={
            "type": "mcp",
            "server_label": "test_mcp_server",
            "server_url": "https://example.com",
            "require_approval": "always",
        },
        on_approval_request=lambda request: {"approve": True},
    )
    public_agent = make_agent(tools=[mcp_tool])
    execution_agent = public_agent.clone()
    set_public_agent(execution_agent, public_agent)
    request_item = McpApprovalRequest(
        id="mcp-approval-callback-public-agent",
        type="mcp_approval_request",
        server_label="test_mcp_server",
        arguments="{}",
        name="list_repo_languages",
    )
    processed_response = make_processed_response(
        new_items=[MCPApprovalRequestItem(raw_item=request_item, agent=execution_agent)],
        mcp_approval_requests=[
            ToolRunMCPApprovalRequest(
                request_item=request_item,
                mcp_tool=mcp_tool,
            )
        ],
    )

    result = await run_loop.execute_tools_and_side_effects(
        bindings=_bind_agent(execution_agent),
        original_input="test",
        pre_step_items=[],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        output_schema=None,
        hooks=RunHooks(),
        context_wrapper=make_context_wrapper(),
        run_config=RunConfig(),
    )

    assert not isinstance(result.next_step, NextStepInterruption)
    assert any(
        isinstance(item, MCPApprovalResponseItem) and item.agent is public_agent
        for item in result.new_step_items
    )


@pytest.mark.asyncio
async def test_execute_tools_surfaces_hosted_mcp_interruptions_without_callback():
    """Hosted MCP approvals should surface as interruptions when no callback is provided."""

    mcp_tool = HostedMCPTool(
        tool_config={
            "type": "mcp",
            "server_label": "test_mcp_server",
            "server_url": "https://example.com",
            "require_approval": "always",
        },
        on_approval_request=None,
    )
    agent = make_agent(tools=[mcp_tool])
    request_item = McpApprovalRequest(
        id="mcp-approval-2",
        type="mcp_approval_request",
        server_label="test_mcp_server",
        arguments="{}",
        name="list_repo_languages",
    )
    processed_response = make_processed_response(
        new_items=[MCPApprovalRequestItem(raw_item=request_item, agent=agent)],
        mcp_approval_requests=[
            ToolRunMCPApprovalRequest(
                request_item=request_item,
                mcp_tool=mcp_tool,
            )
        ],
    )

    result = await run_execute_with_processed_response(agent, processed_response)

    assert isinstance(result.next_step, NextStepInterruption)
    assert result.next_step.interruptions
    assert any(isinstance(item, ToolApprovalItem) for item in result.next_step.interruptions)
    assert any(
        isinstance(item, ToolApprovalItem)
        and getattr(item.raw_item, "id", None) == "mcp-approval-2"
        for item in result.new_step_items
    )


@pytest.mark.asyncio
async def test_execute_tools_uses_public_agent_for_hosted_mcp_interruptions():
    """Hosted MCP approval items should expose the public agent when execution uses a clone."""

    mcp_tool = HostedMCPTool(
        tool_config={
            "type": "mcp",
            "server_label": "test_mcp_server",
            "server_url": "https://example.com",
            "require_approval": "always",
        },
        on_approval_request=None,
    )
    public_agent = make_agent(tools=[mcp_tool])
    execution_agent = public_agent.clone()
    set_public_agent(execution_agent, public_agent)
    request_item = McpApprovalRequest(
        id="mcp-approval-public-agent",
        type="mcp_approval_request",
        server_label="test_mcp_server",
        arguments="{}",
        name="list_repo_languages",
    )
    processed_response = make_processed_response(
        new_items=[MCPApprovalRequestItem(raw_item=request_item, agent=execution_agent)],
        mcp_approval_requests=[
            ToolRunMCPApprovalRequest(
                request_item=request_item,
                mcp_tool=mcp_tool,
            )
        ],
    )

    result = await run_loop.execute_tools_and_side_effects(
        bindings=_bind_agent(execution_agent),
        original_input="test",
        pre_step_items=[],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        output_schema=None,
        hooks=RunHooks(),
        context_wrapper=make_context_wrapper(),
        run_config=RunConfig(),
    )

    assert isinstance(result.next_step, NextStepInterruption)
    assert result.next_step.interruptions
    assert all(item.agent is public_agent for item in result.next_step.interruptions)
    assert any(
        isinstance(item, ToolApprovalItem)
        and getattr(item.raw_item, "id", None) == "mcp-approval-public-agent"
        and item.agent is public_agent
        for item in result.new_step_items
    )


@pytest.mark.asyncio
async def test_resolve_interrupted_turn_uses_public_agent_for_resumed_hosted_mcp_approvals():
    """Resumed hosted MCP approvals should keep the public agent on approval responses."""

    mcp_tool = HostedMCPTool(
        tool_config={
            "type": "mcp",
            "server_label": "test_mcp_server",
            "server_url": "https://example.com",
            "require_approval": "always",
        },
        on_approval_request=None,
    )
    public_agent = make_agent(tools=[mcp_tool])
    execution_agent = public_agent.clone()
    set_public_agent(execution_agent, public_agent)
    request_item = McpApprovalRequest(
        id="mcp-approval-resume-public-agent",
        type="mcp_approval_request",
        server_label="test_mcp_server",
        arguments="{}",
        name="list_repo_languages",
    )
    approval_item = ToolApprovalItem(
        agent=public_agent,
        raw_item=request_item,
        tool_name="list_repo_languages",
    )
    context_wrapper = make_context_wrapper()
    context_wrapper.approve_tool(approval_item)
    processed_response = make_processed_response(
        new_items=[MCPApprovalRequestItem(raw_item=request_item, agent=execution_agent)],
        mcp_approval_requests=[
            ToolRunMCPApprovalRequest(
                request_item=request_item,
                mcp_tool=mcp_tool,
            )
        ],
    )

    result = await turn_resolution.resolve_interrupted_turn(
        bindings=_bind_agent(execution_agent),
        original_input="test",
        original_pre_step_items=[approval_item],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
    )

    responses = [
        item
        for item in result.new_step_items
        if isinstance(item, MCPApprovalResponseItem)
        and item.raw_item.get("approval_request_id") == "mcp-approval-resume-public-agent"
    ]
    assert responses
    assert all(item.agent is public_agent for item in responses)


@pytest.mark.asyncio
async def test_execute_handoffs_uses_public_agent_for_ignored_extra_handoffs():
    """Ignored extra handoff outputs should stay owned by the public agent."""

    first_target = Agent(name="alpha")
    second_target = Agent(name="beta")
    public_agent = Agent(name="triage", handoffs=[first_target, second_target])
    execution_agent = public_agent.clone()
    set_public_agent(execution_agent, public_agent)
    response = ModelResponse(
        output=[get_handoff_tool_call(first_target), get_handoff_tool_call(second_target)],
        usage=Usage(),
        response_id="resp",
    )

    result = await get_execute_result(execution_agent, response)

    ignored_outputs = [
        item
        for item in result.new_step_items
        if isinstance(item, ToolCallOutputItem)
        and item.output == "Multiple handoffs detected, ignoring this one."
    ]
    assert len(ignored_outputs) == 1
    assert ignored_outputs[0].agent is public_agent


@pytest.mark.asyncio
async def test_execute_tools_emits_hosted_mcp_rejection_response():
    """Hosted MCP rejections without callbacks should emit approval responses."""

    mcp_tool = HostedMCPTool(
        tool_config={
            "type": "mcp",
            "server_label": "test_mcp_server",
            "server_url": "https://example.com",
            "require_approval": "always",
        },
        on_approval_request=None,
    )
    agent = make_agent(tools=[mcp_tool])
    request_item = McpApprovalRequest(
        id="mcp-approval-reject",
        type="mcp_approval_request",
        server_label="test_mcp_server",
        arguments="{}",
        name="list_repo_languages",
    )
    processed_response = make_processed_response(
        new_items=[MCPApprovalRequestItem(raw_item=request_item, agent=agent)],
        mcp_approval_requests=[
            ToolRunMCPApprovalRequest(
                request_item=request_item,
                mcp_tool=mcp_tool,
            )
        ],
    )
    context_wrapper = make_context_wrapper()
    reject_tool_call(context_wrapper, agent, request_item, tool_name="list_repo_languages")

    result = await run_loop.execute_tools_and_side_effects(
        bindings=_bind_agent(agent),
        original_input="test",
        pre_step_items=[],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        output_schema=None,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
    )

    responses = [
        item for item in result.new_step_items if isinstance(item, MCPApprovalResponseItem)
    ]
    assert responses, "Rejection should emit an MCP approval response."
    assert responses[0].raw_item["approve"] is False
    assert responses[0].raw_item["approval_request_id"] == "mcp-approval-reject"
    assert "reason" not in responses[0].raw_item
    assert not isinstance(result.next_step, NextStepInterruption)


@pytest.mark.asyncio
async def test_execute_tools_emits_hosted_mcp_rejection_reason_from_explicit_message():
    """Hosted MCP rejections should forward explicit rejection messages as reasons."""

    mcp_tool = HostedMCPTool(
        tool_config={
            "type": "mcp",
            "server_label": "test_mcp_server",
            "server_url": "https://example.com",
            "require_approval": "always",
        },
        on_approval_request=None,
    )
    agent = make_agent(tools=[mcp_tool])
    request_item = McpApprovalRequest(
        id="mcp-approval-reject-reason",
        type="mcp_approval_request",
        server_label="test_mcp_server",
        arguments="{}",
        name="list_repo_languages",
    )
    processed_response = make_processed_response(
        new_items=[MCPApprovalRequestItem(raw_item=request_item, agent=agent)],
        mcp_approval_requests=[
            ToolRunMCPApprovalRequest(
                request_item=request_item,
                mcp_tool=mcp_tool,
            )
        ],
    )
    context_wrapper = make_context_wrapper()
    reject_tool_call(
        context_wrapper,
        agent,
        request_item,
        tool_name="list_repo_languages",
        rejection_message="Denied by policy",
    )

    result = await run_loop.execute_tools_and_side_effects(
        bindings=_bind_agent(agent),
        original_input="test",
        pre_step_items=[],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        output_schema=None,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
    )

    responses = [
        item for item in result.new_step_items if isinstance(item, MCPApprovalResponseItem)
    ]
    assert responses, "Rejection should emit an MCP approval response."
    assert responses[0].raw_item["approve"] is False
    assert responses[0].raw_item["approval_request_id"] == "mcp-approval-reject-reason"
    assert responses[0].raw_item["reason"] == "Denied by policy"
