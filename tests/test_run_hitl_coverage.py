from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest
from openai import BadRequestError
from openai.types.responses import (
    ResponseComputerToolCall,
    ResponseCustomToolCall,
)
from openai.types.responses.response_function_tool_call import ResponseFunctionToolCall
from openai.types.responses.response_output_item import (
    LocalShellCall,
    McpApprovalRequest,
)

from agents import (
    Agent,
    HostedMCPTool,
    MCPToolApprovalRequest,
    ModelBehaviorError,
    RunContextWrapper,
    RunHooks,
    RunItem,
    Runner,
    ToolApprovalItem,
    UserError,
    function_tool,
)
from agents._run_impl import (
    NextStepFinalOutput,
    NextStepInterruption,
    NextStepRunAgain,
    ProcessedResponse,
    RunImpl,
    SingleStepResult,
    ToolRunMCPApprovalRequest,
)
from agents.items import ItemHelpers, ModelResponse, ToolCallItem, ToolCallOutputItem
from agents.result import RunResultStreaming
from agents.run import (
    AgentRunner,
    RunConfig,
    _copy_str_or_list,
    _ServerConversationTracker,
)
from agents.run_state import RunState
from agents.usage import Usage

from .fake_model import FakeModel
from .test_responses import get_function_tool_call, get_text_input_item, get_text_message
from .utils.simple_session import SimpleListSession


class LockingModel(FakeModel):
    """A FakeModel that simulates a conversation lock on the first stream call."""

    def __init__(self) -> None:
        super().__init__()
        self.lock_attempts = 0

    async def stream_response(self, *args, **kwargs):
        self.lock_attempts += 1
        if self.lock_attempts == 1:
            # Simulate the OpenAI Responses API conversation lock error
            response = httpx.Response(
                status_code=400,
                json={"error": {"code": "conversation_locked", "message": "locked"}},
                request=httpx.Request("POST", "https://example.com/responses"),
            )
            exc = BadRequestError("locked", response=response, body=response.json())
            exc.code = "conversation_locked"
            raise exc

        async for event in super().stream_response(*args, **kwargs):
            yield event


@pytest.mark.asyncio
async def test_streaming_retries_after_conversation_lock():
    """Ensure streaming retries after a conversation lock and rewinds inputs."""

    model = LockingModel()
    model.set_next_output([get_text_message("after_retry")])

    agent = Agent(name="test", model=model)
    session = SimpleListSession()

    input_items = [get_text_input_item("hello")]
    run_config = RunConfig(session_input_callback=lambda history, new: history + new)
    result = Runner.run_streamed(agent, input=input_items, session=session, run_config=run_config)

    # Drain the stream; the first attempt raises, the second should succeed.
    async for _ in result.stream_events():
        pass

    assert model.lock_attempts == 2
    assert result.final_output == "after_retry"

    # Session should only contain the original user item once, even after rewind.
    items = await session.get_items()
    user_items = [it for it in items if isinstance(it, dict) and it.get("role") == "user"]
    assert len(user_items) <= 1
    if user_items:
        assert cast(dict[str, Any], user_items[0]).get("content") == "hello"


@pytest.mark.asyncio
async def test_run_raises_for_session_list_without_callback():
    """Validate list input with session requires a session_input_callback (matches JS)."""

    agent = Agent(name="test", model=FakeModel())
    session = SimpleListSession()
    input_items = [get_text_input_item("hi")]

    with pytest.raises(UserError):
        await Runner.run(
            agent,
            input_items,
            session=session,
            run_config=RunConfig(),
        )


@pytest.mark.asyncio
async def test_blocking_resume_resolves_interruption():
    """Ensure blocking resume path handles interruptions and approvals (matches JS HITL)."""

    model = FakeModel()

    async def tool_fn() -> str:
        return "tool_result"

    async def needs_approval(_ctx, _params, _call_id) -> bool:
        return True

    tool = function_tool(tool_fn, name_override="test_tool", needs_approval=needs_approval)
    agent = Agent(name="test", model=model, tools=[tool])

    # First turn: tool call requiring approval
    from openai.types.responses import ResponseOutputMessage

    model.add_multiple_turn_outputs(
        [
            [
                cast(
                    ResponseOutputMessage,
                    {
                        "type": "function_call",
                        "name": "test_tool",
                        "call_id": "call-1",
                        "arguments": "{}",
                    },
                )
            ],
            [get_text_message("done")],
        ]
    )

    result1 = await Runner.run(agent, "do it")
    assert result1.interruptions, "should have an interruption for tool approval"

    state: RunState = result1.to_state()
    # Filter to only ToolApprovalItem instances
    approval_items = [item for item in result1.interruptions if isinstance(item, ToolApprovalItem)]
    if approval_items:
        state.approve(approval_items[0])

    # Resume from state; should execute approved tool and complete.
    result2 = await Runner.run(agent, state)
    assert result2.final_output == "done"


