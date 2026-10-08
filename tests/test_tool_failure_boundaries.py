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
        "provider_final_hook",
        "provider_shell",
        "handoff",
        "mcp_callback",
        "tool_behavior",
        "final_hook",
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

        async def on_agent_end(self, context, agent, output):
            if boundary == "provider_final_hook" or (
                boundary == "final_hook" and effects == ["ticket"]
            ):
                raise UserError("final hook failure")

    async def fail_tool_behavior(context, results):
        if results:
            raise UserError("tool behavior failure")
        from agents import ToolsToFinalOutputResult

        return ToolsToFinalOutputResult(is_final_output=False, final_output=None)

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
        if boundary in ("provider", "provider_final_hook")
        else [
            function_call("create_ticket", {}, call_id="ticket"),
            function_call(
                transfer.tool_name if boundary == "handoff" else "fail", {}, call_id="fail"
            ),
        ]
    )
    if boundary == "provider_final_hook":
        calls[-1] = assistant_message("done")
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
    if boundary in ("tool_behavior", "final_hook"):
        calls = calls[:1]
    model = ScriptedModel(
        [get_exact_output_stream_step(calls) if streaming else calls, [assistant_message("done")]]
    )
    agent = Agent(
        name="support",
        model=model,
        tools=[create_ticket, fail, native, hosted_mcp, hosted_shell],
        handoffs=[transfer],
    )
    if boundary == "tool_behavior":
        agent.tool_use_behavior = fail_tool_behavior
    elif boundary == "final_hook":
        agent.tool_use_behavior = "stop_on_first_tool"
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
            if boundary in ("provider", "provider_final_hook")
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
        assert effects == (
            [] if boundary in ("provider", "provider_final_hook", "provider_shell") else ["ticket"]
        )
    finally:
        session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("scenario", ["completed", "anonymous", "incomplete"])
