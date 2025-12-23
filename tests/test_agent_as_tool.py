from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from openai.types.responses import ResponseOutputMessage, ResponseOutputText
from openai.types.responses.response_function_tool_call import ResponseFunctionToolCall
from pydantic import BaseModel

from agents import (
    Agent,
    AgentBase,
    AgentToolStreamEvent,
    FunctionTool,
    MessageOutputItem,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    Runner,
    Session,
    TResponseInputItem,
)
from agents.stream_events import AgentUpdatedStreamEvent, RawResponsesStreamEvent
from agents.tool_context import ToolContext


class BoolCtx(BaseModel):
    enable_tools: bool


@pytest.mark.asyncio
async def test_agent_as_tool_is_enabled_bool():
    """Test that agent.as_tool() respects static boolean is_enabled parameter."""
    # Create a simple agent
    agent = Agent(
        name="test_agent",
        instructions="You are a test agent that says hello.",
    )

    # Create tool with is_enabled=False
    disabled_tool = agent.as_tool(
        tool_name="disabled_agent_tool",
        tool_description="A disabled agent tool",
        is_enabled=False,
    )

    # Create tool with is_enabled=True (default)
    enabled_tool = agent.as_tool(
        tool_name="enabled_agent_tool",
        tool_description="An enabled agent tool",
        is_enabled=True,
    )

    # Create another tool with default is_enabled (should be True)
    default_tool = agent.as_tool(
        tool_name="default_agent_tool",
        tool_description="A default agent tool",
    )

    # Create test agent that uses these tools
    orchestrator = Agent(
        name="orchestrator",
        instructions="You orchestrate other agents.",
        tools=[disabled_tool, enabled_tool, default_tool],
    )

    # Test with any context
    context = RunContextWrapper(BoolCtx(enable_tools=True))

    # Get all tools - should filter out the disabled one
    tools = await orchestrator.get_all_tools(context)
    tool_names = [tool.name for tool in tools]

    assert "enabled_agent_tool" in tool_names
    assert "default_agent_tool" in tool_names
    assert "disabled_agent_tool" not in tool_names


@pytest.mark.asyncio
async def test_agent_as_tool_is_enabled_callable():
    """Test that agent.as_tool() respects callable is_enabled parameter."""
    # Create a simple agent
    agent = Agent(
        name="test_agent",
        instructions="You are a test agent that says hello.",
    )

    # Create tool with callable is_enabled
    async def cond_enabled(ctx: RunContextWrapper[BoolCtx], agent: AgentBase) -> bool:
        return ctx.context.enable_tools

    conditional_tool = agent.as_tool(
        tool_name="conditional_agent_tool",
        tool_description="A conditionally enabled agent tool",
        is_enabled=cond_enabled,
    )

    # Create tool with lambda is_enabled
    lambda_tool = agent.as_tool(
        tool_name="lambda_agent_tool",
        tool_description="A lambda enabled agent tool",
        is_enabled=lambda ctx, agent: ctx.context.enable_tools,
    )

    # Create test agent that uses these tools
    orchestrator = Agent(
        name="orchestrator",
        instructions="You orchestrate other agents.",
        tools=[conditional_tool, lambda_tool],
    )

    # Test with enable_tools=False
    context_disabled = RunContextWrapper(BoolCtx(enable_tools=False))
    tools_disabled = await orchestrator.get_all_tools(context_disabled)
    assert len(tools_disabled) == 0

    # Test with enable_tools=True
    context_enabled = RunContextWrapper(BoolCtx(enable_tools=True))
    tools_enabled = await orchestrator.get_all_tools(context_enabled)
    tool_names = [tool.name for tool in tools_enabled]

    assert len(tools_enabled) == 2
    assert "conditional_agent_tool" in tool_names
    assert "lambda_agent_tool" in tool_names