@pytest.mark.asyncio
async def test_blocking_interruption_saves_session_items_without_approval_items():
    """Blocking run with session should save input/output but skip approval items."""

    model = FakeModel()

    async def tool_fn() -> str:
        return "tool_result"

    async def needs_approval(_ctx, _params, _call_id) -> bool:
        return True

    tool = function_tool(
        tool_fn, name_override="needs_approval_tool", needs_approval=needs_approval
    )
    agent = Agent(name="test", model=model, tools=[tool])

    session = SimpleListSession()
    run_config = RunConfig(session_input_callback=lambda history, new: history + new)

    # First turn: tool call requiring approval
    model.set_next_output(
        [
            cast(
                Any,
                {
                    "type": "function_call",
                    "name": "needs_approval_tool",
                    "call_id": "call-1",
                    "arguments": "{}",
                },
            )
        ]
    )

    result = await Runner.run(
        agent, [get_text_input_item("hello")], session=session, run_config=run_config
    )
    assert result.interruptions, "should have a tool approval interruption"

    items = await session.get_items()
    # Only the user input should be persisted; approval items should not be saved.
    assert any(isinstance(it, dict) and it.get("role") == "user" for it in items)
    assert not any(
        isinstance(it, dict) and cast(dict[str, Any], it).get("type") == "tool_approval_item"
        for it in items
    )


@pytest.mark.asyncio
async def test_streaming_interruption_with_session_saves_without_approval_items():
    """Streaming run with session saves items and filters approval items."""

    model = FakeModel()

    async def tool_fn() -> str:
        return "tool_result"

    async def needs_approval(_ctx, _params, _call_id) -> bool:
        return True

    tool = function_tool(tool_fn, name_override="stream_tool", needs_approval=needs_approval)
    agent = Agent(name="test", model=model, tools=[tool])

    session = SimpleListSession()
    run_config = RunConfig(session_input_callback=lambda history, new: history + new)

    model.set_next_output(
        [
            cast(
                Any,
                {
                    "type": "function_call",
                    "name": "stream_tool",
                    "call_id": "call-1",
                    "arguments": "{}",
                },
            )
        ]
    )

    result = Runner.run_streamed(
        agent, [get_text_input_item("hi")], session=session, run_config=run_config
    )
    async for _ in result.stream_events():
        pass

    assert result.interruptions, "should surface interruptions"
    items = await session.get_items()
    assert any(isinstance(it, dict) and it.get("role") == "user" for it in items)
    assert not any(
        isinstance(it, dict) and cast(dict[str, Any], it).get("type") == "tool_approval_item"
        for it in items
    )


def test_streaming_requires_callback_when_session_and_list_input():
    """Streaming run should raise if list input used with session without callback."""

    agent = Agent(name="test", model=FakeModel())
    session = SimpleListSession()

    with pytest.raises(UserError):
        Runner.run_streamed(agent, [{"role": "user", "content": "hi"}], session=session)


@pytest.mark.asyncio
async def test_streaming_resume_with_session_and_approved_tool():
    """Streaming resume path with session saves input and executes approved tool."""

    model = FakeModel()

    async def tool_fn() -> str:
        return "tool_result"

    async def needs_approval(_ctx, _params, _call_id) -> bool:
        return True

    tool = function_tool(tool_fn, name_override="stream_resume_tool", needs_approval=needs_approval)
    agent = Agent(name="test", model=model, tools=[tool])

    session = SimpleListSession()
    run_config = RunConfig(session_input_callback=lambda history, new: history + new)

    model.add_multiple_turn_outputs(
        [
            [
                cast(
                    Any,
                    {
                        "type": "function_call",
                        "name": "stream_resume_tool",
                        "call_id": "call-1",
                        "arguments": "{}",
                    },
                )
            ],
            [get_text_message("final")],
        ]
    )

    # First run -> interruption saved to session (without approval item)
    result1 = Runner.run_streamed(
        agent, [get_text_input_item("hello")], session=session, run_config=run_config
    )
    async for _ in result1.stream_events():
        pass

    assert result1.interruptions
    state = result1.to_state()
    state.approve(result1.interruptions[0])

    # Resume from state -> executes tool, completes
    result2 = Runner.run_streamed(agent, state, session=session, run_config=run_config)
    async for _ in result2.stream_events():
        pass

    assert result2.final_output == "final"
    items = await session.get_items()
    user_items = [it for it in items if isinstance(it, dict) and it.get("role") == "user"]
    assert len(user_items) == 1
    assert cast(dict[str, Any], user_items[0]).get("content") == "hello"
    assert not any(
        isinstance(it, dict) and cast(dict[str, Any], it).get("type") == "tool_approval_item"
        for it in items
    )


@pytest.mark.asyncio
async def test_streaming_uses_server_conversation_tracker_no_session_duplication():
    """Streaming with server-managed conversation should not duplicate input when resuming."""

    model = FakeModel()
    agent = Agent(name="test", model=model)

    # First turn response
    model.set_next_output([get_text_message("first")])
    result1 = Runner.run_streamed(
        agent, input="hello", conversation_id="conv123", previous_response_id="resp123"
    )
    async for _ in result1.stream_events():
        pass

    state = result1.to_state()

    # Second turn response
    model.set_next_output([get_text_message("second")])
    result2 = Runner.run_streamed(
        agent, state, conversation_id="conv123", previous_response_id="resp123"
    )
    async for _ in result2.stream_events():
        pass

    assert result2.final_output == "second"
    # Ensure history not duplicated: only two assistant messages produced across runs
    all_messages = [
        item
        for resp in result2.raw_responses
        for item in resp.output
        if isinstance(item, dict) or getattr(item, "type", "") == "message"
    ]
    assert len(all_messages) <= 2