async def test_completed_provider_tool_search_survives_local_failure(streaming, scenario):
    from openai.types.responses import ResponseToolSearchCall, ResponseToolSearchOutputItem

    @tool(failure_error_function=None)
    async def fail() -> str:
        raise ValueError("synthetic sibling failure")

    call_id = None if scenario == "anonymous" else "search"
    search_call = ResponseToolSearchCall(
        id="search_call",
        call_id=call_id,
        type="tool_search_call",
        execution="server",
        status="completed",
        arguments={"query": "synthetic lookup"},
    )
    search_output = ResponseToolSearchOutputItem(
        id="search_output",
        call_id=call_id,
        type="tool_search_output",
        execution="server",
        status="incomplete" if scenario == "incomplete" else "completed",
        tools=[
            {
                "type": "function",
                "name": "lookup",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
        created_by="synthetic_provider",
    )
    calls = [search_call, search_output, function_call("fail", {}, call_id="fail")]
    model = ScriptedModel(
        [get_exact_output_stream_step(calls) if streaming else calls, [assistant_message("done")]]
    )
    agent = Agent(name="search", model=model, tools=[fail])
    session = SQLiteSession("search-failure")
    result = None
    events = []
    expected = (
        []
        if scenario == "incomplete"
        else [
            search_call.model_dump(exclude_unset=True),
            {
                key: value
                for key, value in search_output.model_dump(exclude_unset=True).items()
                if key != "created_by"
            },
        ]
    )
    try:
        with pytest.raises(UserError, match="synthetic sibling failure") as caught:
            if streaming:
                result = Runner.run_streamed(agent, "go", session=session)
                async for event in result.stream_events():
                    if event.type == "run_item_stream_event" and event.name in (
                        "tool_search_called",
                        "tool_search_output_created",
                    ):
                        events.append(event.name)
            else:
                await Runner.run(agent, "go", session=session)
        assert caught.value.run_data is not None
        assert [item.to_input_item() for item in caught.value.run_data.new_items] == expected
        assert (await session.get_items())[1:] == expected
        if result is not None:
            # Provider events already emitted before local execution must not be emitted twice.
            assert events == ["tool_search_called", "tool_search_output_created"]
            assert result.to_input_list()[1:] == expected
            restored = await RunState.from_json(agent, result.to_state().to_json())
            assert [item.to_input_item() for item in restored._generated_items] == expected
            await Runner.run(agent, restored)
            assert model.calls[-1].input[1:] == expected
        else:
            await Runner.run(agent, "continue", session=session)
            assert model.calls[-1].input[1:-1] == expected
    finally:
        session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("completed_on_followup", [False, True])
async def test_completed_program_child_retains_parent_after_sibling_failure(
    streaming, completed_on_followup
):
    from openai.types.responses import ResponseFunctionToolCall, ResponseReasoningItem
    from openai.types.responses.response_function_tool_call import CallerProgram
    from openai.types.responses.response_output_item import Program, ProgramOutput

    from agents import ProgrammaticToolCallingTool

    completed = asyncio.Event()
    effects = []

    @tool(allowed_callers=["programmatic"])
    async def lookup() -> str:
        effects.append("lookup")
        return "found"

    @tool(failure_error_function=None)
    async def fail() -> str:
        await completed.wait()
        raise ValueError("synthetic sibling failure")

    class Hooks(RunHooks):
        async def on_tool_end(self, context, agent, tool, result):
            completed.set()

    reasoning = ResponseReasoningItem(id="program_reasoning", type="reasoning", summary=[])
    program = Program(
        id="program_item",
        call_id="program",
        code="lookup()",
        fingerprint="synthetic",
        type="program",
    )
    caller = CallerProgram(type="program", caller_id="program")
    child = ResponseFunctionToolCall(
        id="child",
        call_id="lookup",
        name="lookup",
        arguments="{}",
        caller=caller,
        type="function_call",
    )
    program_output = ProgramOutput(
        id="program_output",
        call_id="program",
        result="found",
        status="completed",
        type="program_output",
    )
    fail_call = function_call("fail", {}, call_id="fail")
    first = [reasoning, program, child]
    failure_step = [program_output, fail_call] if completed_on_followup else [*first, fail_call]
    steps = [first, failure_step] if completed_on_followup else [failure_step]
    continuation = (
        [assistant_message("done")]
        if completed_on_followup
        else [program_output, assistant_message("done")]
    )
    model = ScriptedModel(
        [
            *(get_exact_output_stream_step(step) if streaming else step for step in steps),
            continuation,
        ]
    )
    agent = Agent(name="program", model=model, tools=[ProgrammaticToolCallingTool(), lookup, fail])
    session = SQLiteSession("program-failure")
    result = None
    try:
        with pytest.raises(UserError, match="synthetic sibling failure") as caught:
            if streaming:
                result = Runner.run_streamed(agent, "go", session=session, hooks=Hooks())
                async for _ in result.stream_events():
                    pass
            else:
                await Runner.run(agent, "go", session=session, hooks=Hooks())
        expected = [
            reasoning.model_dump(exclude_unset=True),
            program.model_dump(exclude_unset=True),
            child.model_dump(exclude_unset=True),
            {
                "type": "function_call_output",
                "call_id": "lookup",
                "output": "found",
                "caller": caller.model_dump(exclude_unset=True),
            },
        ]
        if completed_on_followup:
            expected.append(program_output.model_dump(exclude_unset=True))
        assert caught.value.run_data is not None
        assert [item.to_input_item() for item in caught.value.run_data.new_items] == expected
        assert (await session.get_items())[1:] == expected
        if result is not None:
            restored = await RunState.from_json(agent, result.to_state().to_json())
            continued = await Runner.run(agent, restored)
            assert model.calls[-1].input[1:] == expected
            assert continued.final_output == "done"
        assert effects == ["lookup"]
    finally:
        session.close()