@pytest.mark.asyncio
async def test_agent_as_tool_is_enabled_mixed():
    """Test agent.as_tool() with mixed enabled/disabled tools."""
    # Create a simple agent
    agent = Agent(
        name="test_agent",
        instructions="You are a test agent that says hello.",
    )

    # Create various tools with different is_enabled configurations
    always_enabled = agent.as_tool(
        tool_name="always_enabled",
        tool_description="Always enabled tool",
        is_enabled=True,
    )

    always_disabled = agent.as_tool(
        tool_name="always_disabled",
        tool_description="Always disabled tool",
        is_enabled=False,
    )

    conditionally_enabled = agent.as_tool(
        tool_name="conditionally_enabled",
        tool_description="Conditionally enabled tool",
        is_enabled=lambda ctx, agent: ctx.context.enable_tools,
    )

    default_enabled = agent.as_tool(
        tool_name="default_enabled",
        tool_description="Default enabled tool",
    )

    # Create test agent that uses these tools
    orchestrator = Agent(
        name="orchestrator",
        instructions="You orchestrate other agents.",
        tools=[always_enabled, always_disabled, conditionally_enabled, default_enabled],
    )

    # Test with enable_tools=False
    context_disabled = RunContextWrapper(BoolCtx(enable_tools=False))
    tools_disabled = await orchestrator.get_all_tools(context_disabled)
    tool_names_disabled = [tool.name for tool in tools_disabled]

    assert len(tools_disabled) == 2
    assert "always_enabled" in tool_names_disabled
    assert "default_enabled" in tool_names_disabled
    assert "always_disabled" not in tool_names_disabled
    assert "conditionally_enabled" not in tool_names_disabled

    # Test with enable_tools=True
    context_enabled = RunContextWrapper(BoolCtx(enable_tools=True))
    tools_enabled = await orchestrator.get_all_tools(context_enabled)
    tool_names_enabled = [tool.name for tool in tools_enabled]

    assert len(tools_enabled) == 3
    assert "always_enabled" in tool_names_enabled
    assert "default_enabled" in tool_names_enabled
    assert "conditionally_enabled" in tool_names_enabled
    assert "always_disabled" not in tool_names_enabled


@pytest.mark.asyncio
async def test_agent_as_tool_is_enabled_preserves_other_params():
    """Test that is_enabled parameter doesn't interfere with other agent.as_tool() parameters."""
    # Create a simple agent
    agent = Agent(
        name="test_agent",
        instructions="You are a test agent that returns a greeting.",
    )

    # Custom output extractor
    async def custom_extractor(result):
        return f"CUSTOM: {result.new_items[-1].text if result.new_items else 'No output'}"

    # Create tool with all parameters including is_enabled
    tool = agent.as_tool(
        tool_name="custom_tool_name",
        tool_description="A custom tool with all parameters",
        custom_output_extractor=custom_extractor,
        is_enabled=True,
    )

    # Verify the tool was created with correct properties
    assert tool.name == "custom_tool_name"
    assert isinstance(tool, FunctionTool)
    assert tool.description == "A custom tool with all parameters"
    assert tool.is_enabled is True

    # Verify tool is included when enabled
    orchestrator = Agent(
        name="orchestrator",
        instructions="You orchestrate other agents.",
        tools=[tool],
    )

    context = RunContextWrapper(BoolCtx(enable_tools=True))
    tools = await orchestrator.get_all_tools(context)
    assert len(tools) == 1
    assert tools[0].name == "custom_tool_name"


