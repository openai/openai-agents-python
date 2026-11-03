"""One-shot interaction handlers retain native ownership through every exit."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest

from agents import ActionRequested, Agent, FunctionTool, InteractionExpiredError
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_manual_tools import LookupArgs, provider


async def test_run_sync_defaults_preserve_existing_agent_override():
    result = object()
    received = []
    context = {"record": "A"}

    class ExistingAgent(Agent):
        async def run(self, input, *, context=None, tools=None, before_step=None):
            assert tools is None and before_step is None
            received.append((input, context))
            return result

    actual = await asyncio.to_thread(
        ExistingAgent(instructions="").run_sync,
        "Read A",
        context=context,
    )
    assert actual is result
    assert received == [("Read A", context)]


def manual_agent(responses):
    tool = FunctionTool(
        name="lookup", description="Look up a record", input_type=LookupArgs, output_type=str
    )
    return Agent(instructions="Read records.", tools=[tool], provider=provider(responses))


async def test_run_delivers_concurrent_manual_requests(responses):
    responses.respond(
        function_call("lookup", {"key": "A"}, "first"),
        function_call("lookup", {"key": "B"}, "second"),
    )
    responses.respond(assistant_output("complete"))
    both = asyncio.Event()
    received: set[str] = set()

    async def answer(event):
        received.add(event.arguments.key)
        if len(received) == 2:
            both.set()
        await both.wait()
        return event.reply("answer-" + event.arguments.key)

    async with timeout(15):
        result = await manual_agent(responses).run("Read both", on_interaction=answer)
    assert result.final_output == "complete"
    assert received == {"A", "B"}
    assert tool_output(responses.requests[1], "first") == "answer-A"
    assert tool_output(responses.requests[1], "second") == "answer-B"


async def test_run_propagates_handler_failure_and_releases_provider(responses):
    responses.respond(function_call("lookup", {"key": "A"}, "first"))
    problem = ValueError("Application lookup failed")
    agent = manual_agent(responses)

    async def answer(event):
        raise problem

    async with timeout(15):
        with pytest.raises(ValueError) as failed:
            await agent.run("Read A", on_interaction=answer)
        assert failed.value is problem
        assert len(responses.requests) == 1
        responses.respond(assistant_output("next run"))
        assert (
            await agent.run(
                "Continue",
                on_interaction=lambda _: pytest.fail(
                    "Recovered run unexpectedly requested a lookup"
                ),
            )
        ).final_output == "next run"


async def test_handler_can_answer_own_request_without_general_session_reentry(responses):
    responses.respond(function_call("lookup", {"key": "A"}, "first"))
    responses.respond(assistant_output("complete"))

    async def answer(event):
        # Capture the one-shot session solely to probe its public admission boundary.
        session = event._session()
        assert session is not None
        with pytest.raises(RuntimeError, match="start work in its own Session"):
            await session.submit("unowned input")
        with pytest.raises(RuntimeError, match="read interactions from its own Session"):
            await session.pending_interactions()
        return event.reply("answer-A")

    result = await manual_agent(responses).run("Read A", on_interaction=answer)
    assert result.final_output == "complete"
    assert tool_output(responses.requests[1], "first") == "answer-A"
    assert len(responses.requests) == 2


@pytest.mark.parametrize("handler_kind", ["sync", "async"])
async def test_handler_returning_without_responding_expires_grant_and_joins_cleanup(
    responses, handler_kind
):
    responses.respond(function_call("lookup", {"key": "A"}, "first"))
    requests = []
    agent = manual_agent(responses)

    async def answer(event):
        requests.append(event)

    def sync_answer(event):
        requests.append(event)

    async with timeout(15):
        with pytest.raises(TypeError, match="return a reply"):
            await agent.run(
                "Read A",
                on_interaction=cast(Any, sync_answer if handler_kind == "sync" else answer),
            )
        with pytest.raises((ValueError, RuntimeError), match="expired|closed"):
            await requests[0].respond("must not leak")
        assert len(responses.requests) == 1
        responses.respond(assistant_output("next run"))
        assert (
            await agent.run(
                "Continue",
                on_interaction=lambda _: pytest.fail(
                    "Recovered run unexpectedly requested a lookup"
                ),
            )
        ).final_output == "next run"


async def test_run_rejects_ordinary_input_as_interaction_reply(responses):
    responses.respond(function_call("lookup", {"key": "A"}, "first"))

    async def answer(event):
        return "This must not become another user turn"

    async with timeout(15):
        with pytest.raises(TypeError, match="return a reply"):
            await manual_agent(responses).run("Read A", on_interaction=cast(Any, answer))
    assert len(responses.requests) == 1


async def test_run_rejects_a_live_sibling_reply(responses):
    responses.respond(
        function_call("lookup", {"key": "A"}, "first"),
        function_call("lookup", {"key": "B"}, "second"),
    )
    second = asyncio.Event()
    sibling: ActionRequested[Any, Any] | None = None
    cancelled = asyncio.Event()

    async def answer(event):
        nonlocal sibling
        if event.arguments.key == "B":
            sibling = event
            second.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        await second.wait()
        assert sibling is not None
        return sibling.reply("wrong request")

    async with timeout(15):
        with pytest.raises(TypeError, match="return a reply"):
            await manual_agent(responses).run("Read both", on_interaction=answer)
    assert cancelled.is_set()
    assert len(responses.requests) == 1


async def test_run_validates_manual_output_through_captured_adapter(responses):
    responses.respond(function_call("lookup", {"key": "A"}, "first"))

    async def answer(event):
        return event.reply({"invalid": "not a string"})

    async with timeout(15):
        with pytest.raises(ValueError, match="failed validation"):
            await manual_agent(responses).run("Read A", on_interaction=answer)
    assert len(responses.requests) == 1


async def test_run_cancellation_joins_handler_cleanup_under_repeated_cancel(responses):
    responses.respond(function_call("lookup", {"key": "A"}, "first"))
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    agent = manual_agent(responses)

    async def answer(event):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            finished.set()

    async with timeout(15):
        running = asyncio.create_task(agent.run("Read A", on_interaction=answer))
        try:
            await entered.wait()
            running.cancel()
            await cleaning.wait()
            running.cancel()
            await asyncio.sleep(0)
            assert not running.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert finished.is_set()
        responses.respond(assistant_output("released"))
        assert (
            await agent.run(
                "Continue",
                on_interaction=lambda _: pytest.fail(
                    "Recovered run unexpectedly requested a lookup"
                ),
            )
        ).final_output == "released"


async def test_run_cancellation_joins_synchronous_handler(responses):
    responses.respond(function_call("lookup", {"key": "A"}, "first"))
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def answer(event):
        entered.set()
        try:
            release.wait()
            return None
        finally:
            finished.set()

    async with timeout(15):
        running = asyncio.create_task(
            manual_agent(responses).run("Read A", on_interaction=cast(Any, answer))
        )
        try:
            assert await asyncio.to_thread(entered.wait, 10)
            running.cancel()
            await asyncio.sleep(0)
            assert not running.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await running
    assert finished.is_set()
    assert len(responses.requests) == 1


async def test_native_terminal_retires_independently_answered_handler(responses, monkeypatch):
    responses.respond(function_call("lookup", {"key": "A"}, "first"))
    responses.respond(assistant_output("complete"))
    agent = manual_agent(responses)
    original = agent.session
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    captured: dict[str, Any] = {}

    @asynccontextmanager
    async def observe_session(**kwargs):
        async with original(**kwargs) as session:
            captured["session"] = session
            yield session

    monkeypatch.setattr(agent, "session", observe_session)

    async def answer(event):
        captured["event"] = event
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    async with timeout(15):
        running = asyncio.create_task(agent.run("Read A", on_interaction=answer))
        try:
            await entered.wait()
            await captured["event"].respond("external answer")
            await cleaning.wait()
            assert not running.done()
        finally:
            release.set()
        assert (await running).final_output == "complete"
    assert tool_output(responses.requests[1], "first") == "external answer"


@pytest.mark.parametrize("revoked_at", ["queued", "returned-awaitable"])
async def test_independent_reply_before_handler_admission_preserves_live_sibling(
    responses, monkeypatch, revoked_at
):
    responses.respond(
        function_call("lookup", {"key": "A"}, "first"),
        function_call("lookup", {"key": "B"}, "second"),
    )
    responses.respond(assistant_output("complete"))
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
    agent = manual_agent(responses)
    original_session = agent.session
    original_executor = loop.run_in_executor
    queued_b = asyncio.Event()
    entered = asyncio.Event()
    allow_a = asyncio.Event()
    release_worker = threading.Event()
    workers = []
    captured = {}
    called = []
    bodies = []

    def observe_executor(executor, callback, *args):
        future = original_executor(executor, callback, *args)
        workers.append(future)
        if len(workers) == 2:
            queued_b.set()
        return future

    @asynccontextmanager
    async def observe_session(**kwargs):
        async with original_session(**kwargs) as session:
            captured["session"] = session
            monkeypatch.setattr(loop, "run_in_executor", observe_executor)
            yield session

    monkeypatch.setattr(agent, "session", observe_session)

    def answer(event):
        key = event.arguments.key
        called.append(key)
        if len(called) == (1 if revoked_at == "queued" else 2):
            loop.call_soon_threadsafe(entered.set)
            assert release_worker.wait(10), "Test failed to release the handler"

        async def finish():
            bodies.append(key)
            if key == called[0]:
                await allow_a.wait()
            return event.reply("handler-" + key)

        return finish()

    async with timeout(15):
        running = asyncio.create_task(agent.run("Read both", on_interaction=answer))
        try:
            await entered.wait()
            await queued_b.wait()
            async for request in captured["session"].interactions():
                if request.arguments.key != called[0]:
                    await request.respond("external-" + request.arguments.key)
                    break
            release_worker.set()
            await asyncio.wait((workers[1],))
            allow_a.set()
            result = await running
        finally:
            release_worker.set()
            allow_a.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    assert result.final_output == "complete"
    assert len(called) == (1 if revoked_at == "queued" else 2)
    assert bodies == [called[0]]
    assert len(responses.requests) == 2
    for key, call_id in (("A", "first"), ("B", "second")):
        source = "handler-" if key == called[0] else "external-"
        assert tool_output(responses.requests[1], call_id) == source + key


@pytest.mark.parametrize(
    ("handler_kind", "reply_kind"),
    [("async", "matching"), ("sync", "matching"), ("async", "none"), ("sync", "foreign")],
)
async def test_independent_reply_during_invoked_handler_preserves_only_valid_return(
    responses, monkeypatch, handler_kind, reply_kind
):
    responses.respond(
        function_call("lookup", {"key": "A"}, "first"),
        function_call("lookup", {"key": "B"}, "second"),
    )
    responses.respond(assistant_output("complete"))
    loop = asyncio.get_running_loop()
    agent = manual_agent(responses)
    original_session = agent.session
    captured = {}
    requests = {}
    both_entered = asyncio.Event()
    a_finished = asyncio.Event()
    allow_a = asyncio.Event()
    allow_b = asyncio.Event()
    worker_a = threading.Event()
    worker_b = threading.Event()
    finished = []

    @asynccontextmanager
    async def observe_session(**kwargs):
        async with original_session(**kwargs) as session:
            captured["session"] = session
            yield session

    monkeypatch.setattr(agent, "session", observe_session)

    def entered(event):
        requests[event.arguments.key] = event
        if len(requests) == 2:
            both_entered.set()

    def returned(event):
        key = event.arguments.key
        if key == "A" and reply_kind == "none":
            return None
        if key == "A" and reply_kind == "foreign":
            return requests["B"].reply("must not substitute")
        return event.reply("handler-" + key)

    async def async_handler(event):
        key = event.arguments.key
        entered(event)
        try:
            await (allow_a if key == "A" else allow_b).wait()
            return returned(event)
        finally:
            finished.append(key)
            if key == "A":
                a_finished.set()

    def sync_handler(event):
        key = event.arguments.key
        loop.call_soon_threadsafe(entered, event)
        try:
            assert (worker_a if key == "A" else worker_b).wait(10), "Handler was not released"
            return returned(event)
        finally:
            finished.append(key)
            if key == "A":
                loop.call_soon_threadsafe(a_finished.set)

    handler = async_handler if handler_kind == "async" else sync_handler
    async with timeout(15):
        running = asyncio.create_task(agent.run("Read both", on_interaction=cast(Any, handler)))
        try:
            await both_entered.wait()
            async for event in captured["session"].interactions():
                if event.arguments.key == "A":
                    await event.respond("external-A")
                    with pytest.raises(InteractionExpiredError, match="expired|already delivered"):
                        await event.respond("duplicate-A")
                    break
            assert not allow_b.is_set() and not worker_b.is_set()
            allow_a.set()
            worker_a.set()
            await a_finished.wait()
            allow_b.set()
            worker_b.set()
            if reply_kind == "matching":
                result = await running
                assert result.final_output == "complete"
                assert len(responses.requests) == 2
                assert tool_output(responses.requests[1], "first") == "external-A"
                assert tool_output(responses.requests[1], "second") == "handler-B"
            else:
                with pytest.raises(TypeError, match="return a reply"):
                    await running
        finally:
            allow_a.set()
            allow_b.set()
            worker_a.set()
            worker_b.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    assert sorted(finished) == ["A", "B"]


async def test_run_reports_handler_cleanup_failure(responses):
    responses.respond(
        function_call("lookup", {"key": "A"}, "first"),
        function_call("lookup", {"key": "B"}, "second"),
    )
    entered = asyncio.Event()
    primary = ValueError("Primary failure")
    cleanup = RuntimeError("Cleanup failure")

    async def answer(event):
        if event.arguments.key == "A":
            await entered.wait()
            raise primary
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            raise cleanup

    async with timeout(15):
        with pytest.raises(ValueError) as failed:
            await manual_agent(responses).run("Read both", on_interaction=answer)
    assert failed.value is primary
    contexts = []
    current = primary.__context__
    while current is not None and current not in contexts:
        contexts.append(current)
        current = current.__context__
    assert cleanup in contexts


async def test_run_checks_handler_before_provider_start(responses):
    with pytest.raises(TypeError, match="on_interaction must be callable"):
        await manual_agent(responses).run("Read A", on_interaction=cast(Any, 42))
    assert responses.requests == []
