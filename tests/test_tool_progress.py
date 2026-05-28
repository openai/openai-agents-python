"""Tests for ToolContext.send_progress and ToolProgressStreamEvent."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agents import Agent, Runner
from agents.run_context import RunContextWrapper, _StreamContext
from agents.stream_events import ToolProgressStreamEvent
from agents.tool import function_tool
from agents.tool_context import ToolContext

from .fake_model import FakeModel
from .test_responses import get_function_tool_call, get_text_message


def _make_stream_context(
    queue: asyncio.Queue[Any], loop: asyncio.AbstractEventLoop
) -> _StreamContext:
    return _StreamContext(event_queue=queue, event_loop=loop)


def _make_tool_context(
    *,
    stream_context: _StreamContext | None = None,
    tool_name: str = "test_tool",
    tool_call_id: str = "call-1",
) -> ToolContext[None]:
    ctx: ToolContext[None] = ToolContext(
        context=None,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        tool_arguments="{}",
        _stream_context=stream_context,
    )
    return ctx


class TestSendProgress:
    def test_send_progress_with_queue(self) -> None:
        """send_progress pushes a ToolProgressStreamEvent to the queue."""
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._send_and_verify(loop))
        finally:
            loop.close()

    async def _send_and_verify(self, loop: asyncio.AbstractEventLoop) -> None:
        queue: asyncio.Queue[Any] = asyncio.Queue()
        sc = _make_stream_context(queue, loop)
        ctx = _make_tool_context(stream_context=sc)
        ctx.send_progress({"status": "working", "progress": 0.5})

        # call_soon_threadsafe schedules on the loop, so we need to let it run.
        await asyncio.sleep(0)

        assert not queue.empty()
        event = queue.get_nowait()
        assert isinstance(event, ToolProgressStreamEvent)
        assert event.tool_name == "test_tool"
        assert event.tool_call_id == "call-1"
        assert event.data == {"status": "working", "progress": 0.5}
        assert event.type == "tool_progress_stream_event"

    def test_send_progress_without_stream_context_is_noop(self) -> None:
        """send_progress does nothing when _stream_context is None."""
        ctx = _make_tool_context(stream_context=None)
        ctx.send_progress({"status": "working"})

    def test_send_progress_failure_does_not_raise(self) -> None:
        """If the queue operation fails, send_progress logs but doesn't crash."""
        bad_sc = _StreamContext(event_queue="not_a_queue", event_loop="not_a_loop")
        ctx = _make_tool_context(stream_context=bad_sc)
        ctx.send_progress({"status": "working"})

    def test_multiple_progress_events(self) -> None:
        """Multiple send_progress calls all arrive in order."""
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._send_multiple(loop))
        finally:
            loop.close()

    async def _send_multiple(self, loop: asyncio.AbstractEventLoop) -> None:
        queue: asyncio.Queue[Any] = asyncio.Queue()
        sc = _make_stream_context(queue, loop)
        ctx = _make_tool_context(stream_context=sc)
        ctx.send_progress({"step": 1})
        ctx.send_progress({"step": 2})
        ctx.send_progress({"step": 3})
        await asyncio.sleep(0)

        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        assert len(events) == 3
        assert events[0].data == {"step": 1}
        assert events[1].data == {"step": 2}
        assert events[2].data == {"step": 3}


class TestToolProgressStreamEvent:
    def test_fields(self) -> None:
        event = ToolProgressStreamEvent(
            tool_name="my_tool",
            tool_call_id="call-123",
            data={"progress": 0.75},
        )
        assert event.tool_name == "my_tool"
        assert event.tool_call_id == "call-123"
        assert event.data == {"progress": 0.75}
        assert event.type == "tool_progress_stream_event"

    def test_data_accepts_any_type(self) -> None:
        event_str = ToolProgressStreamEvent(tool_name="t", tool_call_id="c", data="hello")
        assert event_str.data == "hello"

        event_num = ToolProgressStreamEvent(tool_name="t", tool_call_id="c", data=42)
        assert event_num.data == 42

        event_list = ToolProgressStreamEvent(tool_name="t", tool_call_id="c", data=[1, 2, 3])
        assert event_list.data == [1, 2, 3]


class TestStreamContextPropagation:
    def test_fork_with_tool_input_propagates_stream_context(self) -> None:
        """_fork_with_tool_input preserves _stream_context."""
        sc = _StreamContext(event_queue=object(), event_loop=object())
        wrapper: RunContextWrapper[None] = RunContextWrapper(context=None)
        wrapper._stream_context = sc

        forked = wrapper._fork_with_tool_input(tool_input="test")
        assert forked._stream_context is sc

    def test_fork_without_tool_input_propagates_stream_context(self) -> None:
        """_fork_without_tool_input preserves _stream_context."""
        sc = _StreamContext(event_queue=object(), event_loop=object())
        wrapper: RunContextWrapper[None] = RunContextWrapper(context=None)
        wrapper._stream_context = sc

        forked = wrapper._fork_without_tool_input()
        assert forked._stream_context is sc

    def test_tool_context_from_agent_context_propagates_stream_context(self) -> None:
        """ToolContext.from_agent_context copies _stream_context from parent."""
        sc = _StreamContext(event_queue=object(), event_loop=object())
        wrapper: RunContextWrapper[None] = RunContextWrapper(context=None)
        wrapper._stream_context = sc

        tool_call = ToolContext.from_agent_context(
            wrapper,
            tool_call_id="call-1",
            tool_name="test",
            tool_arguments="{}",
        )
        assert tool_call._stream_context is sc