@pytest.mark.asyncio
async def test_agent_as_tool_returns_final_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent tool should return final_output when no custom extractor is provided."""

    agent = Agent(name="storyteller")

    result = type(
        "DummyResult",
        (),
        {"final_output": "Hello world"},
    )()

    async def fake_run(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        conversation_id,
        session,
    ):
        assert starting_agent is agent
        assert input == "hello"
        return result

    monkeypatch.setattr(Runner, "run", classmethod(fake_run))

    tool = agent.as_tool(
        tool_name="story_tool",
        tool_description="Tell a short story",
        is_enabled=True,
    )

    assert isinstance(tool, FunctionTool)
    tool_context = ToolContext(
        context=None,
        tool_name="story_tool",
        tool_call_id="call_1",
        tool_arguments='{"input": "hello"}',
    )
    output = await tool.on_invoke_tool(tool_context, '{"input": "hello"}')

    assert output == "Hello world"


@pytest.mark.asyncio
async def test_agent_as_tool_custom_output_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Custom output extractors should receive the RunResult from Runner.run."""

    agent = Agent(name="summarizer")

    message = ResponseOutputMessage(
        id="msg_2",
        role="assistant",
        status="completed",
        type="message",
        content=[
            ResponseOutputText(
                annotations=[],
                text="Original text",
                type="output_text",
                logprobs=[],
            )
        ],
    )

    class DummySession(Session):
        session_id = "sess_123"

        async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
            return []

        async def add_items(self, items: list[TResponseInputItem]) -> None:
            return None

        async def pop_item(self) -> TResponseInputItem | None:
            return None

        async def clear_session(self) -> None:
            return None

    dummy_session = DummySession()

    class DummyResult:
        def __init__(self, items: list[MessageOutputItem]) -> None:
            self.new_items = items

    run_result = DummyResult([MessageOutputItem(agent=agent, raw_item=message)])

    async def fake_run(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        conversation_id,
        session,
    ):
        assert starting_agent is agent
        assert input == "summarize this"
        assert isinstance(context, ToolContext)
        assert context.tool_call_id == "call_2"
        assert context.tool_name == "summary_tool"
        assert max_turns == 7
        assert hooks is hooks_obj
        assert run_config is run_config_obj
        assert previous_response_id == "resp_1"
        assert conversation_id == "conv_1"
        assert session is dummy_session
        return run_result

    monkeypatch.setattr(Runner, "run", classmethod(fake_run))

    async def extractor(result) -> str:
        assert result is run_result
        return "custom output"

    hooks_obj = RunHooks[Any]()
    run_config_obj = RunConfig(model="gpt-4.1-mini")

    tool = agent.as_tool(
        tool_name="summary_tool",
        tool_description="Summarize input",
        custom_output_extractor=extractor,
        is_enabled=True,
        run_config=run_config_obj,
        max_turns=7,
        hooks=hooks_obj,
        previous_response_id="resp_1",
        conversation_id="conv_1",
        session=dummy_session,
    )

    assert isinstance(tool, FunctionTool)
    tool_context = ToolContext(
        context=None,
        tool_name="summary_tool",
        tool_call_id="call_2",
        tool_arguments='{"input": "summarize this"}',
    )
    output = await tool.on_invoke_tool(tool_context, '{"input": "summarize this"}')

    assert output == "custom output"


