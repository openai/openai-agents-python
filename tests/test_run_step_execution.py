from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, cast

import pytest
from openai.types.responses.response_output_item import McpApprovalRequest
from openai.types.responses.response_output_message import ResponseOutputMessage
from openai.types.responses.response_output_refusal import ResponseOutputRefusal
from pydantic import BaseModel

from agents import (
    Agent,
    ApplyPatchTool,
    HostedMCPTool,
    MCPApprovalRequestItem,
    MCPApprovalResponseItem,
    MessageOutputItem,
    ModelBehaviorError,
    ModelResponse,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    RunItem,
    ShellTool,
    ToolApprovalItem,
    ToolCallItem,
    ToolCallOutputItem,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrail,
    TResponseInputItem,
    Usage,
)
from agents.run_internal import run_loop
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
async def test_plaintext_agent_hosted_shell_with_refusal_message_is_final_output():
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

    result = await get_execute_result(agent, response)

    assert len(result.generated_items) == 3
    assert isinstance(result.generated_items[0], ToolCallItem)
    assert isinstance(result.generated_items[1], ToolCallOutputItem)
    assert isinstance(result.generated_items[2], MessageOutputItem)
    assert isinstance(result.next_step, NextStepFinalOutput)
    assert result.next_step.output == ""


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
        agent=agent,
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
        agent=agent,
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
        agent=agent,
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
    assert not isinstance(result.next_step, NextStepInterruption)