class TestStreamingIntegration:
    @pytest.mark.asyncio
    async def test_progress_events_in_streamed_run(self) -> None:
        """Integration test: send_progress events appear in stream_events()."""

        async def _progress_fn(ctx: ToolContext) -> str:
            ctx.send_progress({"status": "starting"})
            ctx.send_progress({"status": "done"})
            return "result"

        tool = function_tool(_progress_fn, name_override="progress_tool")
        model = FakeModel()
        agent = Agent(name="test", model=model, tools=[tool])

        model.add_multiple_turn_outputs(
            [
                [get_function_tool_call("progress_tool", "{}")],
                [get_text_message("final answer")],
            ]
        )

        result = Runner.run_streamed(agent, input="test")
        progress_events: list[ToolProgressStreamEvent] = []
        async for event in result.stream_events():
            if isinstance(event, ToolProgressStreamEvent):
                progress_events.append(event)

        assert len(progress_events) == 2
        assert progress_events[0].data == {"status": "starting"}
        assert progress_events[0].tool_name == "progress_tool"
        assert progress_events[1].data == {"status": "done"}

    @pytest.mark.asyncio
    async def test_progress_noop_in_non_streamed_run(self) -> None:
        """send_progress is a no-op in non-streaming Runner.run()."""

        calls: list[dict[str, Any]] = []

        async def _progress_fn(ctx: ToolContext) -> str:
            ctx.send_progress({"status": "working"})
            calls.append({"tool": "progress_tool"})
            return "result"

        tool = function_tool(_progress_fn, name_override="progress_tool")
        model = FakeModel()
        agent = Agent(name="test", model=model, tools=[tool])

        model.add_multiple_turn_outputs(
            [
                [get_function_tool_call("progress_tool", "{}")],
                [get_text_message("done")],
            ]
        )

        result = await Runner.run(agent, input="test")
        assert len(calls) == 1
        assert result.final_output == "done"

    @pytest.mark.asyncio
    async def test_parallel_tools_with_progress(self) -> None:
        """Two concurrent tools emitting progress; events arrive with correct tool_call_id."""

        async def _tool_a(ctx: ToolContext) -> str:
            ctx.send_progress({"tool": "a", "step": 1})
            ctx.send_progress({"tool": "a", "step": 2})
            return "a_done"

        async def _tool_b(ctx: ToolContext) -> str:
            ctx.send_progress({"tool": "b", "step": 1})
            return "b_done"

        tool_a = function_tool(_tool_a, name_override="tool_a")
        tool_b = function_tool(_tool_b, name_override="tool_b")
        model = FakeModel()
        agent = Agent(name="test", model=model, tools=[tool_a, tool_b])

        model.add_multiple_turn_outputs(
            [
                [
                    get_function_tool_call("tool_a", "{}", call_id="call_a"),
                    get_function_tool_call("tool_b", "{}", call_id="call_b"),
                ],
                [get_text_message("final")],
            ]
        )

        result = Runner.run_streamed(agent, input="test")
        progress_events: list[ToolProgressStreamEvent] = []
        async for event in result.stream_events():
            if isinstance(event, ToolProgressStreamEvent):
                progress_events.append(event)

        a_events = [e for e in progress_events if e.tool_name == "tool_a"]
        b_events = [e for e in progress_events if e.tool_name == "tool_b"]
        assert len(a_events) == 2
        assert len(b_events) == 1
        assert a_events[0].tool_call_id == "call_a"
        assert b_events[0].tool_call_id == "call_b"

    @pytest.mark.asyncio
    async def test_progress_events_arrive_before_tool_output(self) -> None:
        """Progress events appear before the tool_output event for the same tool."""
        from agents.stream_events import RunItemStreamEvent

        async def _progress_fn(ctx: ToolContext) -> str:
            ctx.send_progress({"status": "working"})
            return "result"

        tool = function_tool(_progress_fn, name_override="progress_tool")
        model = FakeModel()
        agent = Agent(name="test", model=model, tools=[tool])

        model.add_multiple_turn_outputs(
            [
                [get_function_tool_call("progress_tool", "{}")],
                [get_text_message("done")],
            ]
        )

        result = Runner.run_streamed(agent, input="test")
        event_types: list[str] = []
        async for event in result.stream_events():
            if isinstance(event, ToolProgressStreamEvent):
                event_types.append("progress")
            elif isinstance(event, RunItemStreamEvent) and event.name == "tool_output":
                event_types.append("tool_output")

        assert event_types.index("progress") < event_types.index("tool_output")
