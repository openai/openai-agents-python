"""Completed work survives supported failures outside ordinary tool exceptions."""

from __future__ import annotations

import asyncio

import pytest
from openai.types.responses import (
    ResponseCustomToolCall,
    ResponseFunctionShellToolCall,
    ResponseFunctionShellToolCallOutput,
    ResponseFunctionWebSearch,
)
from openai.types.responses.response_output_item import McpApprovalRequest

from agents import (
    Agent,
    CustomTool,
    HostedMCPTool,
    RunHooks,
    Runner,
    RunState,
    ShellTool,
    SQLiteSession,
    UserError,
    handoff,
)
from agents.decorators import tool
from agents.testing import ScriptedModel, assistant_message, function_call

from .model_test_helpers import get_exact_output_stream_step
from .test_tool_batch_failure_history import _shape


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    "boundary",
    [
        "provider",
        "provider_shell",
        "handoff",
        "mcp_callback",
        "tool_cancel",
        "native_cancel",
        "parent_cancel",
    ],
)
async def test_completed_history_at_failure_boundaries(streaming: bool, boundary: str):
    completed = asyncio.Event()
    blocked = asyncio.Event()
    effects: list[str] = []

    @tool
    async def create_ticket() -> str:
        effects.append("ticket")
        return "ticket T-1"

    @tool(failure_error_function=None)
    async def fail() -> str:
        if boundary not in ("provider", "provider_shell"):
            await completed.wait()
        if boundary == "parent_cancel":
            blocked.set()
            await asyncio.Future()
        if boundary == "tool_cancel":
            raise asyncio.CancelledError("tool-local cancellation")
        raise ValueError("synthetic failure")

    async def cancel_native(context, value):
        await completed.wait()
        raise asyncio.CancelledError("native tool-local cancellation")

    native = CustomTool(name="native", description="synthetic", on_invoke_tool=cancel_native)

    class Hooks(RunHooks):
        async def on_tool_end(self, context, agent, tool, result):
            if tool.name == "create_ticket":
                completed.set()

    async def fail_handoff(context):
        assert effects == ["ticket"]
        raise UserError("handoff failure")

    target = Agent(name="target", model=ScriptedModel([[assistant_message("done")]]))
    transfer = handoff(target, on_handoff=fail_handoff)
    calls = (
        [
            ResponseFunctionWebSearch(
                id="ws_done",
                type="web_search_call",
                status="completed",
                action={"type": "search", "query": "synthetic query"},
            ),
            function_call("fail", {}, call_id="fail"),
        ]
        if boundary == "provider"
        else [
            function_call("create_ticket", {}, call_id="ticket"),
            function_call(
                transfer.tool_name if boundary == "handoff" else "fail", {}, call_id="fail"
            ),
        ]
    )
    if boundary == "native_cancel":
        calls[-1] = ResponseCustomToolCall(
            type="custom_tool_call", name="native", call_id="fail", input="synthetic"
        )

    async def fail_approval(request):
        assert effects == ["ticket"]
        raise UserError("approval callback failure")

    hosted_mcp = HostedMCPTool(
        tool_config={
            "type": "mcp",
            "server_label": "synthetic",
            "server_url": "https://example.com",
            "require_approval": "always",
        },
        on_approval_request=fail_approval,
    )
    hosted_shell = ShellTool(
        environment={"type": "container_reference", "container_id": "cntr_synthetic"}
    )
    if boundary == "provider_shell":
        calls = [
            ResponseFunctionShellToolCall(
                id="sh_done",
                type="shell_call",
                call_id="shell",
                status="completed",
                action={"commands": ["echo synthetic"]},
            ),
            ResponseFunctionShellToolCallOutput(
                id="sh_output",
                type="shell_call_output",
                call_id="shell",
                status="completed",
                output=[
                    {
                        "stdout": "synthetic",
                        "stderr": "",
                        "outcome": {"type": "exit", "exit_code": 0},
                    }
                ],
            ),
            function_call("fail", {}, call_id="fail"),
        ]
    elif boundary == "mcp_callback":
        calls[-1] = McpApprovalRequest(
            id="approval",
            type="mcp_approval_request",
            server_label="synthetic",
            arguments="{}",
            name="synthetic",
        )
    model = ScriptedModel(
        [get_exact_output_stream_step(calls) if streaming else calls, [assistant_message("done")]]
    )
    agent = Agent(
        name="support",
        model=model,
        tools=[create_ticket, fail, native, hosted_mcp, hosted_shell],
        handoffs=[transfer],
    )
    session = SQLiteSession("failure-boundaries")
    result = None
    caught = None
    output_events = []
    try:
        if streaming:
            result = Runner.run_streamed(agent, "go", session=session, hooks=Hooks())

            async def consume():
                async for event in result.stream_events():
                    if (
                        event.type == "run_item_stream_event"
                        and event.item.type == "tool_call_output_item"
                    ):
                        output_events.append(event.item.to_input_item())

            consumer = asyncio.create_task(consume())
            if boundary == "parent_cancel":
                await asyncio.wait_for(blocked.wait(), 2)
                result.cancel()
            if boundary in ("tool_cancel", "native_cancel", "parent_cancel"):
                await asyncio.wait_for(consumer, 2)
            else:
                with pytest.raises(UserError) as exc:
                    await asyncio.wait_for(consumer, 2)
                caught = exc.value
        else:
            task = asyncio.create_task(Runner.run(agent, "go", session=session, hooks=Hooks()))
            if boundary == "parent_cancel":
                await asyncio.wait_for(blocked.wait(), 2)
                task.cancel()
            error_type = (
                asyncio.CancelledError
                if boundary in ("tool_cancel", "native_cancel", "parent_cancel")
                else UserError
            )
            with pytest.raises(error_type) as exc:
                await asyncio.wait_for(task, 2)
            caught = exc.value
        expected = (
            []
            if boundary == "parent_cancel"
            else ["web_search_call"]
            if boundary == "provider"
            else ["shell_call:shell", "shell_call_output:shell"]
            if boundary == "provider_shell"
            else ["function_call:ticket", "function_call_output:ticket"]
        )
        assert _shape(await session.get_items())[1:] == expected
        if isinstance(caught, UserError):
            assert caught.run_data is not None
            assert _shape([item.to_input_item() for item in caught.run_data.new_items]) == expected
        if result is not None:
            assert _shape(output_events) == [item for item in expected if "output:" in item]
            assert _shape(result.to_input_list())[1:] == expected
            if boundary != "parent_cancel":
                restored = await RunState.from_json(agent, result.to_state().to_json())
                assert (
                    _shape([item.to_input_item() for item in restored._generated_items]) == expected
                )
        if boundary != "parent_cancel":
            await Runner.run(agent, "continue", session=session)
            assert _shape(model.calls[-1].input)[1:-1] == expected
        assert effects == ([] if boundary in ("provider", "provider_shell") else ["ticket"])
    finally:
        session.close()
