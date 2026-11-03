"""Synchronous callbacks retain ownership until their actual worker returns."""

import asyncio
import threading
from collections.abc import Awaitable
from contextvars import ContextVar

import pytest
from pydantic import BaseModel, model_serializer

from agents import Agent, ToolContext, TurnStoppedError, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import Application, provider


async def test_sync_tool_preserves_contextvars_and_native_call_identity(responses):
    application = Application()
    trace = ContextVar("native-test-trace", default="missing")
    token = trace.set("request-context")
    loop_thread = threading.get_ident()
    calls = []

    @function_tool
    def lookup(ctx: ToolContext[Application], key: str) -> str:
        calls.append((ctx, trace.get(), threading.get_ident()))
        return ctx.context.tenant + ":" + key

    responses.respond(function_call("lookup", {"key": "record"}, "sync-call"))
    responses.respond(assistant_output("complete"))
    try:
        result = await Agent(
            instructions="Read.", tools=[lookup], provider=provider(responses)
        ).run("read", context=application)
    finally:
        trace.reset(token)
    assert result.final_output == "complete"
    [(ctx, value, worker_thread)] = calls
    assert ctx.context is application and ctx.submission is result.turn
    assert ctx.call_id == "sync-call"
    assert value == "request-context" and worker_thread != loop_thread
    assert "tenant-a:record" in str(responses.requests[1]["input"])


@pytest.mark.parametrize("ending", ["cancel", "close", "deadline"])
async def test_sync_worker_is_joined_and_late_result_is_not_converted(responses, ending):
    started = asyncio.Event()
    release = threading.Event()
    exited = threading.Event()
    effects = []
    serialized = []
    loop = asyncio.get_running_loop()

    class LateValue(BaseModel):
        @model_serializer
        def serialize(self) -> str:
            serialized.append("late")
            return "late-worker-output"

    @function_tool(callback_timeout=0.01 if ending == "deadline" else None)
    def pending() -> object:
        effects.append("application-effect")
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(10), "Test failed to release the owned worker"
            return LateValue()
        finally:
            exited.set()

    responses.respond(function_call("pending", {}, "sync-pending"))
    responses.respond(assistant_output("recovered"))
    agent = Agent(instructions="Read.", tools=[pending], provider=provider(responses))
    async with agent.session() as session:
        handle = await session.submit("read")
        waiter = asyncio.create_task((handle).get_result())
        entry = asyncio.create_task(started.wait())
        owner = None
        try:
            async with timeout(10):
                if ending == "deadline":
                    # The deadline includes executor admission. It may expire
                    # before this worker enters, in which case only the turn ends.
                    await asyncio.wait((entry, waiter), return_when=asyncio.FIRST_COMPLETED)
                else:
                    await entry
            worker_entered = started.is_set()
            if worker_entered:
                if ending == "deadline":
                    # Keep the real worker held while the deadline fires.
                    await asyncio.sleep(0.03)
                else:
                    owner = asyncio.create_task(
                        session._cancel(handle) if ending == "cancel" else session._aclose()
                    )
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    assert not owner.done()
                assert not waiter.done() and not exited.is_set()
                assert effects == ["application-effect"] and serialized == []
            else:
                assert ending == "deadline" and waiter.done()
                assert not exited.is_set() and effects == [] and serialized == []
        finally:
            release.set()
            entry.cancel()
            await asyncio.gather(entry, return_exceptions=True)
        async with timeout(10):
            if owner is not None:
                await owner
            with pytest.raises(TimeoutError if ending == "deadline" else TurnStoppedError):
                await waiter
        assert exited.is_set() is worker_entered
        assert effects == (["application-effect"] if worker_entered else [])
        assert serialized == []
        assert len(responses.requests) == 1
        if ending != "close":
            assert "late-worker-output" not in str([entry async for entry in session.items()])
            later = await session.submit("continue")
            assert (await (later).get_result()).final_output == "recovered"


class HeldInterrupt:
    """Delay native interruption after the Session has revoked Python ownership."""

    def __init__(self, native):
        self.native = native
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def __getattr__(self, name):
        return getattr(self.native, name)

    async def _interrupt_session(self, expected_turn_id):
        self.entered.set()
        await self.release.wait()
        return await self.native._interrupt_session(expected_turn_id)