@pytest.mark.asyncio
async def test_execute_approved_tools_with_invalid_raw_item_type():
    """Tool approval with non-ResponseFunctionToolCall raw_item produces error output."""

    async def tool_fn() -> str:
        return "ok"

    async def needs_approval_fn(
        context: RunContextWrapper[Any], args: dict[str, Any], tool_name: str
    ) -> bool:
        return True

    tool = function_tool(
        tool_fn, name_override="invalid_raw_tool", needs_approval=needs_approval_fn
    )
    agent = Agent(name="test", model=FakeModel(), tools=[tool])

    # Raw item is dict instead of ResponseFunctionToolCall
    approval_item = ToolApprovalItem(
        agent=agent,
        raw_item={"name": "invalid_raw_tool", "call_id": "c1", "type": "function_call"},
    )

    context_wrapper: RunContextWrapper[Any] = RunContextWrapper(context={})
    context_wrapper.approve_tool(approval_item, always_approve=True)
    generated: list[RunItem] = []

    await AgentRunner._execute_approved_tools_static(
        agent=agent,
        interruptions=[approval_item],
        context_wrapper=context_wrapper,
        generated_items=generated,
        run_config=RunConfig(),
        hooks=RunHooks(),
    )

    assert generated, "Should emit a ToolCallOutputItem for invalid raw_item type"
    assert "invalid raw_item type" in generated[0].output


def test_server_conversation_tracker_prime_is_idempotent():
    tracker = _ServerConversationTracker(conversation_id="c1", previous_response_id=None)
    original_input = [{"id": "a", "type": "message"}]
    tracker.prime_from_state(
        original_input=original_input,  # type: ignore[arg-type]
        generated_items=[],
        model_responses=[],
        session_items=None,
    )
    # Second call should early-return without raising
    tracker.prime_from_state(
        original_input=original_input,  # type: ignore[arg-type]
        generated_items=[],
        model_responses=[],
        session_items=None,
    )
    assert tracker.sent_initial_input is True


@pytest.mark.asyncio
async def test_resume_interruption_with_server_conversation_tracker_final_output():
    """Resuming HITL with server-managed conversation should finalize output without session saves."""  # noqa: E501

    async def tool_fn() -> str:
        return "approved_output"

    async def needs_approval(*_args, **_kwargs) -> bool:
        return True

    tool = function_tool(
        tool_fn,
        name_override="echo_tool",
        needs_approval=needs_approval,
        failure_error_function=None,
    )
    agent = Agent(
        name="test",
        model=FakeModel(),
        tools=[tool],
        tool_use_behavior="stop_on_first_tool",
    )
    model = cast(FakeModel, agent.model)

    # First turn: model requests the tool (requires approval)
    model.set_next_output([get_function_tool_call("echo_tool", "{}", call_id="call-1")])
    first_result = await Runner.run(agent, "hello", conversation_id="conv-1")
    assert first_result.interruptions

    state = first_result.to_state()
    state.approve(state.get_interruptions()[0], always_approve=True)

    # Resume with same conversation id to exercise server conversation tracker resume path.
    resumed = await Runner.run(agent, state, conversation_id="conv-1")

    assert resumed.final_output == "approved_output"
    assert not resumed.interruptions


def test_filter_incomplete_function_calls_drops_orphans():
    """Ensure incomplete function calls are removed while valid history is preserved."""

    items = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        {"type": "function_call", "name": "foo", "call_id": "orphan", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "kept", "output": "ok"},
        {"type": "function_call", "name": "foo", "call_id": "kept", "arguments": "{}"},
    ]

    filtered = AgentRunner._filter_incomplete_function_calls(items)  # type: ignore[arg-type]

    assert any(item.get("call_id") == "kept" for item in filtered if isinstance(item, dict))
    assert not any(item.get("call_id") == "orphan" for item in filtered if isinstance(item, dict))


def test_normalize_input_items_strips_provider_data_and_normalizes_fields():
    """Top-level provider data should be stripped and callId normalized when resuming HITL runs."""

    items = [
        {
            "type": "message",
            "role": "user",
            "providerData": {"foo": "bar"},
            "provider_data": {"baz": "qux"},
            "content": [{"type": "input_text", "text": "hi"}],
        },
        {
            "type": "function_call_result",
            "callId": "abc123",
            "name": "should_drop",
            "status": "completed",
            "output": {"type": "text", "text": "ok"},
        },
    ]

    normalized = AgentRunner._normalize_input_items(items)  # type: ignore[arg-type]

    first = cast(dict[str, Any], normalized[0])
    assert "providerData" not in first and "provider_data" not in first

    second = cast(dict[str, Any], normalized[1])
    assert second["type"] == "function_call_output"
    assert "name" not in second and "status" not in second
    assert second.get("call_id") == "abc123"


