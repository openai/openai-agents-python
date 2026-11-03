"""Exercise callback deadlines through native admission and session ownership."""

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, model_serializer

from agents import Agent, Allow, ToolContext, ToolOutput, TurnStoppedError, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    core_owner as core_owner,
    function_call,
    responses as responses,
)

from .test_native_agent import Application, provider


@pytest.mark.parametrize("duration", [0, -1, float("inf"), float("nan"), 10**1000, True, "1"])
def test_callback_deadline_rejects_invalid_duration(duration):
    async def callback() -> str:
        return "unused"

    with pytest.raises(ValueError, match="timeout"):
        function_tool(callback_timeout=duration)(callback)


async def test_callback_completes_with_context_before_its_deadline(responses):
    application = Application()
    hooks = []

    async def before(context: ToolContext[Application]) -> Allow:
        await asyncio.sleep(0.3)
        hooks.append("before")
        return Allow()

    async def after(context: ToolContext[Application], output: ToolOutput) -> ToolOutput:
        await asyncio.sleep(0.3)
        hooks.append("after")
        return output

    @function_tool(callback_timeout=0.25, before=before, after=after)
    async def lookup(context: ToolContext[Application], key: str) -> str:
        application.calls.append(context)
        return f"{context.context.tenant}:{key}"

    responses.respond(function_call("lookup", {"key": "record-a"}, "deadline-fast"))
    responses.respond(assistant_output("completed"))
    agent = Agent(instructions="Read the record.", tools=[lookup], provider=provider(responses))
    async with agent.session(context=application) as session:
        handle = await session.submit("read")
        assert (await (handle).get_result()).final_output == "completed"
        assert application.calls[0].submission is handle
        assert application.calls[0].context is application
        assert "tenant-a:record-a" in str(responses.requests[1]["input"])
        # Hooks exceed the body deadline independently and still complete.
        assert hooks == ["before", "after"]


@pytest.mark.parametrize("swallow_cancellation", [False, True])
@pytest.mark.usefixtures("core_owner")
async def test_callback_deadline_joins_cleanup_rejects_late_value_and_allows_next_turn(
    responses, swallow_cancellation
):
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleaned = asyncio.Event()
    effects = []
    serialized = []

    class LateValue(BaseModel):
        @model_serializer
        def serialize(self) -> str:
            serialized.append("late")
            return "late-callback-value"

    @function_tool(callback_timeout=0.01)
    async def pending() -> object:
        effects.append("application-effect")
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            if swallow_cancellation:
                return LateValue()
            raise
        finally:
            cleanup_started.set()
            await cleanup_release.wait()
            cleaned.set()

    responses.respond(function_call("pending", {}, "deadline-pending"))
    responses.respond(assistant_output("recovered"))
    agent = Agent(
        instructions="Use the selected tool.", tools=[pending], provider=provider(responses)
    )
    async with agent.session() as session:
        home = session._home.name
        reader = session._reader
        handle = await session.submit("first")
        waiter = asyncio.create_task((handle).get_result())
        try:
            async with timeout(10):
                await cleanup_started.wait()
            assert not waiter.done()
            assert effects == ["application-effect"]
            assert serialized == []
        finally:
            cleanup_release.set()
        async with timeout(10):
            with pytest.raises(TimeoutError):
                await waiter
        assert cleaned.is_set()
        assert serialized == []
        assert "late-callback-value" not in str([entry async for entry in session.items()])
        following = await session.submit("next")
        assert (await (following).get_result()).final_output == "recovered"
        assert following.turn_id != handle.turn_id
        assert len(responses.requests) == 2
    assert reader.done()
    assert not Path(home).exists()


async def test_callback_crossing_cutoff_before_timer_dispatch_rejects_its_value(responses):
    serialized = []

    class LateValue(BaseModel):
        @model_serializer
        def serialize(self) -> str:
            serialized.append("late")
            return "late-busy-callback-value"

    @function_tool(callback_timeout=0.001)
    async def busy() -> object:
        # Hold the event loop past the deadline so its timer cannot run first.
        time.sleep(0.02)
        return LateValue()

    responses.respond(function_call("busy", {}, "deadline-busy"))
    responses.respond(assistant_output("recovered"))
    agent = Agent(instructions="Use the tool.", tools=[busy], provider=provider(responses))
    async with timeout(10), agent.session() as session:
        handle = await session.submit("first")
        with pytest.raises(TimeoutError, match="callback timeout"):
            await (handle).get_result()
        assert serialized == []
        assert "late-busy-callback-value" not in str([entry async for entry in session.items()])
        following = await session.submit("next")
        assert (await (following).get_result()).final_output == "recovered"


