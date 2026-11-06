"""Check native Session ownership across callback and application lifetimes."""

import asyncio
import importlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, field_validator, model_serializer

from agents import (
    Agent,
    Completed,
    NativeError,
    NativeProvider,
    Session,
    StaleTurnError,
    ToolContext,
    Turn,
    TurnStoppedError,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    core_owner as core_owner,
    function_call,
    responses as responses,
)


def provider(responses: ResponsesFixture) -> NativeProvider:
    return NativeProvider(base_url=responses.url, api_key="synthetic-lifecycle-key")


@pytest.mark.usefixtures("core_owner")
async def test_native_error_waits_for_terminal_without_interrupting_failed_turn(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses.respond(
        {
            "type": "response.failed",
            "response": {
                "error": {
                    "type": "invalid_request_error",
                    "code": "invalid_prompt",
                    "message": "synthetic request rejected",
                }
            },
        }
    )
    responses.respond(assistant_output("recovered"))
    agent = Agent(instructions="Answer briefly.", provider=provider(responses))
    terminal_waiting, release_terminal = asyncio.Event(), asyncio.Event()
    interrupted = False
    async with timeout(30), agent.session() as session:
        native = session._native

        class HoldFailedTerminal:
            failed = False

            def __getattr__(self, name: str) -> Any:
                return getattr(native, name)

            async def _next_session_event(self) -> str:
                raw = await native._next_session_event()
                event = json.loads(raw)
                if event is not None:
                    kind = event["msg"]["type"]
                    if kind == "error":
                        self.failed = True
                    elif self.failed and kind in {"task_complete", "turn_complete"}:
                        terminal_waiting.set()
                        await release_terminal.wait()
                return raw

            async def _interrupt_session(self, expected_turn_id) -> None:
                nonlocal interrupted
                interrupted = True
                await native._interrupt_session(expected_turn_id)

        monkeypatch.setattr(session, "_native", HoldFailedTerminal())
        handle = await session.submit("rejected request")
        waiting = asyncio.create_task((handle).get_result())
        try:
            await terminal_waiting.wait()
            # Drain the ready callbacks from the Error notice while the native
            # terminal remains gated. No wall-clock race decides this assertion.
            for _ in range(3):
                await asyncio.sleep(0)
            assert not interrupted
            assert not waiting.done()
        finally:
            release_terminal.set()
            with pytest.raises(NativeError, match="synthetic request rejected"):
                await waiting
        assert "synthetic request rejected" in json.dumps(
            [entry async for entry in session.turns()]
        )
        result = await (await session.submit("try again")).get_result()
        assert result.final_output == "recovered"
    assert len(responses.requests) == 2


@pytest.mark.asyncio
async def test_cancel_settled_handle_preserves_active_submission_and_exact_results(
    responses: ResponsesFixture,
) -> None:
    responses.respond(assistant_output("first result"))
    responses.respond(assistant_output("second result"))
    agent = Agent(instructions="Be concise.", model="gpt-5.5", provider=provider(responses))
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_second_response(event: dict[str, Any]) -> None:
        if event["type"] == "response.created":
            entered.set()
            await release.wait()

    async with timeout(10), agent.session() as session:
        first = await session.submit("first")
        first_result = await (first).get_result()
        responses.before_event = hold_second_response
        second = await session.submit("second")
        waiting = asyncio.create_task((second).get_result())
        try:
            await entered.wait()
            await session._cancel(first)
            await session._cancel(first)
            assert await (first).get_result() is first_result
            assert not waiting.done()
            async with agent.session() as foreign:
                with pytest.raises(StaleTurnError, match="different Session instance"):
                    await foreign._cancel(first)
        finally:
            release.set()
            second_result = await waiting
        assert first_result.final_output == "first result"
        assert second_result.final_output == "second result"
        await session._cancel(first)
        await session._cancel(second)
    for _ in range(2):
        await session._cancel(first)
        await session._cancel(second)
        assert await (first).get_result() is first_result
        assert await (second).get_result() is second_result
    assert len(responses.requests) == 2


def task_cancellation(error: asyncio.CancelledError) -> asyncio.CancelledError:
    if sys.version_info < (3, 11):
        # Python 3.10 Task wraps a raised cancellation and retains it as context.
        assert error.args == ()
        assert error.__cause__ is None
        original = error.__context__
        assert isinstance(original, asyncio.CancelledError)
        return original
    return error


@pytest.mark.parametrize("awaited_child", [False, True])
@pytest.mark.parametrize("observe_result", [False, True])
async def test_callback_cannot_wait_on_its_own_submission(
    responses: ResponsesFixture, awaited_child: bool, observe_result: bool
) -> None:
    current = None
    earlier = None
    earlier_result = None

    @function_tool
    async def inspect_wait(context: ToolContext[None]) -> str:
        assert current is not None
        async with timeout(2):
            with pytest.raises(RuntimeError, match="wait on its own turn"):
                if awaited_child:
                    await asyncio.create_task((context.submission).get_result())
                else:
                    await (context.submission).get_result()
        assert earlier is not None
        if observe_result:
            assert await (earlier).get_result() is earlier_result
        async with timeout(2):
            with pytest.raises(RuntimeError, match="wait for requests"):
                if awaited_child:
                    await asyncio.create_task(context.submission.wait_for_request_or_result())
                else:
                    await context.submission.wait_for_request_or_result()
        if awaited_child:
            result = await asyncio.create_task(earlier.wait_for_request_or_result())
        else:
            result = await earlier.wait_for_request_or_result()
        assert isinstance(result, Completed)
        assert result.final_output == "earlier result"
        assert await earlier.get_result() is result
        if observe_result:
            assert result is earlier_result
        return "self-wait rejected; earlier result retained"

    responses.respond(assistant_output("earlier result"))
    responses.respond(function_call("inspect_wait", {}, "self-wait"))
    responses.respond(assistant_output("callback completed"))
    agent = Agent(
        instructions="Use the selected tool.",
        tools=[inspect_wait],
        provider=provider(responses),
    )
    async with timeout(30), agent.session() as session:
        current = session
        earlier = await session.submit("first")
        if observe_result:
            earlier_result = await (earlier).get_result()
        else:
            # Observations can settle a prior Turn without reading its result.
            # Do not prime the logical result cache before entering the callback.
            events = [event async for event in earlier.events()]
            assert events
        handle = await session.submit("call the tool")
        result = await (handle).get_result()
        assert result.final_output == "callback completed"
        assert (await earlier.get_result()).final_output == "earlier result"


@pytest.mark.parametrize("operation", ["get_result", "request_or_result", "handed_off_events"])
async def test_callback_cannot_wait_for_external_queued_work(
    responses: ResponsesFixture, operation: str
) -> None:
    entered, queued_available = asyncio.Event(), asyncio.Event()
    queued: Turn[str] | None = None
    queued_events = None
    earlier: Turn[str] | None = None
    earlier_result = None
    rejected: list[str] = []

    @function_tool
    async def await_queued(context: ToolContext[None]) -> str:
        entered.set()
        await queued_available.wait()
        assert queued is not None and queued_events is not None and earlier is not None
        assert await earlier.get_result() is earlier_result
        async with timeout(2):
            with pytest.raises(RuntimeError, match="A tool callback cannot"):
                if operation == "get_result":
                    await queued.get_result()
                elif operation == "request_or_result":
                    await queued.wait_for_request_or_result()
                else:
                    await anext(queued_events)
        rejected.append(operation)
        return "queued wait rejected"

    responses.respond(assistant_output("earlier result"))
    responses.respond(function_call("await_queued", {}, "queued-self-wait"))
    responses.respond(assistant_output("active completed"))
    responses.respond(assistant_output("queued completed"))
    agent = Agent(
        instructions="Use the selected tool.", tools=[await_queued], provider=provider(responses)
    )
    async with timeout(30), agent.session() as session:
        earlier = await session.submit("first")
        earlier_result = await earlier.get_result()
        active = await session.submit("run the tool")
        await entered.wait()
        queued = await session.submit("after the active turn", if_busy="queue")
        queued_events = queued.events()
        assert queued.turn_id is None
        queued_available.set()
        try:
            assert (await active.get_result()).final_output == "active completed"
            assert rejected == [operation]
            assert (await queued.get_result()).final_output == "queued completed"
            assert await earlier.get_result() is earlier_result
        finally:
            await queued_events.aclose()
    assert len(responses.requests) == 4


@pytest.mark.parametrize("reader_exit", ["error", "eof"])
async def test_reader_exit_immediately_after_terminal_preserves_the_result(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch, reader_exit: str
) -> None:
    responses.respond(assistant_output("confirmed terminal result"))
    agent = Agent(instructions="Answer briefly.", provider=provider(responses))
    async with timeout(30), agent.session() as session:
        native_thread = session._native

        class ExitAfterTerminal:
            terminal_delivered = False

            def __getattr__(self, name: str) -> Any:
                return getattr(native_thread, name)

            async def _next_session_event(self) -> str:
                if self.terminal_delivered:
                    # Do not yield after delivering the terminal: the execution
                    # task must still be waiting when the reader exits.
                    if reader_exit == "error":
                        raise OSError("reader failed after confirmed terminal")
                    return "null"
                raw = await native_thread._next_session_event()
                event = json.loads(raw)
                if event is not None and event["msg"]["type"] in {
                    "task_complete",
                    "turn_complete",
                }:
                    self.terminal_delivered = True
                return raw

        monkeypatch.setattr(session, "_native", ExitAfterTerminal())
        handle = await session.submit("start")
        await session._reader
        result = await (handle).get_result()
        assert result.final_output == "confirmed terminal result"
        with pytest.raises(RuntimeError, match="cannot continue"):
            await session.submit("must not reach the provider")
        assert len(responses.requests) == 1
    assert await (handle).get_result() is result


@pytest.mark.parametrize("prior_turn", [False, True])
async def test_confirmed_native_rejection_preserves_the_session(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch, prior_turn: bool
) -> None:
    from agents import _native

    rejection_started = asyncio.Event()
    event_delivered = asyncio.Event()
    original_next = _native.NativeThread._next_session_event

    async def next_event(native_thread):
        if not prior_turn and not event_delivered.is_set():
            # The startup reader is already active at Session publication.
            # Install this receipt interception before it starts polling.
            await rejection_started.wait()
            event_delivered.set()
            return json.dumps({"id": "session-event", "msg": {"type": "session_configured"}})
        return await original_next(native_thread)

    monkeypatch.setattr(_native.NativeThread, "_next_session_event", next_event)
    agent = Agent(instructions="Answer briefly.", provider=provider(responses))
    async with timeout(30), agent.session() as session:
        original_id = session.id
        earlier = None
        earlier_result = None
        if prior_turn:
            responses.respond(assistant_output("earlier result"))
            earlier = await session.submit("first")
            earlier_result = await (earlier).get_result()
        native_thread = session._native

        class RejectOnce:
            reject_next = True

            def __getattr__(self, name: str) -> Any:
                return getattr(native_thread, name)

            async def _submit(self, request: str) -> str:
                if self.reject_next:
                    self.reject_next = False
                    if not prior_turn:
                        # Force a non-turn event to reach the reader while it
                        # still awaits this submission's acceptance.
                        rejection_started.set()
                        await event_delivered.wait()
                    # Inject only the receipt. The engine, reader lifetime,
                    # subsequent valid submission and cleanup remain native.
                    return json.dumps(
                        {
                            "status": "not_submitted",
                            "reason": {"type": "not_idle"},
                        }
                    )
                return await native_thread._submit(request)

        monkeypatch.setattr(session, "_native", RejectOnce())
        rejected = await session.submit("obtain a confirmed native rejection")
        with pytest.raises(NativeError, match="not submitted: not_idle"):
            await rejected.get_result()
        assert len(responses.requests) == int(prior_turn)
        responses.respond(assistant_output("accepted after rejection"))
        handle = await session.submit("valid input")
        assert (await (handle).get_result()).final_output == "accepted after rejection"
        assert session.id == original_id
        assert len(responses.requests) == int(prior_turn) + 1
        if earlier is not None:
            assert await (earlier).get_result() is earlier_result


async def test_unconfirmed_submission_failure_still_retires_the_session(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = OSError("native acceptance could not be confirmed")
    agent = Agent(instructions="Answer briefly.", provider=provider(responses))
    async with timeout(30), agent.session() as session:
        native_thread = session._native

        class SubmitFailure:
            def __getattr__(self, name: str) -> Any:
                return getattr(native_thread, name)

            async def _submit(self, request: str) -> str:
                raise failure

        monkeypatch.setattr(session, "_native", SubmitFailure())
        submitted = await session.submit("unknown acceptance")
        with pytest.raises(OSError) as caught:
            await submitted.get_result()
        assert caught.value is failure
        with pytest.raises(RuntimeError, match="cannot continue"):
            await session.submit("must not reach the provider")
        assert responses.requests == []


@pytest.mark.parametrize("failure_point", ["thread_start", "construction"])
@pytest.mark.parametrize("explicit_cause", [False, True])
@pytest.mark.usefixtures("core_owner")
async def test_open_preserves_primary_error_and_all_cleanup_failures(
    responses: ResponsesFixture,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    explicit_cause: bool,
) -> None:
    native = importlib.import_module("agents._native")
    create_engine = native.create_engine
    primary = ValueError("session startup failed")
    original_cause = LookupError("original startup cause") if explicit_cause else None
    thread_error = OSError("thread cleanup failed")
    engine_error = OSError("engine cleanup failed")
    closed = []
    native_owners = []
    close_orphan = native.NativeProviderLease.close_orphan
    homes = []

    class FailingClose:
        def __init__(self, owner: Any, kind: str, error: BaseException) -> None:
            self.owner = owner
            native_owners.append(owner)
            self.kind = kind
            self.error = error

        def __getattr__(self, name: str) -> Any:
            return getattr(self.owner, name)

    class Engine(FailingClose):
        async def start_replaceable_thread(self, *args: Any, **kwargs: Any) -> Any:
            if failure_point == "thread_start":
                raise primary from original_cause
            thread = await self.owner.start_replaceable_thread(*args, **kwargs)
            return FailingClose(thread, "thread", thread_error)

    async def create(*args: Any, **kwargs: Any) -> Any:
        homes.append(Path(json.loads(args[1])["codex_home"]))
        return Engine(await create_engine(*args, **kwargs), "engine", engine_error)

    async def failing_retirement(lease: Any) -> None:
        await close_orphan(lease)
        closed.append("native retirement")
        # The native owner now returns one composite cleanup failure. Preserve
        # every nested cause without reintroducing Python-owned native stages.
        engine_error.__context__ = thread_error if failure_point == "construction" else None
        raise engine_error

    def fail_construction(*args: Any, **kwargs: Any) -> None:
        raise primary from original_cause

    monkeypatch.setattr(native, "create_engine", create)
    monkeypatch.setattr(native.NativeProviderLease, "close_orphan", failing_retirement)
    if failure_point == "construction":
        monkeypatch.setattr(Session, "__init__", fail_construction)
    agent = Agent(instructions="Answer briefly.", provider=provider(responses))
    async with timeout(30):
        with pytest.raises(ValueError) as caught:
            async with agent.session():
                pytest.fail("opening must fail")
    assert caught.value is primary
    assert primary.__cause__ is original_cause
    assert primary.__suppress_context__
    assert primary.__context__ is engine_error
    assert engine_error.__context__ is (thread_error if failure_point == "construction" else None)
    if failure_point == "construction":
        assert thread_error.__context__ is None
    assert closed == ["native retirement"]
    for owner in native_owners:
        if isinstance(owner, native.NativeThread):
            with pytest.raises(RuntimeError, match="closed"):
                await owner.read_thread_history(None)
    await agent.provider.aclose()
    assert len(homes) == 1 and not homes[0].exists()
    assert responses.requests == []


@pytest.mark.usefixtures("core_owner")
async def test_cancelled_open_joins_late_cleanup_and_preserves_cancellation(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = importlib.import_module("agents._native")
    create_engine = native.create_engine
    creation_started = asyncio.Event()
    creation_release = asyncio.Event()
    close_started = asyncio.Event()
    close_release = asyncio.Event()
    cleanup_error = OSError("late engine cleanup failed")
    homes = []

    close_orphan = native.NativeProviderLease.close_orphan

    async def delayed_retirement(lease: Any) -> None:
        close_started.set()
        await close_release.wait()
        await close_orphan(lease)
        raise cleanup_error

    async def create(*args: Any, **kwargs: Any) -> Any:
        homes.append(Path(json.loads(args[1])["codex_home"]))
        creation_started.set()
        await creation_release.wait()
        return await create_engine(*args, **kwargs)

    monkeypatch.setattr(native, "create_engine", create)
    monkeypatch.setattr(native.NativeProviderLease, "close_orphan", delayed_retirement)
    agent = Agent(instructions="Answer briefly.", provider=provider(responses))
    async with timeout(30):
        opening = asyncio.create_task(agent.run("never submitted"))
        try:
            await creation_started.wait()
            opening.cancel("caller cancelled acquisition")
            creation_release.set()
            await close_started.wait()
            opening.cancel("cancelled again during cleanup")
            await asyncio.sleep(0)
            assert not opening.done()
            close_release.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await opening
            cancellation = task_cancellation(caught.value)
            assert str(cancellation) == "caller cancelled acquisition"
            assert cancellation.__context__ is cleanup_error
            assert opening.cancelled()
            assert cleanup_error.__context__ is None
        finally:
            creation_release.set()
            close_release.set()
            await asyncio.gather(opening, return_exceptions=True)
    assert len(homes) == 1 and not homes[0].exists()
    await agent.provider.aclose()
    assert responses.requests == []


@pytest.mark.parametrize("body_fails", [False, True])
@pytest.mark.parametrize("engine_fails", [False, True])
@pytest.mark.usefixtures("core_owner")
async def test_session_exit_preserves_body_error_and_reports_normal_close_failure(
    responses: ResponsesFixture,
    monkeypatch: pytest.MonkeyPatch,
    body_fails: bool,
    engine_fails: bool,
) -> None:
    primary = ValueError("application body failed")
    cleanup_error = OSError("thread cleanup failed after closing")
    engine_error = OSError("engine cleanup failed after closing")
    agent = Agent(instructions="Answer briefly.", provider=provider(responses))
    expected = ValueError if body_fails else OSError
    async with timeout(30):
        with pytest.raises(expected) as caught:
            async with agent.session() as session:
                home = Path(session._home.name)
                native_thread = session._native
                native_engine = session._engine

                class FailingClose:
                    def __getattr__(self, name: str) -> Any:
                        return getattr(native_thread, name)

                    async def close(self) -> None:
                        await native_thread.close()
                        raise cleanup_error

                monkeypatch.setattr(session, "_native", FailingClose())
                if engine_fails:

                    class FailingEngineClose:
                        def __getattr__(self, name: str) -> Any:
                            return getattr(native_engine, name)

                        async def close(self) -> None:
                            await native_engine.close()
                            raise engine_error

                    monkeypatch.setattr(session, "_engine", FailingEngineClose())
                if body_fails:
                    raise primary
    last_cleanup_error = engine_error if engine_fails else cleanup_error
    assert caught.value is (primary if body_fails else last_cleanup_error)
    if body_fails:
        assert primary.__context__ is last_cleanup_error
    if engine_fails:
        assert engine_error.__context__ is cleanup_error
    assert cleanup_error.__context__ is None
    assert session._reader.done()
    if engine_fails:
        # A failed close receipt does not prove that the native owner stopped,
        # even when this injected failure follows actual shutdown.
        assert home.exists()
        assert not session._closed
        monkeypatch.setattr(session, "_native", native_thread)
        monkeypatch.setattr(session, "_engine", native_engine)
        await session._aclose()
    assert not home.exists()
    assert responses.requests == []


@pytest.mark.usefixtures("core_owner")
async def test_failed_close_retires_reader_and_allows_explicit_retry(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = Agent(instructions="Answer briefly.", provider=provider(responses))
    manager = agent.session()
    session = await manager.__aenter__()
    native_thread, native_engine = session._native, session._engine
    home = Path(session._home.name)
    thread_error = OSError("thread close could not start")
    engine_error = OSError("engine close could not start")
    attempts: list[str] = []

    class FailBeforeClose:
        def __init__(self, owner: Any, name: str, error: OSError) -> None:
            self.owner, self.name, self.error = owner, name, error

        def __getattr__(self, name: str) -> Any:
            return getattr(self.owner, name)

        async def close(self) -> None:
            attempts.append(self.name)
            raise self.error

    monkeypatch.setattr(session, "_native", FailBeforeClose(native_thread, "thread", thread_error))
    monkeypatch.setattr(session, "_engine", FailBeforeClose(native_engine, "engine", engine_error))
    closing = asyncio.create_task(session._aclose())
    try:
        done, _ = await asyncio.wait((closing,), timeout=2)
        assert closing in done, "Failed shutdown must report its error promptly"
        with pytest.raises(OSError) as caught:
            await closing
        assert caught.value is engine_error
        assert engine_error.__context__ is thread_error
        assert attempts == ["thread", "engine"]
        assert session._reader.done()
        assert not session._closed and home.exists()
        with pytest.raises(RuntimeError, match="closed"):
            await session.submit("must remain fenced")
    finally:
        monkeypatch.setattr(session, "_native", native_thread)
        monkeypatch.setattr(session, "_engine", native_engine)
        # Also release the baseline's stuck close before failing the assertion.
        if not closing.done():
            await native_thread.close()
            await native_engine.close()
        await asyncio.gather(closing, return_exceptions=True)
        await session._aclose()
        await manager.__aexit__(None, None, None)
    assert session._closed and not home.exists()
    assert responses.requests == []


@pytest.mark.parametrize("cancel_again", [False, True])
@pytest.mark.usefixtures("core_owner")
async def test_cancelled_close_preserves_first_cancellation_after_cleanup_failure(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch, cancel_again: bool
) -> None:
    manager = Agent(instructions="Answer briefly.", provider=provider(responses)).session()
    session = await manager.__aenter__()
    native_engine = session._engine
    started, release = asyncio.Event(), asyncio.Event()
    cleanup_error = OSError("engine cleanup failed after cancellation")

    class FailAfterClose:
        async def close(self) -> None:
            started.set()
            await release.wait()
            await native_engine.close()
            raise cleanup_error

    monkeypatch.setattr(session, "_engine", FailAfterClose())
    closing = asyncio.create_task(session._aclose())
    try:
        await started.wait()
        closing.cancel("first close cancellation")
        await asyncio.sleep(0)
        if cancel_again:
            closing.cancel("later close cancellation")
            await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await closing
        cancellation = task_cancellation(caught.value)
        assert str(cancellation) == "first close cancellation"
        assert cancellation.__context__ is cleanup_error
        assert closing.cancelled()
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)
        monkeypatch.setattr(session, "_engine", native_engine)
        await session._aclose()
        await manager.__aexit__(None, None, None)


@pytest.mark.skipif(sys.version_info < (3, 12), reason="Eager tasks require Python 3.12.")
@pytest.mark.usefixtures("core_owner")
async def test_session_exit_preserves_nested_cleanup_with_eager_tasks(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    loop.set_task_factory(asyncio.eager_task_factory)
    try:
        await test_session_exit_preserves_body_error_and_reports_normal_close_failure(
            responses, monkeypatch, body_fails=True, engine_fails=True
        )
    finally:
        loop.set_task_factory(previous_factory)


@pytest.mark.usefixtures("core_owner")
async def test_cancelled_submit_preserves_cancellation_when_close_fails(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    submission_started = asyncio.Event()
    cancellation_observed = False
    cleanup_error = OSError("thread cleanup failed after closing")
    agent = Agent(instructions="Answer briefly.", provider=provider(responses))
    async with timeout(30):
        with pytest.raises(OSError) as normal_exit:
            async with agent.session() as session:
                home = Path(session._home.name)
                native_thread = session._native

                class PendingSubmit:
                    def __getattr__(self, name: str) -> Any:
                        return getattr(native_thread, name)

                    async def _submit(self, request: str) -> str:
                        submission_started.set()
                        await asyncio.Event().wait()
                        raise AssertionError("pending acceptance must be cancelled")

                    async def close(self) -> None:
                        await native_thread.close()
                        raise cleanup_error

                monkeypatch.setattr(session, "_native", PendingSubmit())
                submitted = await session.submit("never accepted")
                closing = None
                try:
                    await submission_started.wait()
                    # Close revokes this locally acknowledged submission while
                    # the actual native receipt is still unknown.
                    closing = asyncio.create_task(session._aclose())
                    with pytest.raises(OSError) as closed:
                        await closing
                    assert closed.value is cleanup_error
                    with pytest.raises(NativeError, match="Turn cleanup failed") as caught:
                        await submitted.get_result()
                    assert isinstance(caught.value.__cause__, asyncio.CancelledError)
                    assert caught.value.__cause__.__context__ is cleanup_error
                    with pytest.raises(NativeError) as again:
                        await submitted.get_result()
                    assert again.value is caught.value
                    assert session._reader.done()
                    assert not home.exists()
                    cancellation_observed = True
                finally:
                    if closing is not None and not closing.done():
                        await closing
    assert normal_exit.value is cleanup_error
    assert cancellation_observed
    assert responses.requests == []


@pytest.mark.parametrize("reader_exit", ["error", "eof"])
@pytest.mark.parametrize("phase", ["new", "completed", "active"])
@pytest.mark.usefixtures("core_owner")
async def test_reader_exit_retires_session_and_preserves_exact_handles(
    responses: ResponsesFixture,
    monkeypatch: pytest.MonkeyPatch,
    reader_exit: str,
    phase: str,
) -> None:
    from agents import _native

    reader_blocked = asyncio.Event()
    reader_release = asyncio.Event()
    reader_error = OSError("synthetic native event reader failure")
    fail_next_read = phase != "completed"
    original_next = _native.NativeThread._next_session_event

    async def next_event(native_thread: Any) -> str:
        nonlocal fail_next_read
        if fail_next_read:
            reader_blocked.set()
            await reader_release.wait()
            if reader_exit == "error":
                raise reader_error
            return "null"
        raw = await original_next(native_thread)
        event = json.loads(raw)
        if event is not None and event["msg"]["type"] in {
            "task_complete",
            "turn_complete",
            "turn_aborted",
        }:
            fail_next_read = True
        return raw

    # Opening can consume every startup event before publishing the Session.
    # Intercept before its sole reader starts, without replacing an in-flight read.
    # Only event delivery fails; submission, history and cleanup remain native.
    monkeypatch.setattr(_native.NativeThread, "_next_session_event", next_event)
    handle = None
    completed = None
    failure = None
    agent = Agent(instructions="Answer briefly.", provider=provider(responses))
    async with timeout(30), agent.session() as session:
        home = Path(session._home.name)
        try:
            if phase != "new":
                responses.respond(assistant_output("retained native result"))
                handle = await session.submit("first input")
                if phase == "completed":
                    completed = await (handle).get_result()
                    assert completed.final_output == "retained native result"
                else:
                    while True:
                        turns = [entry async for entry in session.turns()]
                        if (
                            turns
                            and turns[-1]["status"] == "completed"
                            and handle.turn_id is not None
                        ):
                            assert turns[-1]["id"] == handle.turn_id
                            break
                        await asyncio.sleep(0.01)
            await reader_blocked.wait()
            requests_before_failure = len(responses.requests)
            reader_release.set()
            await session._reader
            if phase == "active":
                assert handle is not None
                expected = OSError if reader_exit == "error" else NativeError
                with pytest.raises(expected) as caught:
                    await (handle).get_result()
                failure = caught.value
                if reader_exit == "error":
                    assert failure is reader_error
                else:
                    assert "event stream ended before settlement" in str(failure)
            with pytest.raises(RuntimeError, match="cannot continue"):
                await session.submit("must not reach the provider")
            assert len(responses.requests) == requests_before_failure
            assert requests_before_failure == (0 if phase == "new" else 1)
        finally:
            reader_release.set()
    assert not home.exists()
    if completed is not None:
        assert handle is not None
        assert await (handle).get_result() is completed
    if failure is not None:
        assert handle is not None
        with pytest.raises(type(failure)) as repeated:
            await (handle).get_result()
        assert repeated.value is failure


async def test_concurrent_close_joins_cleanup_without_owning_application_consumer(
    responses: ResponsesFixture,
) -> None:
    entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleaned = asyncio.Event()
    consumer_started = asyncio.Event()
    consumer_settled = asyncio.Event()
    consumer_release = asyncio.Event()
    passive_close_started = asyncio.Event()

    @function_tool
    async def pending() -> str:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleanup_started.set()
            await cleanup_release.wait()
            cleaned.set()
        return "unreachable"

    responses.respond(function_call("pending", {}, "concurrent-close"))
    agent = Agent(
        instructions="Use the pending tool.",
        tools=[pending],
        provider=provider(responses),
    )
    tasks: list[asyncio.Task[Any]] = []
    async with timeout(30), agent.session() as session:
        try:
            handle = await session.submit("start")
            await entered.wait()

            async def consume() -> str:
                consumer_started.set()
                with pytest.raises(TurnStoppedError):
                    await (handle).get_result()
                consumer_settled.set()
                await consumer_release.wait()
                return "application-finished"

            async def passive_close() -> None:
                passive_close_started.set()
                await session._aclose()

            consumer = asyncio.create_task(consume())
            tasks.append(consumer)
            await consumer_started.wait()
            first_close = asyncio.create_task(session._aclose())
            tasks.append(first_close)
            await cleanup_started.wait()
            cancelled_close = asyncio.create_task(passive_close())
            tasks.append(cancelled_close)
            await passive_close_started.wait()
            cancelled_close.cancel()
            # Give the passive caller a turn to receive its cancellation.
            await asyncio.sleep(0)
            assert not first_close.done()
            assert not cancelled_close.done()
            assert not cleaned.is_set()

            cleanup_release.set()
            await first_close
            with pytest.raises(asyncio.CancelledError):
                await cancelled_close
            assert cleaned.is_set()
            await consumer_settled.wait()
            assert not consumer.done()
            with pytest.raises(RuntimeError, match="closed"):
                await session.submit("after-close")
            consumer_release.set()
            assert await consumer == "application-finished"
        finally:
            cleanup_release.set()
            consumer_release.set()
            try:
                await session._aclose()
            finally:
                await asyncio.gather(*tasks, return_exceptions=True)


async def test_native_interrupt_failure_joins_callback_cleanup_and_retires_session(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleaned = asyncio.Event()
    interruption_error = RuntimeError("synthetic native interruption failure")

    @function_tool
    async def pending() -> str:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleanup_started.set()
            await cleanup_release.wait()
            cleaned.set()
        return "unreachable"

    responses.respond(function_call("pending", {}, "failed-interrupt"))
    agent = Agent(
        instructions="Use the pending tool.",
        tools=[pending],
        provider=provider(responses),
    )
    cancellation = None
    async with timeout(30), agent.session() as session:
        native_thread = session._native

        class InterruptFailure:
            def __getattr__(self, name: str) -> Any:
                return getattr(native_thread, name)

            async def _interrupt_session(self, expected_turn_id) -> None:
                raise interruption_error

        # Inject only the failing boundary; the engine, reader, and close stay native.
        monkeypatch.setattr(session, "_native", InterruptFailure())
        try:
            handle = await session.submit("start")
            await entered.wait()
            cancellation = asyncio.create_task(session._cancel(handle))
            await cleanup_started.wait()
            assert not cancellation.done()
            assert not cleaned.is_set()
            cleanup_release.set()
            with pytest.raises(NativeError, match="cancellation failed") as caught:
                await cancellation
            assert caught.value.__cause__ is interruption_error
            assert cleaned.is_set()
            with pytest.raises(NativeError):
                await (handle).get_result()
            with pytest.raises(RuntimeError, match="cannot continue"):
                await session.submit("must-not-run")
            assert len(responses.requests) == 1
        finally:
            cleanup_release.set()
            if cancellation is not None:
                await asyncio.gather(cancellation, return_exceptions=True)


async def test_callback_exception_preserves_error_and_session_reuses_settled_turn(
    responses: ResponsesFixture,
) -> None:
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleaned = asyncio.Event()
    callback_error = ValueError("synthetic record lookup failed: record-a")

    @function_tool
    async def failing_lookup() -> str:
        try:
            raise callback_error
        finally:
            cleanup_started.set()
            await cleanup_release.wait()
            cleaned.set()

    responses.respond(function_call("failing_lookup", {}, "callback-failure"))
    responses.respond(assistant_output("recovered"))
    agent = Agent(
        instructions="Use the lookup tool.",
        tools=[failing_lookup],
        provider=provider(responses),
    )
    waiting = None
    async with timeout(30), agent.session() as session:
        try:
            failed = await session.submit("first")
            waiting = asyncio.create_task((failed).get_result())
            await cleanup_started.wait()
            assert not waiting.done()
            cleanup_release.set()
            with pytest.raises(ValueError, match="record-a") as caught:
                await waiting
            assert caught.value is callback_error
            assert cleaned.is_set()
            recovered = await session.submit("continue after failure")
            assert (await (recovered).get_result()).final_output == "recovered"
            with pytest.raises(ValueError, match="record-a") as repeated:
                await (failed).get_result()
            assert repeated.value is callback_error
            assert len([entry async for entry in session.turns()]) == 2
            assert len(responses.requests) == 2
        finally:
            cleanup_release.set()
            if waiting is not None:
                await asyncio.gather(waiting, return_exceptions=True)


async def test_callback_child_cannot_close_until_invocation_scope_has_ended(
    responses: ResponsesFixture,
) -> None:
    detached_release = asyncio.Event()
    children: list[asyncio.Task[None]] = []
    current = None

    async def close_after_callback() -> None:
        await detached_release.wait()
        assert current is not None
        await current._aclose()

    @function_tool
    async def spawn_children() -> str:
        assert current is not None
        awaited_child = asyncio.create_task(current._aclose())
        children.append(awaited_child)
        with pytest.raises(RuntimeError, match="own Session"):
            await awaited_child
        detached_child = asyncio.create_task(close_after_callback())
        children.append(detached_child)
        return "awaited child rejected; detached child waiting"

    responses.respond(function_call("spawn_children", {}, "callback-children"))
    responses.respond(assistant_output("callback settled"))
    agent = Agent(
        instructions="Use the child task tool.",
        tools=[spawn_children],
        provider=provider(responses),
    )
    async with timeout(30), agent.session() as session:
        current = session
        try:
            handle = await session.submit("start")
            result = await (handle).get_result()
            assert result.final_output == "callback settled"
            assert len(children) == 2
            detached_child = children[1]
            assert not detached_child.done()
            detached_release.set()
            await detached_child
            assert await (handle).get_result() is result
            with pytest.raises(RuntimeError, match="closed"):
                await session.submit("after detached close")
        finally:
            detached_release.set()
            await asyncio.gather(*children, return_exceptions=True)


@pytest.mark.usefixtures("core_owner")
async def test_session_captures_instructions_before_native_creation_suspends(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = importlib.import_module("agents._native")
    create_engine = native.create_engine
    creating = asyncio.Event()
    creation_release = asyncio.Event()

    async def gated_create_engine(*args: Any, **kwargs: Any) -> Any:
        creating.set()
        await creation_release.wait()
        return await create_engine(*args, **kwargs)

    # Suspend acquisition without replacing the native engine or its requests.
    monkeypatch.setattr(native, "create_engine", gated_create_engine)
    responses.respond(assistant_output("first result"))
    responses.respond(assistant_output("later result"))
    agent = Agent(instructions="Original captured instructions.", provider=provider(responses))
    async with timeout(30):
        opening = asyncio.create_task(agent.run("first"))
        try:
            await creating.wait()
            agent = replace(agent, instructions="Updated instructions for future sessions.")
            creation_release.set()
            assert (await opening).final_output == "first result"
            first_input = str(responses.requests[0]["input"])
            assert "Original captured instructions." in first_input
            assert "Updated instructions for future sessions." not in first_input

            assert (await agent.run("later")).final_output == "later result"
            later_input = str(responses.requests[1]["input"])
            assert "Updated instructions for future sessions." in later_input
            assert "Original captured instructions." not in later_input
        finally:
            creation_release.set()
            await asyncio.gather(opening, return_exceptions=True)


async def test_native_admission_rejection_prevents_validator_and_callback_effects(
    responses: ResponsesFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    effects: list[str] = []

    class Payload(BaseModel):
        value: str

        @field_validator("value")
        @classmethod
        def observe_validation(cls, value: str) -> str:
            effects.append("validator")
            return value

    @function_tool
    async def lookup(payload: Payload) -> str:
        effects.append("callback")
        return payload.value

    responses.respond(
        function_call("lookup", {"payload": {"value": "record-a"}}, "rejected-admission")
    )
    agent = Agent(instructions="Use the lookup tool.", tools=[lookup], provider=provider(responses))
    async with timeout(30), agent.session() as session:
        native_thread = session._native

        class StaleAdmission:
            def __getattr__(self, name: str) -> Any:
                return getattr(native_thread, name)

            def _check_python_tool(self, raw: str) -> None:
                native_thread._check_python_tool(raw)
                stale = json.loads(raw)
                stale["_sdk_python_tool"]["generation"] += 1
                native_thread._check_python_tool(json.dumps(stale))

        # Inject stale identity only at admission; the real native gate rejects it.
        monkeypatch.setattr(session, "_native", StaleAdmission())
        handle = await session.submit("start")
        with pytest.raises(RuntimeError, match="^Expired or invalid managed Python tool request$"):
            await (handle).get_result()
        assert effects == []
        assert len(responses.requests) == 1


@pytest.mark.parametrize("operation", ["cancel", "aclose"])
async def test_cancel_before_queued_callback_dispatch_prevents_application_effects(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    effects: list[str] = []
    response_release = asyncio.Event()
    cancellation_ready = asyncio.Event()
    cancellation_requested = asyncio.Event()

    class Payload(BaseModel):
        value: str

        @field_validator("value")
        @classmethod
        def observe_validation(cls, value: str) -> str:
            effects.append("validator")
            return value

    @function_tool
    async def lookup(payload: Payload) -> str:
        effects.append("callback")
        return payload.value

    async def hold_tool_response(event: dict[str, Any]) -> None:
        if event["type"] == "response.output_item.done":
            await response_release.wait()

    responses.before_event = hold_tool_response
    responses.respond(
        function_call("lookup", {"payload": {"value": "record-a"}}, "cancel-before-dispatch")
    )
    agent = Agent(instructions="Use the lookup tool.", tools=[lookup], provider=provider(responses))
    cancellation = None
    async with timeout(30), agent.session() as session:
        native_thread = session._native

        class CancelOnNativeRequest:
            def __getattr__(self, name: str) -> Any:
                return getattr(native_thread, name)

            async def _next_session_event(self) -> str:
                raw = await native_thread._next_session_event()
                event = json.loads(raw)
                if event is not None and event["msg"]["type"] == "dynamic_tool_call_request":
                    # Wake the public cancel caller before the reader queues dispatch.
                    cancellation_requested.set()
                return raw

        # Preserve the genuine event, its identity, and all native admission checks.
        monkeypatch.setattr(session, "_native", CancelOnNativeRequest())
        try:
            handle = await session.submit("start")

            async def cancel_when_requested() -> None:
                cancellation_ready.set()
                await cancellation_requested.wait()
                if operation == "cancel":
                    await session._cancel(handle)
                else:
                    await session._aclose()

            cancellation = asyncio.create_task(cancel_when_requested())
            await cancellation_ready.wait()
            response_release.set()
            await cancellation
            with pytest.raises(TurnStoppedError):
                await (handle).get_result()
            assert effects == []
            assert len(responses.requests) == 1
        finally:
            response_release.set()
            cancellation_requested.set()
            if cancellation is not None:
                await asyncio.gather(cancellation, return_exceptions=True)


async def test_cancel_joins_callback_cleanup_without_serializing_returned_result(
    responses: ResponsesFixture,
) -> None:
    entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleaned = asyncio.Event()
    serialized: list[str] = []

    class Result(BaseModel):
        value: str

        @model_serializer
        def serialize(self) -> dict[str, str]:
            serialized.append(self.value)
            return {"value": self.value}

    @function_tool
    async def pending() -> Result:
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cleanup_started.set()
            await cleanup_release.wait()
            cleaned.set()
            return Result(value="partial result")
        raise AssertionError("The pending tool should only return after cancellation")

    responses.respond(function_call("pending", {}, "cancel-before-serialization"))
    responses.respond(assistant_output("after cancellation"))
    agent = Agent(
        instructions="Use the pending tool.", tools=[pending], provider=provider(responses)
    )
    cancellation = None
    async with timeout(30), agent.session() as session:
        try:
            handle = await session.submit("start")
            await entered.wait()
            cancellation = asyncio.create_task(session._cancel(handle))
            await cleanup_started.wait()
            assert not cancellation.done()
            assert not cleaned.is_set()
            cleanup_release.set()
            await cancellation
            assert cleaned.is_set()
            assert serialized == []
            with pytest.raises(TurnStoppedError):
                await (handle).get_result()
            recovered = await session.submit("continue")
            assert (await (recovered).get_result()).final_output == "after cancellation"
            with pytest.raises(TurnStoppedError):
                await (handle).get_result()
            assert len(responses.requests) == 2
        finally:
            cleanup_release.set()
            if cancellation is not None:
                await asyncio.gather(cancellation, return_exceptions=True)


@pytest.mark.usefixtures("core_owner")
async def test_reused_native_call_id_fails_exact_handle_and_releases_turn(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    admitted: list[tuple[str, str]] = []
    second_admitted = asyncio.Event()

    @function_tool
    async def lookup(key: str) -> str:
        calls.append(key)
        return key

    responses.respond(function_call("lookup", {"key": "first"}, "reused-call"))
    responses.respond(function_call("lookup", {"key": "second"}, "reused-call"))
    responses.respond(assistant_output("recovered"))
    agent = Agent(instructions="Use the lookup tool.", tools=[lookup], provider=provider(responses))
    async with timeout(30), agent.session() as session:
        native_thread = session._native

        class ObserveNativeRequests:
            def __getattr__(self, name: str) -> Any:
                return getattr(native_thread, name)

            async def _next_session_event(self) -> str:
                raw = await native_thread._next_session_event()
                event = json.loads(raw)
                if event is not None and event["msg"]["type"] == "dynamic_tool_call_request":
                    message = event["msg"]
                    native_thread._check_python_tool(json.dumps(message))
                    admitted.append((message["callId"], message["arguments"]["key"]))
                    if len(admitted) == 2:
                        second_admitted.set()
                return raw

        # Observe both genuine pending requests without altering native admission.
        monkeypatch.setattr(session, "_native", ObserveNativeRequests())
        failed = await session.submit("start")
        await second_admitted.wait()
        with pytest.raises(NativeError, match="reused-call") as caught:
            await (failed).get_result()
        assert admitted == [("reused-call", "first"), ("reused-call", "second")]
        assert calls == ["first"]
        assert len(responses.requests) == 2

        recovered = await session.submit("continue after reused call")
        assert recovered.turn_id != failed.turn_id
        assert (await (recovered).get_result()).final_output == "recovered"
        with pytest.raises(NativeError, match="reused-call") as repeated:
            await (failed).get_result()
        assert repeated.value is caught.value
        assert calls == ["first"]
        assert len(responses.requests) == 3


async def test_ordinary_reused_call_id_fails_exact_handle_and_releases_turn(
    responses: ResponsesFixture,
) -> None:
    calls: list[str] = []

    @function_tool
    async def lookup(key: str) -> str:
        calls.append(key)
        return key

    responses.respond(function_call("lookup", {"key": "first"}, "reused-call"))
    responses.respond(function_call("lookup", {"key": "second"}, "reused-call"))
    responses.respond(assistant_output("recovered"))
    agent = Agent(instructions="Use the lookup tool.", tools=[lookup], provider=provider(responses))
    async with timeout(30), agent.session() as session:
        failed = await session.submit("start")
        with pytest.raises(NativeError, match="reused-call") as caught:
            await (failed).get_result()
        assert calls == ["first"]
        assert len(responses.requests) == 2

        recovered = await session.submit("continue after reused call")
        assert recovered.turn_id != failed.turn_id
        assert (await (recovered).get_result()).final_output == "recovered"
        with pytest.raises(NativeError, match="reused-call") as repeated:
            await (failed).get_result()
        assert repeated.value is caught.value
        assert calls == ["first"]
        assert len(responses.requests) == 3


@pytest.mark.parametrize("operation", ["cancel", "aclose", "body"])
@pytest.mark.usefixtures("core_owner")
async def test_callback_cleanup_errors_survive_stopping(
    responses: ResponsesFixture, operation: str
) -> None:
    entered = asyncio.Event()
    cleaned = asyncio.Event()
    cleanup_error = OSError("tool cleanup failed")
    body_error = ValueError("application body failed")

    @function_tool
    async def pending() -> str:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleaned.set()
            raise cleanup_error

    responses.respond(function_call("pending", {}, "cleanup-failure"))
    agent = Agent(instructions="Use the tool.", tools=[pending], provider=provider(responses))
    expected = (
        NativeError if operation == "cancel" else ValueError if operation == "body" else OSError
    )
    with pytest.raises(expected) as caught:
        async with timeout(30), agent.session() as session:
            home = Path(session._home.name)
            handle = await session.submit("start")
            await entered.wait()
            if operation == "cancel":
                await session._cancel(handle)
            elif operation == "aclose":
                await session._aclose()
            else:
                raise body_error
    if operation == "cancel":
        assert caught.value.__cause__ is cleanup_error
    elif operation == "body":
        assert caught.value is body_error
        assert body_error.__context__ is cleanup_error
    else:
        assert caught.value is cleanup_error
    with pytest.raises(NativeError) as stopped:
        await (handle).get_result()
    assert stopped.value.effects == "unknown"
    assert stopped.value.__cause__.__context__ is cleanup_error
    assert cleaned.is_set()
    assert session._closed and session._reader.done()
    assert not home.exists()
    assert len(responses.requests) == 1


@pytest.mark.parametrize("repeated", [False, True])
@pytest.mark.usefixtures("core_owner")
async def test_cancelled_cancel_retains_callback_cleanup_failure(
    responses: ResponsesFixture, repeated: bool
) -> None:
    entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleanup_error = OSError("tool cleanup failed after caller cancellation")

    def retains_cleanup_error(initial_error: BaseException) -> bool:
        pending_errors = [initial_error]
        seen: set[int] = set()
        while pending_errors:
            error = pending_errors.pop()
            if id(error) in seen:
                continue
            seen.add(id(error))
            for linked in (error.__cause__, error.__context__):
                if linked is not None:
                    pending_errors.append(linked)
        return id(cleanup_error) in seen

    @function_tool
    async def pending() -> str:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleanup_started.set()
            await cleanup_release.wait()
            raise cleanup_error

    responses.respond(function_call("pending", {}, "cancelled-cancel-cleanup"))
    agent = Agent(instructions="Use the tool.", tools=[pending], provider=provider(responses))
    async with timeout(30), agent.session() as session:
        home = Path(session._home.name)
        handle = await session.submit("start")
        await entered.wait()
        cancelling = asyncio.create_task(session._cancel(handle))
        try:
            await cleanup_started.wait()
            cancelling.cancel("first cancellation")
            await asyncio.sleep(0)
            if repeated:
                cancelling.cancel("second cancellation")
                await asyncio.sleep(0)
            assert not cancelling.done()
            cleanup_release.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await cancelling
            cancellation = task_cancellation(caught.value)
            assert cancellation.args == ("first cancellation",)
            assert retains_cleanup_error(cancellation)
            with pytest.raises(NativeError) as stopped:
                await (handle).get_result()
            assert stopped.value.effects == "unknown"
            assert retains_cleanup_error(stopped.value)
        finally:
            cleanup_release.set()
            await asyncio.gather(cancelling, return_exceptions=True)
    assert session._closed and session._reader.done()
    assert not home.exists()
    assert len(responses.requests) == 1


@pytest.mark.usefixtures("core_owner")
async def test_failed_home_cleanup_allows_internal_close_retry(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        timeout(30),
        Agent(instructions="Answer.", provider=provider(responses)).session() as session,
    ):
        home = Path(session._home.name)
        native_home = session._home
        cleanup = native_home.cleanup
        cleanup_error = OSError("transient home removal failure")
        attempts = 0

        def fail_once() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise cleanup_error
            cleanup()

        class FailingHome:
            name = str(home)

            def cleanup(self) -> None:
                fail_once()

        monkeypatch.setattr(session, "_home", FailingHome())
        try:
            with pytest.raises(OSError) as caught:
                await session._aclose()
            assert caught.value is cleanup_error
            assert not session._closed and home.exists()
            with pytest.raises(RuntimeError, match="closed"):
                await session.submit("blocked")
            await session._aclose()
            assert attempts == 2
            assert session._closed and not home.exists()
        finally:
            monkeypatch.setattr(session, "_home", native_home)
            if home.exists():
                cleanup()  # Failed-test teardown only; the success assertions precede this.