@pytest.mark.asyncio
async def test_streaming_resume_with_server_tracker_and_approved_tool():
    """Streaming resume with server-managed conversation should resolve interruption."""

    async def tool_fn() -> str:
        return "approved_output"

    async def needs_approval(*_args, **_kwargs) -> bool:
        return True

    tool = function_tool(
        tool_fn,
        name_override="stream_server_tool",
        needs_approval=needs_approval,
        failure_error_function=None,
    )
    agent = Agent(
        name="test",
        model=FakeModel(),
        tools=[tool],
        tool_use_behavior="stop_on_first_tool",
    )
    model = cast(FakeModel, agent.model)

    model.set_next_output([get_function_tool_call("stream_server_tool", "{}", call_id="call-1")])
    result1 = Runner.run_streamed(agent, "hello", conversation_id="conv-stream-1")
    async for _ in result1.stream_events():
        pass

    assert result1.interruptions
    state = result1.to_state()
    state.approve(state.get_interruptions()[0], always_approve=True)

    result2 = Runner.run_streamed(agent, state, conversation_id="conv-stream-1")
    async for _ in result2.stream_events():
        pass

    assert result2.final_output == "approved_output"


@pytest.mark.asyncio
async def test_blocking_resume_with_server_tracker_final_output():
    """Blocking resume path with server-managed conversation should resolve interruptions."""

    async def tool_fn() -> str:
        return "ok"

    async def needs_approval(*_args, **_kwargs) -> bool:
        return True

    tool = function_tool(
        tool_fn,
        name_override="blocking_server_tool",
        needs_approval=needs_approval,
        failure_error_function=None,
    )
    agent = Agent(
        name="test",
        model=FakeModel(),
        tools=[tool],
        tool_use_behavior="stop_on_first_tool",
    )
    model = cast(FakeModel, agent.model)

    model.set_next_output([get_function_tool_call("blocking_server_tool", "{}", call_id="c-block")])
    first = await Runner.run(agent, "hi", conversation_id="conv-block")
    assert first.interruptions

    state = first.to_state()
    state.approve(first.interruptions[0], always_approve=True)

    # Resume with same conversation id to hit server tracker resume branch.
    second = await Runner.run(agent, state, conversation_id="conv-block")

    assert second.final_output == "ok"
    assert not second.interruptions


@pytest.mark.asyncio
async def test_resolve_interrupted_turn_reconstructs_function_runs():
    """Pending approvals should reconstruct function runs when state lacks processed functions."""

    async def tool_fn() -> str:
        return "approved"

    async def needs_approval(*_args, **_kwargs) -> bool:
        return True

    tool = function_tool(
        tool_fn,
        name_override="reconstruct_tool",
        needs_approval=needs_approval,
        failure_error_function=None,
    )
    agent = Agent(
        name="test",
        model=FakeModel(),
        tools=[tool],
        tool_use_behavior="stop_on_first_tool",
    )
    context_wrapper: RunContextWrapper[Any] = RunContextWrapper(context={})
    run_state = RunState(context_wrapper, original_input="hi", starting_agent=agent)

    approval = ToolApprovalItem(
        agent=agent,
        raw_item={
            "type": "function_call",
            "name": "reconstruct_tool",
            "callId": "c123",
            "arguments": "{}",
        },
    )
    context_wrapper.approve_tool(approval, always_approve=True)
    run_state._current_step = NextStepInterruption(interruptions=[approval])
    run_state._generated_items = [approval]
    run_state._model_responses = [ModelResponse(output=[], usage=Usage(), response_id="resp")]
    run_state._last_processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    # Inject AgentRunner into module globals to mirror normal runtime import order.
    import agents._run_impl as run_impl

    run_impl.AgentRunner = AgentRunner  # type: ignore[attr-defined]

    turn_result = await RunImpl.resolve_interrupted_turn(
        agent=agent,
        original_input=run_state._original_input,
        original_pre_step_items=run_state._generated_items,
        new_response=run_state._model_responses[-1],
        processed_response=run_state._last_processed_response,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
        run_state=run_state,
    )

    from agents._run_impl import NextStepFinalOutput

    assert isinstance(turn_result.next_step, NextStepFinalOutput)
    assert turn_result.next_step.output == "approved"


@pytest.mark.asyncio
async def test_mcp_approval_requests_emit_response_items():
    """Hosted MCP approval callbacks should produce response items without interruptions."""

    approvals: list[object] = []

    def on_approval(request: MCPToolApprovalRequest) -> dict[str, object]:
        approvals.append(request.data)
        return {"approve": True, "reason": "ok"}

    mcp_tool = HostedMCPTool(
        tool_config={"type": "mcp", "server_label": "srv"},
        on_approval_request=on_approval,  # type: ignore[arg-type]
    )
    agent = Agent(name="test", model=FakeModel(), tools=[mcp_tool])
    context_wrapper: RunContextWrapper[Any] = RunContextWrapper(context={})

    mcp_request = McpApprovalRequest(  # type: ignore[call-arg]
        id="req-1",
        server_label="srv",
        type="mcp_approval_request",
        approval_url="https://example.com",
        name="tool1",
        arguments="{}",
    )
    response = ModelResponse(output=[mcp_request], usage=Usage(), response_id="resp")

    processed = RunImpl.process_model_response(
        agent=agent,
        all_tools=[mcp_tool],
        response=response,
        output_schema=None,
        handoffs=[],
    )

    step = await RunImpl.execute_tools_and_side_effects(
        agent=agent,
        original_input="hi",
        pre_step_items=[],
        new_response=response,
        processed_response=processed,
        output_schema=None,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
    )

    assert isinstance(step.next_step, NextStepRunAgain)
    assert any(item.type == "mcp_approval_response_item" for item in step.new_step_items)
    assert approvals, "Approval callback should have been invoked"