@pytest.mark.asyncio
async def test_agent_as_tool_streams_events_with_on_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(name="streamer")
    stream_events = [
        RawResponsesStreamEvent(data=cast(Any, {"type": "response_started"})),
        RawResponsesStreamEvent(data=cast(Any, {"type": "output_text_delta", "delta": "hi"})),
    ]

    class DummyStreamingResult:
        def __init__(self) -> None:
            self.final_output = "streamed output"
            self.current_agent = agent

        async def stream_events(self):
            for ev in stream_events:
                yield ev

    run_calls: list[dict[str, Any]] = []

    def fake_run_streamed(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        auto_previous_response_id=False,
        conversation_id,
        session,
    ):
        run_calls.append(
            {
                "starting_agent": starting_agent,
                "input": input,
                "context": context,
                "max_turns": max_turns,
                "hooks": hooks,
                "run_config": run_config,
                "previous_response_id": previous_response_id,
                "conversation_id": conversation_id,
                "session": session,
            }
        )
        return DummyStreamingResult()

    async def unexpected_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Runner.run should not be called when on_stream is provided.")

    monkeypatch.setattr(Runner, "run_streamed", classmethod(fake_run_streamed))
    monkeypatch.setattr(Runner, "run", classmethod(unexpected_run))

    received_events: list[AgentToolStreamEvent] = []

    async def on_stream(payload: AgentToolStreamEvent) -> None:
        received_events.append(payload)

    tool_call = ResponseFunctionToolCall(
        id="call_123",
        arguments='{"input": "run streaming"}',
        call_id="call-123",
        name="stream_tool",
        type="function_call",
    )

    tool = cast(
        FunctionTool,
        agent.as_tool(
            tool_name="stream_tool",
            tool_description="Streams events",
            on_stream=on_stream,
        ),
    )

    tool_context = ToolContext(
        context=None,
        tool_name="stream_tool",
        tool_call_id=tool_call.call_id,
        tool_arguments=tool_call.arguments,
        tool_call=tool_call,
    )
    output = await tool.on_invoke_tool(tool_context, '{"input": "run streaming"}')

    assert output == "streamed output"
    assert len(received_events) == len(stream_events)
    assert received_events[0]["agent"] is agent
    assert received_events[0]["tool_call"] is tool_call
    assert received_events[0]["event"] == stream_events[0]
    assert run_calls[0]["input"] == "run streaming"


