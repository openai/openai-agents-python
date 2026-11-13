"""Pure boundary checks for form preparation and complete cleanup ownership."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from _agents_codex._lifecycle import _sync_run, _SyncRun
from _agents_codex._queued_roots import _QueuedRoots
from _agents_codex.session import _EnvironmentLease
from agents import FormRequest, Session, UrlRequest
from agents.manual import _question


class QuestionSession:
    id = "synthetic-session"
    _native_agents = None


def test_form_reply_uses_complete_schema_and_captures_detached_json() -> None:
    session = QuestionSession()
    request = _question(
        session,
        {
            "type": "elicitation_request",
            "id": "synthetic-form",
            "server_name": "records",
            "turn_id": "turn",
            "request": {
                "mode": "form",
                "message": "Count",
                "requested_schema": {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "allOf": [{"properties": {"count": {"minimum": 2}}}],
                    "required": ["count"],
                    "additionalProperties": False,
                },
            },
        },
        SimpleNamespace(turn_id="turn", submission_id="submission"),
    )
    assert isinstance(request, FormRequest)
    assert request.mode == "form"
    assert not hasattr(request, "accept_url")
    for invalid in ({}, {"count": 1}, {"count": True}, {"count": 3, "extra": "private"}):
        with pytest.raises(ValueError, match="MCP form failed validation"):
            request.reply(invalid)
    values = {"count": 3}
    reply = request.reply(values)
    values["count"] = 1
    assert reply._value == {"count": 3}
    assert reply._request is request
    assert request.reply(None)._value is None
    assert not hasattr(UrlRequest, "respond")


async def cleanup_session(events, *, managed_environment=True):
    @asynccontextmanager
    async def environment():
        task = asyncio.current_task()
        events.append(("enter", task))
        try:
            yield object()
        finally:
            events.append(("exit", asyncio.current_task()))
            assert asyncio.current_task() is task

    lease = _EnvironmentLease(environment()) if managed_environment else None
    if lease is not None:
        await lease.acquire()
    session = object.__new__(Session)
    session._loop = asyncio.get_running_loop()
    session._close_task = None
    session._configured_capture = None
    session._scope_close_task = None
    session._native_scope_failure = None
    session._sync_cleanup_owner = _sync_run.get()
    session._sync_cleanup_deferred = False
    session._interaction_error = None
    session._environment_lease = lease
    session._resources_retired = False
    session._closed = False
    session._last_turn = None
    session._queued_roots = _QueuedRoots(lambda: None)
    session._observations = SimpleNamespace(notify=lambda: None)
    session._provider_lease = SimpleNamespace(release=lambda: events.append(("provider", None)))
    session._profile_provider_leases = {}
    session._profile_provider_keys = {}
    session._owned_provider = None
    session._repair_session = None
    session._model_context_snapshots = {}
    session._managed_tool_streams = {}
    session._approval_ready = {}
    session._profiles = {}
    session._profile_catalogs = {}
    session._catalog_bindings = {}
    failed = False

    async def retire():
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("synthetic transient native retirement")
        session._resources_retired = True
        events.append(("native", None))

    session._close_session = retire
    return session


async def test_public_close_retries_native_then_original_environment_scope() -> None:
    events = []
    session = await cleanup_session(events)
    with pytest.raises(OSError) as failure:
        await session.aclose()
    assert failure.value.cleanup is session
    assert [event[0] for event in events] == ["enter"]
    await session.aclose()
    await session.aclose()
    assert [event[0] for event in events] == ["enter", "native", "exit", "provider"]
    assert session._closed


def test_sync_cleanup_remains_reachable_after_run_returns() -> None:
    events = []
    owner = _SyncRun()

    async def run():
        session = await cleanup_session(events)
        await session.aclose()

    with pytest.raises(OSError) as failure:
        owner.run(run())
    assert not owner.loop.is_closed()
    assert owner.deferred == 1
    asyncio.run(failure.value.cleanup.aclose())
    asyncio.run(failure.value.cleanup.aclose())
    assert owner.loop.is_closed()
    assert owner.deferred == 0
    assert [event[0] for event in events] == ["enter", "native", "exit", "provider"]


async def test_concurrent_cleanup_retry_joins_cancelled_waiter() -> None:
    events = []
    session = await cleanup_session(events)
    with pytest.raises(OSError):
        await session.aclose()
    retire = session._close_session
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_retirement():
        entered.set()
        await release.wait()
        await retire()

    session._close_session = held_retirement
    first = asyncio.create_task(session.aclose())
    second = asyncio.create_task(session.aclose())
    await entered.wait()
    first.cancel("observer stopped")
    await asyncio.sleep(0)
    assert not first.done() and not second.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert [event[0] for event in events] == ["enter", "native", "exit", "provider"]


async def test_scope_cleanup_preserves_body_error_and_exposes_original_owner(monkeypatch) -> None:
    from agents import Agent

    events = []
    session = await cleanup_session(events)

    async def open_session(*args, **kwargs):
        return session

    monkeypatch.setattr(Session, "_open", open_session)
    primary = ValueError("synthetic body failure")
    with pytest.raises(ValueError) as caught:
        async with Agent(instructions="Answer").session():
            raise primary
    assert caught.value is primary
    assert primary.cleanup is session
    assert isinstance(primary.__context__, OSError)
    await primary.cleanup.aclose()
    assert [event[0] for event in events] == ["enter", "native", "exit", "provider"]


def test_run_sync_exposes_cleanup_after_scope_exit_failure(monkeypatch) -> None:
    from agents import Agent, Completed, RunUsage

    events = []

    async def open_session(*args, **kwargs):
        session = await cleanup_session(events)
        session._interaction_handler = None

        async def result():
            usage = RunUsage(
                input_tokens=0,
                cached_input_tokens=0,
                cache_write_input_tokens=0,
                output_tokens=0,
                reasoning_output_tokens=0,
                total_tokens=0,
                requests=0,
                missing_usage_requests=0,
            )
            return Completed(
                turn=SimpleNamespace(run_usage=usage),
                final_output="synthetic completed answer",
            )

        async def submit(*args, **kwargs):
            return SimpleNamespace(get_result=result)

        session.submit = submit
        return session

    monkeypatch.setattr(Session, "_open", open_session)
    with pytest.raises(OSError) as caught:
        Agent(instructions="Answer").run_sync("Synthetic input")
    cleanup = caught.value.cleanup
    owner = cleanup._sync_cleanup_owner
    assert owner is not None and not owner.loop.is_closed()
    asyncio.run(cleanup.aclose())
    asyncio.run(cleanup.aclose())
    assert owner.loop.is_closed()
    assert [event[0] for event in events] == ["enter", "native", "exit", "provider"]


async def test_first_scope_exit_observes_failed_early_native_close() -> None:
    events = []
    session = await cleanup_session(events, managed_environment=False)
    cleanup_error = OSError("native retirement reported failure after closing")

    async def fail_after_retirement():
        session._resources_retired = True
        events.append(("native", None))
        raise cleanup_error

    session._close_session = fail_after_retirement
    with pytest.raises(OSError) as early:
        await session._aclose()
    assert early.value is cleanup_error
    assert session._closed
    with pytest.raises(OSError) as scope:
        await session.aclose()
    assert scope.value is cleanup_error
    await session.aclose()
    assert [event[0] for event in events] == ["native", "provider"]