def test_run_state_to_json_deduplicates_last_processed_new_items():
    """RunState serialization should merge generated and lastProcessedResponse new_items without duplicates."""  # noqa: E501

    agent = Agent(name="test", model=FakeModel())
    context_wrapper: RunContextWrapper[Any] = RunContextWrapper(context={})
    state = RunState(
        context_wrapper, original_input=[{"type": "message", "content": "hi"}], starting_agent=agent
    )

    # Existing generated item with call_id
    existing = ToolApprovalItem(
        agent=agent,
        raw_item={"type": "function_call", "call_id": "c1", "name": "foo", "arguments": "{}"},
    )
    state._generated_items = [existing]

    # last_processed_response contains an item with same call_id; should be deduped
    last_new_item = ToolApprovalItem(
        agent=agent,
        raw_item={"type": "function_call", "call_id": "c1", "name": "foo", "arguments": "{}"},
    )
    state._last_processed_response = ProcessedResponse(
        new_items=[last_new_item],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )
    state._model_responses = [ModelResponse(output=[], usage=Usage(), response_id="r1")]
    state._current_step = NextStepInterruption(interruptions=[existing])

    serialized = state.to_json()

    generated = serialized["generatedItems"]
    assert len(generated) == 1
    assert generated[0]["rawItem"]["callId"] == "c1"


@pytest.mark.asyncio
async def test_apply_patch_without_tool_raises_model_behavior_error():
    """Model emitting apply_patch without tool should raise ModelBehaviorError (HITL tool flow)."""

    model = FakeModel()
    # Emit apply_patch function call without registering apply_patch tool
    model.set_next_output(
        [
            ResponseFunctionToolCall(
                id="1",
                call_id="cp1",
                type="function_call",
                name="apply_patch",
                arguments='{"patch":"diff"}',
            )
        ]
    )
    agent = Agent(name="test", model=model)

    with pytest.raises(ModelBehaviorError):
        await Runner.run(agent, "hi")


@pytest.mark.asyncio
async def test_resolve_interrupted_turn_reconstructs_and_keeps_pending_hosted_mcp():
    """resolve_interrupted_turn should rebuild function runs and keep hosted MCP approvals pending."""  # noqa: E501

    async def on_approval(req):
        # Leave approval undecided to keep it pending
        return {"approve": False}

    tool_name = "foo"

    @function_tool(name_override=tool_name)
    def foo_tool():
        return "ok"

    mcp_tool = HostedMCPTool(
        tool_config={"type": "mcp", "server_label": "srv"},
        on_approval_request=on_approval,
    )
    agent = Agent(name="test", model=FakeModel(), tools=[foo_tool, mcp_tool])
    context_wrapper: RunContextWrapper[Any] = RunContextWrapper(context={})

    class HashableToolApproval(ToolApprovalItem):
        __hash__ = object.__hash__

    approval_item = HashableToolApproval(
        agent=agent,
        raw_item={"type": "function_call", "call_id": "c1", "name": tool_name, "arguments": "{}"},
    )
    hosted_request = HashableToolApproval(
        agent=agent,
        raw_item={
            "type": "hosted_tool_call",
            "id": "req1",
            "name": "hosted",
            "providerData": {"type": "mcp_approval_request"},
        },
    )

    # Pre-approve hosted request so resolve_interrupted_turn emits response item and skips set()
    context_wrapper.approve_tool(hosted_request, always_approve=True)

    result = await RunImpl.resolve_interrupted_turn(
        agent=agent,
        original_input="hi",
        original_pre_step_items=[approval_item, hosted_request],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="r1"),
        processed_response=ProcessedResponse(
            new_items=[],
            handoffs=[],
            functions=[],
            computer_actions=[],
            local_shell_calls=[],
            shell_calls=[],
            apply_patch_calls=[],
            tools_used=[],
            mcp_approval_requests=[
                ToolRunMCPApprovalRequest(request_item=hosted_request, mcp_tool=mcp_tool)  # type: ignore[arg-type]
            ],
            interruptions=[],
        ),
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
    )

    # Function tool should have executed and produced new items, and approval response should be emitted  # noqa: E501
    assert any(item.type == "tool_call_output_item" for item in result.new_step_items)
    assert any(
        isinstance(item.raw_item, dict)
        and cast(dict[str, Any], item.raw_item).get("providerData", {}).get("type")
        == "mcp_approval_response"
        for item in result.new_step_items
    )