@pytest.mark.parametrize("close", [False, True])
@pytest.mark.usefixtures("core_owner")
async def test_callback_deadline_stop_joins_interrupt_and_cleanup_despite_caller_cancellation(
    responses, monkeypatch, close
):
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleaned = asyncio.Event()
    interrupt_started = asyncio.Event()
    interrupt_release = asyncio.Event()

    @function_tool(callback_timeout=0.01)
    async def pending() -> str:
        try:
            await asyncio.Future()
        finally:
            cleanup_started.set()
            await cleanup_release.wait()
            cleaned.set()
        return "unreachable"

    responses.respond(function_call("pending", {}, "deadline-stop"))
    responses.respond(assistant_output("recovered"))
    agent = Agent(instructions="Use the tool.", tools=[pending], provider=provider(responses))
    async with timeout(15), agent.session() as session:
        original = session._native

        class HoldInterrupt:
            def __getattr__(self, name: str) -> Any:
                return getattr(original, name)

            async def _interrupt_session(self, expected_turn_id) -> Any:
                interrupt_started.set()
                await interrupt_release.wait()
                return await original._interrupt_session(expected_turn_id)

        monkeypatch.setattr(session, "_native", HoldInterrupt())
        home = Path(session._home.name)
        reader = session._reader
        handle = await session.submit("first")
        waiter = asyncio.create_task((handle).get_result())
        stopping = None
        try:
            await cleanup_started.wait()
            await interrupt_started.wait()
            stopping = asyncio.create_task(session._aclose() if close else session._cancel(handle))
            await asyncio.sleep(0)
            stopping.cancel("first caller cancellation")
            await asyncio.sleep(0)
            stopping.cancel("second caller cancellation")
            await asyncio.sleep(0)
            assert not stopping.done()
            assert not waiter.done()
            assert not cleaned.is_set()
            interrupt_release.set()
            await asyncio.sleep(0)
            assert not stopping.done()
            assert not cleaned.is_set()
            cleanup_release.set()
            with pytest.raises(asyncio.CancelledError):
                await stopping
            with pytest.raises(TimeoutError, match="callback timeout"):
                await waiter
            assert cleaned.is_set()
            if not close:
                following = await session.submit("next")
                assert (await (following).get_result()).final_output == "recovered"
        finally:
            interrupt_release.set()
            cleanup_release.set()
            await asyncio.gather(
                waiter, *(() if stopping is None else (stopping,)), return_exceptions=True
            )
    assert reader.done()
    assert not home.exists()


async def test_close_started_before_deadline_keeps_close_as_the_stop_owner(responses, monkeypatch):
    entered = asyncio.Event()
    response_release = asyncio.Event()
    closer_armed = asyncio.Event()
    close_started = asyncio.Event()
    close_release = asyncio.Event()
    cleanup_started = asyncio.Event()

    @function_tool(callback_timeout=0.03)
    async def pending() -> str:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleanup_started.set()
        return "unreachable"

    async def hold_tool_response(event: dict[str, Any]) -> None:
        if event["type"] == "response.output_item.done":
            await response_release.wait()

    responses.before_event = hold_tool_response
    responses.respond(function_call("pending", {}, "close-before-deadline"))
    agent = Agent(instructions="Use the tool.", tools=[pending], provider=provider(responses))
    async with timeout(10), agent.session() as session:
        original_close = session._close

        async def hold_close() -> None:
            close_started.set()
            await close_release.wait()
            await original_close()

        async def close_after_entry() -> None:
            closer_armed.set()
            await entered.wait()
            await session._aclose()

        monkeypatch.setattr(session, "_close", hold_close)
        closing = None
        try:
            handle = await session.submit("first")
            # Wake the armed closer before the callback's timer can dispatch.
            closing = asyncio.create_task(close_after_entry())
            await closer_armed.wait()
            response_release.set()
            await close_started.wait()
            # aclose has claimed ownership, but its controller is held before it
            # can set the submission's stopping flag or cancel the callback.
            await asyncio.sleep(0.06)
            assert not cleanup_started.is_set()
            assert not closing.done()
            close_release.set()
            await closing
            with pytest.raises(TurnStoppedError):
                await (handle).get_result()
            assert cleanup_started.is_set()
        finally:
            response_release.set()
            close_release.set()
            if closing is not None:
                if not entered.is_set():
                    closing.cancel()
                await asyncio.gather(closing, return_exceptions=True)


async def test_callback_deadline_retains_original_cleanup_failure(responses):
    cleanup_error = OSError("deadline cleanup failed")

    @function_tool(callback_timeout=0.01)
    async def pending() -> str:
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            raise cleanup_error

    responses.respond(function_call("pending", {}, "deadline-cleanup-error"))
    agent = Agent(instructions="Use the tool.", tools=[pending], provider=provider(responses))
    async with timeout(10), agent.session() as session:
        handle = await session.submit("first")
        with pytest.raises(TimeoutError, match="callback timeout") as caught:
            await (handle).get_result()
        pending_errors: list[BaseException] = [caught.value]
        seen: set[int] = set()
        while pending_errors:
            error = pending_errors.pop()
            if id(error) in seen:
                continue
            seen.add(id(error))
            pending_errors.extend(
                linked for linked in (error.__cause__, error.__context__) if linked is not None
            )
        assert id(cleanup_error) in seen