@pytest.mark.asyncio
async def test_agent_as_tool_streaming_updates_agent_on_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_agent = Agent(name="primary")
    handed_off_agent = Agent(name="delegate")

    events = [
        AgentUpdatedStreamEvent(new_agent=first_agent),
        RawResponsesStreamEvent(data=cast(Any, {"type": "response_started"})),
        AgentUpdatedStreamEvent(new_agent=handed_off_agent),
        RawResponsesStreamEvent(data=cast(Any, {"type": "output_text_delta", "delta": "hello"})),
    ]

    class DummyStreamingResult:
        def __init__(self) -> None:
            self.final_output = "delegated output"
            self.current_agent = first_agent

        async def stream_events(self):
            for ev in events:
                yield ev

    def fake_run_streamed(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        auto_previous_response_id=False,
        conversation_id,
        session,
    ):
        return DummyStreamingResult()

    monkeypatch.setattr(Runner, "run_streamed", classmethod(fake_run_streamed))
    monkeypatch.setattr(
        Runner,
        "run",
        classmethod(lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no run"))),
    )

    seen_agents: list[Agent[Any]] = []

    async def on_stream(payload: AgentToolStreamEvent) -> None:
        seen_agents.append(payload["agent"])

    tool = cast(
        FunctionTool,
        first_agent.as_tool(
            tool_name="delegate_tool",
            tool_description="Streams handoff events",
            on_stream=on_stream,
        ),
    )

    tool_call = ResponseFunctionToolCall(
        id="call_delegate",
        arguments='{"input": "handoff"}',
        call_id="call-delegate",
        name="delegate_tool",
        type="function_call",
    )
    tool_context = ToolContext(
        context=None,
        tool_name="delegate_tool",
        tool_call_id=tool_call.call_id,
        tool_arguments=tool_call.arguments,
        tool_call=tool_call,
    )

    output = await tool.on_invoke_tool(tool_context, '{"input": "handoff"}')

    assert output == "delegated output"
    assert seen_agents == [first_agent, first_agent, handed_off_agent, handed_off_agent]


@pytest.mark.asyncio
async def test_agent_as_tool_streaming_works_with_custom_extractor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(name="streamer")
    stream_events = [RawResponsesStreamEvent(data=cast(Any, {"type": "response_started"}))]
    stream_events = [RawResponsesStreamEvent(data=cast(Any, {"type": "response_started"}))]

    class DummyStreamingResult:
        def __init__(self) -> None:
            self.final_output = "raw output"
            self.current_agent = agent

        async def stream_events(self):
            for ev in stream_events:
                yield ev

    streamed_instance = DummyStreamingResult()

    def fake_run_streamed(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        auto_previous_response_id=False,
        conversation_id,
        session,
    ):
        return streamed_instance

    async def unexpected_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Runner.run should not be called when on_stream is provided.")

    monkeypatch.setattr(Runner, "run_streamed", classmethod(fake_run_streamed))
    monkeypatch.setattr(Runner, "run", classmethod(unexpected_run))

    received: list[Any] = []

    async def extractor(result) -> str:
        received.append(result)
        return "custom value"

    callbacks: list[Any] = []

    async def on_stream(payload: AgentToolStreamEvent) -> None:
        callbacks.append(payload["event"])

    tool_call = ResponseFunctionToolCall(
        id="call_abc",
        arguments='{"input": "stream please"}',
        call_id="call-abc",
        name="stream_tool",
        type="function_call",
    )

    tool = cast(
        FunctionTool,
        agent.as_tool(
            tool_name="stream_tool",
            tool_description="Streams events",
            custom_output_extractor=extractor,
            on_stream=on_stream,
        ),
    )

    tool_context = ToolContext(
        context=None,
        tool_name="stream_tool",
        tool_call_id=tool_call.call_id,
        tool_arguments=tool_call.arguments,
        tool_call=tool_call,
    )
    output = await tool.on_invoke_tool(tool_context, '{"input": "stream please"}')

    assert output == "custom value"
    assert received == [streamed_instance]
    assert callbacks == stream_events


@pytest.mark.asyncio
async def test_agent_as_tool_streaming_accepts_sync_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(name="sync_handler_agent")

    class DummyStreamingResult:
        def __init__(self) -> None:
            self.final_output = "ok"
            self.current_agent = agent

        async def stream_events(self):
            yield RawResponsesStreamEvent(data=cast(Any, {"type": "response_started"}))

    monkeypatch.setattr(
        Runner, "run_streamed", classmethod(lambda *args, **kwargs: DummyStreamingResult())
    )
    monkeypatch.setattr(
        Runner,
        "run",
        classmethod(lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no run"))),
    )

    calls: list[str] = []

    def sync_handler(event: AgentToolStreamEvent) -> None:
        calls.append(event["event"].type)

    tool_call = ResponseFunctionToolCall(
        id="call_sync",
        arguments='{"input": "go"}',
        call_id="call-sync",
        name="sync_tool",
        type="function_call",
    )

    tool = cast(
        FunctionTool,
        agent.as_tool(
            tool_name="sync_tool",
            tool_description="Uses sync handler",
            on_stream=sync_handler,
        ),
    )
    tool_context = ToolContext(
        context=None,
        tool_name="sync_tool",
        tool_call_id=tool_call.call_id,
        tool_arguments=tool_call.arguments,
        tool_call=tool_call,
    )

    output = await tool.on_invoke_tool(tool_context, '{"input": "go"}')

    assert output == "ok"
    assert calls == ["raw_response_event"]


@pytest.mark.asyncio
async def test_agent_as_tool_streaming_dispatches_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """on_stream handlers should not block streaming iteration."""
    agent = Agent(name="nonblocking_agent")

    first_handler_started = asyncio.Event()
    allow_handler_to_continue = asyncio.Event()
    second_event_yielded = asyncio.Event()
    second_event_handled = asyncio.Event()

    first_event = RawResponsesStreamEvent(data=cast(Any, {"type": "response_started"}))
    second_event = RawResponsesStreamEvent(
        data=cast(Any, {"type": "output_text_delta", "delta": "hi"})
    )

    class DummyStreamingResult:
        def __init__(self) -> None:
            self.final_output = "ok"
            self.current_agent = agent

        async def stream_events(self):
            yield first_event
            second_event_yielded.set()
            yield second_event

    dummy_result = DummyStreamingResult()

    monkeypatch.setattr(Runner, "run_streamed", classmethod(lambda *args, **kwargs: dummy_result))
    monkeypatch.setattr(
        Runner,
        "run",
        classmethod(lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no run"))),
    )

    async def on_stream(payload: AgentToolStreamEvent) -> None:
        if payload["event"] is first_event:
            first_handler_started.set()
            await allow_handler_to_continue.wait()
        else:
            second_event_handled.set()

    tool_call = ResponseFunctionToolCall(
        id="call_nonblocking",
        arguments='{"input": "go"}',
        call_id="call-nonblocking",
        name="nonblocking_tool",
        type="function_call",
    )

    tool = cast(
        FunctionTool,
        agent.as_tool(
            tool_name="nonblocking_tool",
            tool_description="Uses non-blocking streaming handler",
            on_stream=on_stream,
        ),
    )
    tool_context = ToolContext(
        context=None,
        tool_name="nonblocking_tool",
        tool_call_id=tool_call.call_id,
        tool_arguments=tool_call.arguments,
        tool_call=tool_call,
    )

    async def _invoke_tool() -> Any:
        return await tool.on_invoke_tool(tool_context, '{"input": "go"}')

    invoke_task: asyncio.Task[Any] = asyncio.create_task(_invoke_tool())

    await asyncio.wait_for(first_handler_started.wait(), timeout=1.0)
    await asyncio.wait_for(second_event_yielded.wait(), timeout=1.0)
    assert invoke_task.done() is False

    allow_handler_to_continue.set()
    await asyncio.wait_for(second_event_handled.wait(), timeout=1.0)
    output = await asyncio.wait_for(invoke_task, timeout=1.0)

    assert output == "ok"


@pytest.mark.asyncio
async def test_agent_as_tool_streaming_handler_exception_does_not_fail_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(name="handler_error_agent")

    class DummyStreamingResult:
        def __init__(self) -> None:
            self.final_output = "ok"
            self.current_agent = agent

        async def stream_events(self):
            yield RawResponsesStreamEvent(data=cast(Any, {"type": "response_started"}))

    monkeypatch.setattr(
        Runner, "run_streamed", classmethod(lambda *args, **kwargs: DummyStreamingResult())
    )
    monkeypatch.setattr(
        Runner,
        "run",
        classmethod(lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no run"))),
    )

    def bad_handler(event: AgentToolStreamEvent) -> None:
        raise RuntimeError("boom")

    tool_call = ResponseFunctionToolCall(
        id="call_bad",
        arguments='{"input": "go"}',
        call_id="call-bad",
        name="error_tool",
        type="function_call",
    )

    tool = cast(
        FunctionTool,
        agent.as_tool(
            tool_name="error_tool",
            tool_description="Handler throws",
            on_stream=bad_handler,
        ),
    )
    tool_context = ToolContext(
        context=None,
        tool_name="error_tool",
        tool_call_id=tool_call.call_id,
        tool_arguments=tool_call.arguments,
        tool_call=tool_call,
    )

    output = await tool.on_invoke_tool(tool_context, '{"input": "go"}')

    assert output == "ok"


@pytest.mark.asyncio
async def test_agent_as_tool_without_stream_uses_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(name="nostream_agent")

    class DummyResult:
        def __init__(self) -> None:
            self.final_output = "plain"

    run_calls: list[dict[str, Any]] = []

    async def fake_run(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        auto_previous_response_id=False,
        conversation_id,
        session,
    ):
        run_calls.append({"input": input})
        return DummyResult()

    monkeypatch.setattr(Runner, "run", classmethod(fake_run))
    monkeypatch.setattr(
        Runner,
        "run_streamed",
        classmethod(lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no stream"))),
    )

    tool = cast(
        FunctionTool,
        agent.as_tool(
            tool_name="nostream_tool",
            tool_description="No streaming path",
        ),
    )
    tool_context = ToolContext(
        context=None,
        tool_name="nostream_tool",
        tool_call_id="call-no",
        tool_arguments='{"input": "plain"}',
    )

    output = await tool.on_invoke_tool(tool_context, '{"input": "plain"}')

    assert output == "plain"
    assert run_calls == [{"input": "plain"}]


@pytest.mark.asyncio
async def test_agent_as_tool_streaming_sets_tool_call_from_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = Agent(name="direct_invocation_agent")

    class DummyStreamingResult:
        def __init__(self) -> None:
            self.final_output = "ok"
            self.current_agent = agent

        async def stream_events(self):
            yield RawResponsesStreamEvent(data=cast(Any, {"type": "response_started"}))

    monkeypatch.setattr(
        Runner, "run_streamed", classmethod(lambda *args, **kwargs: DummyStreamingResult())
    )
    monkeypatch.setattr(
        Runner,
        "run",
        classmethod(lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no run"))),
    )

    captured: list[AgentToolStreamEvent] = []

    async def on_stream(event: AgentToolStreamEvent) -> None:
        captured.append(event)

    tool_call = ResponseFunctionToolCall(
        id="call_direct",
        arguments='{"input": "hi"}',
        call_id="direct-call-id",
        name="direct_stream_tool",
        type="function_call",
    )

    tool = cast(
        FunctionTool,
        agent.as_tool(
            tool_name="direct_stream_tool",
            tool_description="Direct invocation",
            on_stream=on_stream,
        ),
    )
    tool_context = ToolContext(
        context=None,
        tool_name="direct_stream_tool",
        tool_call_id=tool_call.call_id,
        tool_arguments=tool_call.arguments,
        tool_call=tool_call,
    )

    output = await tool.on_invoke_tool(tool_context, '{"input": "hi"}')

    assert output == "ok"
    assert captured[0]["tool_call"] is tool_call


@pytest.mark.asyncio
async def test_agent_as_tool_failure_error_function_none_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If failure_error_function=None, exceptions should propagate to the caller."""
    agent = Agent(name="failing_agent")

    async def fake_run(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        conversation_id,
        session,
    ):
        assert starting_agent is agent
        assert input == "hello"
        raise RuntimeError("test failure")

    monkeypatch.setattr(Runner, "run", classmethod(fake_run))

    tool = agent.as_tool(
        tool_name="failing_agent_tool",
        tool_description="Agent tool that raises",
        is_enabled=True,
        failure_error_function=None,
    )

    assert isinstance(tool, FunctionTool)

    tool_context = ToolContext(
        context=None,
        tool_name="failing_agent_tool",
        tool_call_id="call_1",
        tool_arguments='{"input": "hello"}',
    )

    with pytest.raises(RuntimeError, match="test failure"):
        await tool.on_invoke_tool(tool_context, '{"input": "hello"}')


@pytest.mark.asyncio
async def test_agent_as_tool_failure_error_function_custom_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom failure_error_function should be used to convert exceptions into tool output."""
    agent = Agent(name="failing_agent")

    async def fake_run(
        cls,
        starting_agent,
        input,
        *,
        context,
        max_turns,
        hooks,
        run_config,
        previous_response_id,
        conversation_id,
        session,
    ):
        assert starting_agent is agent
        assert input == "hello"
        raise ValueError("test failure")

    monkeypatch.setattr(Runner, "run", classmethod(fake_run))

    def custom_failure_handler(ctx: RunContextWrapper[Any], error: Exception) -> str:
        return f"handled:{type(error).__name__}:{error}"

    tool = agent.as_tool(
        tool_name="failing_agent_tool",
        tool_description="Agent tool that raises",
        is_enabled=True,
        failure_error_function=custom_failure_handler,
    )

    assert isinstance(tool, FunctionTool)

    tool_context = ToolContext(
        context=None,
        tool_name="failing_agent_tool",
        tool_call_id="call_1",
        tool_arguments='{"input": "hello"}',
    )

    result = await tool.on_invoke_tool(tool_context, '{"input": "hello"}')
    assert result == "handled:ValueError:test failure"