@pytest.mark.asyncio
async def test_resolve_interrupted_turn_pending_hosted_mcp_preserved():
    """Pending hosted MCP approvals should remain in pre_step_items when still awaiting a decision."""  # noqa: E501

    async def on_approval(req):
        return {"approve": False}

    tool_name = "foo"

    @function_tool(name_override=tool_name)
    def foo_tool():
        return "ok"

    mcp_tool = HostedMCPTool(
        tool_config={"type": "mcp", "server_label": "srv"},
        on_approval_request=on_approval,
    )
    agent = Agent(name="test", model=FakeModel(), tools=[foo_tool, mcp_tool])
    context_wrapper: RunContextWrapper[Any] = RunContextWrapper(context={})

    class HashableToolApproval(ToolApprovalItem):
        __hash__ = object.__hash__

    approval_item = HashableToolApproval(
        agent=agent,
        raw_item={"type": "function_call", "call_id": "c1", "name": tool_name, "arguments": "{}"},
    )
    hosted_request = HashableToolApproval(
        agent=agent,
        raw_item={
            "type": "hosted_tool_call",
            "id": "req1",
            "name": "hosted",
            "providerData": {"type": "mcp_approval_request"},
        },
    )

    result = await RunImpl.resolve_interrupted_turn(
        agent=agent,
        original_input="hi",
        original_pre_step_items=[approval_item, hosted_request],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="r1"),
        processed_response=ProcessedResponse(
            new_items=[],
            handoffs=[],
            functions=[],
            computer_actions=[],
            local_shell_calls=[],
            shell_calls=[],
            apply_patch_calls=[],
            tools_used=[],
            mcp_approval_requests=[
                ToolRunMCPApprovalRequest(request_item=hosted_request, mcp_tool=mcp_tool)  # type: ignore[arg-type]
            ],
            interruptions=[],
        ),
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
    )

    assert hosted_request in result.pre_step_items
    assert isinstance(result.next_step, NextStepRunAgain)
    assert isinstance(result.next_step, NextStepRunAgain)


def test_server_conversation_tracker_filters_seen_items():
    """ServerConversationTracker should skip already-sent items and tool outputs."""

    agent = Agent(name="test", model=FakeModel())
    tracker = _ServerConversationTracker(conversation_id="c1")

    original_input = [{"id": "m1", "type": "message", "content": "hi"}]

    tracker.prime_from_state(
        original_input=original_input,  # type: ignore[arg-type]
        generated_items=[],
        model_responses=[],
        session_items=[cast(Any, {"id": "sess1", "type": "message", "content": "old"})],
    )
    tracker.server_tool_call_ids.add("call1")

    generated_items = [
        ToolCallOutputItem(
            agent=agent,
            raw_item={"type": "function_call_output", "call_id": "call1", "output": "ok"},
            output="ok",
        ),
        ToolCallItem(agent=agent, raw_item={"id": "m1", "type": "message", "content": "dup"}),
        ToolCallItem(agent=agent, raw_item={"id": "m2", "type": "message", "content": "new"}),
    ]

    prepared = tracker.prepare_input(original_input=original_input, generated_items=generated_items)  # type: ignore[arg-type]

    assert prepared == [{"id": "m2", "type": "message", "content": "new"}]


def test_server_conversation_tracker_rewind_initial_input():
    """rewind_initial_input should queue items to resend after a retry."""

    tracker = _ServerConversationTracker(previous_response_id="prev")

    original_input: list[Any] = [{"id": "m1", "type": "message", "content": "hi"}]
    # Prime and send initial input
    tracker.prepare_input(original_input=original_input, generated_items=[])
    tracker.mark_input_as_sent(original_input)

    rewind_items: list[Any] = [{"id": "m2", "type": "message", "content": "redo"}]
    tracker.rewind_input(rewind_items)

    assert tracker.remaining_initial_input == rewind_items


@pytest.mark.asyncio
async def test_run_resume_from_interruption_persists_new_items(monkeypatch):
    """AgentRunner.run should persist resumed interruption items before returning."""

    agent = Agent(name="test", model=FakeModel())
    session = SimpleListSession()
    context_wrapper: RunContextWrapper[Any] = RunContextWrapper(context={})

    # Pending approval in current step
    approval_item = ToolApprovalItem(
        agent=agent,
        raw_item={"type": "function_call", "call_id": "c1", "name": "foo", "arguments": "{}"},
    )

    # Stub resolve_interrupted_turn to return new items and stay interrupted
    async def fake_resolve_interrupted_turn(**kwargs):
        return SingleStepResult(
            original_input="hi",
            model_response=ModelResponse(
                output=[get_text_message("ok")], usage=Usage(), response_id="r1"
            ),
            pre_step_items=[],
            new_step_items=[
                ToolCallItem(
                    agent=agent,
                    raw_item={
                        "type": "function_call",
                        "call_id": "c1",
                        "name": "foo",
                        "arguments": "{}",
                    },
                )
            ],
            next_step=NextStepInterruption([approval_item]),
            tool_input_guardrail_results=[],
            tool_output_guardrail_results=[],
        )

    monkeypatch.setattr(RunImpl, "resolve_interrupted_turn", fake_resolve_interrupted_turn)

    # Build RunState as if we were resuming after an approval interruption
    run_state = RunState(
        context=context_wrapper,
        original_input=[get_text_input_item("hello")],
        starting_agent=agent,
    )
    run_state._current_step = NextStepInterruption([approval_item])
    run_state._generated_items = [approval_item]
    run_state._model_responses = [
        ModelResponse(output=[get_text_message("before")], usage=Usage(), response_id="prev")
    ]
    run_state._last_processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[approval_item],
    )

    result = await Runner.run(agent, run_state, session=session)

    assert isinstance(result.interruptions, list) and result.interruptions
    # Ensure new items were persisted to the session during resume
    assert len(session._items) > 0