@pytest.mark.parametrize("ending", ["cancel", "deadline"])
async def test_queued_sync_callback_cannot_start_after_ownership_ends(
    responses, monkeypatch, ending
):
    from concurrent.futures import ThreadPoolExecutor

    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
    occupied = asyncio.Event()
    release_worker = threading.Event()
    queued = asyncio.Event()
    settled = asyncio.Event()
    effects = []

    def occupy():
        loop.call_soon_threadsafe(occupied.set)
        assert release_worker.wait(10), "Test failed to release the executor"

    blocker = loop.run_in_executor(None, occupy)
    await occupied.wait()
    original = loop.run_in_executor

    def observe(executor, callback, *args):
        future = original(executor, callback, *args)
        future.add_done_callback(lambda _: settled.set())
        queued.set()
        return future

    @function_tool(callback_timeout=0.01 if ending == "deadline" else None)
    def pending() -> str:
        effects.append("callback-body-entered")
        return "late"

    responses.respond(function_call("pending", {}, "queued-sync"))
    agent = Agent(instructions="Read.", tools=[pending], provider=provider(responses))
    async with agent.session() as session:
        native = HeldInterrupt(session._native)
        session._native = native
        monkeypatch.setattr(loop, "run_in_executor", observe)
        handle = await session.submit("read")
        waiter = asyncio.create_task((handle).get_result())
        owner = None
        try:
            async with timeout(10):
                await queued.wait()
                if ending == "cancel":
                    owner = asyncio.create_task(session._cancel(handle))
                await native.entered.wait()
                release_worker.set()
                await blocker
                await settled.wait()
                assert effects == []
                assert not waiter.done()
        finally:
            release_worker.set()
            native.release.set()
            await blocker
            await asyncio.gather(
                *(task for task in (owner, waiter) if task is not None),
                return_exceptions=True,
            )
        if owner is not None:
            await owner
        with pytest.raises(TimeoutError if ending == "deadline" else TurnStoppedError):
            await waiter
        assert effects == []
        assert len(responses.requests) == 1


async def test_sync_returned_coroutine_cannot_start_after_ownership_ends(responses):
    entered = asyncio.Event()
    release_body = threading.Event()
    returned = asyncio.Event()
    effects = []
    loop = asyncio.get_running_loop()

    async def deferred() -> str:
        effects.append("deferred-body-entered")
        return "late"

    @function_tool
    def pending() -> Awaitable[str]:
        effects.append("sync-body-entered")
        loop.call_soon_threadsafe(entered.set)
        assert release_body.wait(10), "Test failed to release the sync body"
        value = deferred()
        loop.call_soon_threadsafe(returned.set)
        return value

    responses.respond(function_call("pending", {}, "deferred-sync"))
    agent = Agent(instructions="Read.", tools=[pending], provider=provider(responses))
    async with agent.session() as session:
        native = HeldInterrupt(session._native)
        session._native = native
        handle = await session.submit("read")
        waiter = asyncio.create_task((handle).get_result())
        owner = None
        try:
            async with timeout(10):
                await entered.wait()
                owner = asyncio.create_task(session._cancel(handle))
                await native.entered.wait()
                release_body.set()
                await returned.wait()
                # Join the actual invocation while native interruption remains held.
                await asyncio.gather(*session._active.callbacks, return_exceptions=True)
                assert effects == ["sync-body-entered"]
                assert not owner.done()
        finally:
            release_body.set()
            native.release.set()
            await asyncio.gather(
                *(task for task in (owner, waiter) if task is not None),
                return_exceptions=True,
            )
        assert effects == ["sync-body-entered"]
        assert len(responses.requests) == 1


@pytest.mark.parametrize("synchronous", [False, True])
async def test_callback_argument_names_reach_the_user_function(responses, synchronous):
    calls = []

    async def asynchronous(callback: str, admit: str) -> str:
        calls.append((callback, admit))
        return callback + ":" + admit

    def sync(callback: str, admit: str) -> str:
        calls.append((callback, admit))
        return callback + ":" + admit

    echo = function_tool(name="echo")(sync if synchronous else asynchronous)
    responses.respond(
        function_call("echo", {"callback": "user", "admit": "value"}, "argument-names")
    )
    responses.respond(assistant_output("complete"))
    result = await Agent(instructions="Read.", tools=[echo], provider=provider(responses)).run(
        "read"
    )
    assert result.final_output == "complete"
    assert calls == [("user", "value")]
    assert "user:value" in str(responses.requests[1]["input"])