@pytest.mark.asyncio
async def test_run_with_session_list_input_requires_callback():
    """Passing list input with a session but no session_input_callback should raise UserError."""

    agent = Agent(name="test", model=FakeModel())
    session = SimpleListSession()
    with pytest.raises(UserError):
        await Runner.run(agent, input=[get_text_input_item("hi")], session=session)


@pytest.mark.asyncio
async def test_resume_sets_persisted_item_count_when_zero(monkeypatch):
    """Resuming with generated items and zero counter should set persisted count to len(generated_items)."""  # noqa: E501

    agent = Agent(name="test", model=FakeModel())
    context_wrapper: RunContextWrapper[Any] = RunContextWrapper(context={})
    generated_item = ToolCallItem(
        agent=agent,
        raw_item={"type": "function_call", "call_id": "c1", "name": "foo", "arguments": "{}"},
    )

    run_state = RunState(
        context=context_wrapper,
        original_input=[get_text_input_item("hello")],
        starting_agent=agent,
    )
    run_state._generated_items = [generated_item]
    run_state._current_turn_persisted_item_count = 0
    run_state._model_responses = [
        ModelResponse(output=[get_text_message("ok")], usage=Usage(), response_id="r1")
    ]
    run_state._last_processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    # Stub RunImpl._run_single_turn to end the run immediately with a final output
    async def fake_run_single_turn(*args, **kwargs):
        return SingleStepResult(
            original_input="hello",
            model_response=run_state._model_responses[-1],
            pre_step_items=[],
            new_step_items=[],
            next_step=NextStepFinalOutput("done"),
            tool_input_guardrail_results=[],
            tool_output_guardrail_results=[],
        )

    monkeypatch.setattr(AgentRunner, "_run_single_turn", fake_run_single_turn)

    result = await Runner.run(agent, run_state)
    assert result.final_output == "done"
    assert run_state._current_turn_persisted_item_count == len(run_state._generated_items)


@pytest.mark.parametrize(
    "output_item, expected_message",
    [
        (
            cast(
                Any,
                {
                    "id": "sh1",
                    "call_id": "call1",
                    "type": "shell_call",
                    "action": {"type": "exec", "commands": ["echo hi"]},
                    "status": "in_progress",
                },
            ),
            "shell call without a shell tool",
        ),
        (
            ResponseCustomToolCall(
                type="custom_tool_call",
                name="apply_patch",
                call_id="call1",
                input=json.dumps({"patch": "diff"}),
            ),
            "apply_patch call without an apply_patch tool",
        ),
        (
            ResponseComputerToolCall(
                id="c1",
                call_id="call1",
                type="computer_call",
                action={"type": "keypress", "keys": ["a"]},  # type: ignore[arg-type]
                pending_safety_checks=[],
                status="in_progress",
            ),
            "computer action without a computer tool",
        ),
        (
            LocalShellCall(
                id="s1",
                call_id="call1",
                type="local_shell_call",
                action={"type": "exec", "command": ["echo", "hi"], "env": {}},  # type: ignore[arg-type]
                status="in_progress",
            ),
            "local shell call without a local shell tool",
        ),
    ],
)
def test_process_model_response_missing_tools_raise(output_item, expected_message):
    """process_model_response should error when model emits tool calls without corresponding tools."""  # noqa: E501

    agent = Agent(name="test", model=FakeModel())
    response = ModelResponse(output=[output_item], usage=Usage(), response_id="r1")

    with pytest.raises(ModelBehaviorError, match=expected_message):
        RunImpl.process_model_response(
            agent=agent,
            all_tools=[],
            response=response,
            output_schema=None,
            handoffs=[],
        )


@pytest.mark.asyncio
async def test_execute_mcp_approval_requests_handles_reason():
    """execute_mcp_approval_requests should include rejection reason in response."""

    async def on_request(req):
        return {"approve": False, "reason": "not allowed"}

    mcp_tool = HostedMCPTool(
        tool_config={"type": "mcp", "server_label": "srv"},
        on_approval_request=on_request,
    )
    request_item = cast(
        McpApprovalRequest,
        {
            "id": "req-1",
            "server_label": "srv",
            "type": "mcp_approval_request",
            "approval_url": "https://example.com",
            "name": "tool1",
            "arguments": "{}",
        },
    )
    agent = Agent(name="test", model=FakeModel(), tools=[mcp_tool])
    context_wrapper: RunContextWrapper[Any] = RunContextWrapper(context={})

    responses = await RunImpl.execute_mcp_approval_requests(
        agent=agent,
        approval_requests=[ToolRunMCPApprovalRequest(request_item=request_item, mcp_tool=mcp_tool)],
        context_wrapper=context_wrapper,
    )

    assert len(responses) == 1
    raw = responses[0].raw_item
    assert cast(dict[str, Any], raw).get("approval_request_id") == "req-1"
    assert cast(dict[str, Any], raw).get("approve") is False
    assert cast(dict[str, Any], raw).get("reason") == "not allowed"


@pytest.mark.asyncio
async def test_rewind_session_items_strips_stray_and_waits_cleanup():
    session = SimpleListSession()
    target = {"content": "hi", "role": "user"}
    # Order matters: pop_item pops from end
    session._items = [
        cast(Any, {"id": "server", "type": "message"}),
        cast(Any, {"id": "stray", "type": "message"}),
        cast(Any, target),
    ]

    tracker = _ServerConversationTracker(conversation_id="convX", previous_response_id=None)
    tracker.server_item_ids.add("server")

    await AgentRunner._rewind_session_items(session, [cast(Any, target)], tracker)

    items = await session.get_items()
    # Should have removed the target and stray items during rewind/strip
    assert all(it.get("id") == "server" for it in items) or items == []


@pytest.mark.asyncio
async def test_maybe_get_openai_conversation_id():
    class SessionWithId(SimpleListSession):
        def _get_session_id(self):
            return self.session_id

    session = SessionWithId(session_id="conv-123")
    conv_id = await AgentRunner._maybe_get_openai_conversation_id(session)
    assert conv_id == "conv-123"


@pytest.mark.asyncio
async def test_start_streaming_fresh_run_exercises_persistence(monkeypatch):
    """Cover the fresh streaming loop and guardrail finalization paths."""

    starting_input = [get_text_input_item("hi")]
    agent = Agent(name="agent", instructions="hi", model=None)
    context_wrapper = RunContextWrapper(context=None)
    run_config = RunConfig()

    async def fake_prepare_input_with_session(
        cls,
        input,
        session,
        session_input_callback,
        *,
        include_history_in_prepared_input=True,
        preserve_dropped_new_items=False,
    ):
        # Return the input as both prepared input and snapshot
        return input, ItemHelpers.input_to_new_input_list(input)

    async def fake_get_all_tools(cls, agent_param, context_param):
        return []

    async def fake_get_handoffs(cls, agent_param, context_param):
        return []

    def fake_get_output_schema(cls, agent_param):
        return None

    async def fake_run_single_turn_streamed(
        cls,
        streamed_result,
        agent_param,
        hooks,
        context_param,
        run_config_param,
        should_run_agent_start_hooks,
        tool_use_tracker,
        all_tools,
        server_conversation_tracker=None,
        session=None,
        session_items_to_rewind=None,
        pending_server_items=None,
    ):
        model_response = ModelResponse(output=[], usage=Usage(), response_id="resp")
        return SingleStepResult(
            original_input=streamed_result.input,
            model_response=model_response,
            pre_step_items=[],
            new_step_items=[],
            next_step=NextStepFinalOutput(output="done"),
            tool_input_guardrail_results=[],
            tool_output_guardrail_results=[],
            processed_response=None,
        )

    monkeypatch.setattr(
        AgentRunner, "_prepare_input_with_session", classmethod(fake_prepare_input_with_session)
    )
    monkeypatch.setattr(AgentRunner, "_get_all_tools", classmethod(fake_get_all_tools))
    monkeypatch.setattr(AgentRunner, "_get_handoffs", classmethod(fake_get_handoffs))
    monkeypatch.setattr(AgentRunner, "_get_output_schema", classmethod(fake_get_output_schema))
    monkeypatch.setattr(
        AgentRunner, "_run_single_turn_streamed", classmethod(fake_run_single_turn_streamed)
    )

    streamed_result = RunResultStreaming(
        input=_copy_str_or_list(starting_input),
        new_items=[],
        current_agent=agent,
        raw_responses=[],
        final_output=None,
        is_complete=False,
        current_turn=0,
        max_turns=1,
        input_guardrail_results=[],
        output_guardrail_results=[],
        tool_input_guardrail_results=[],
        tool_output_guardrail_results=[],
        _current_agent_output_schema=None,
        trace=None,
        context_wrapper=context_wrapper,
        interruptions=[],
        _current_turn_persisted_item_count=0,
        _original_input=_copy_str_or_list(starting_input),
    )

    await AgentRunner._start_streaming(
        starting_input=_copy_str_or_list(starting_input),
        streamed_result=streamed_result,
        starting_agent=agent,
        max_turns=1,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=run_config,
        previous_response_id=None,
        auto_previous_response_id=False,
        conversation_id=None,
        session=None,
        run_state=None,
        is_resumed_state=False,
    )

    assert streamed_result.is_complete
    assert streamed_result.final_output == "done"
    assert streamed_result.raw_responses and streamed_result.raw_responses[-1].response_id == "resp"
