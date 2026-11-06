"""Deferred results respect the credential selected by each native Session."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
import sys
import traceback
import weakref
from contextlib import AsyncExitStack, asynccontextmanager
from textwrap import dedent

import pytest

from agents import Agent, NativeProvider, Session, Turn, _native
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_deferred_jobs import Journal, Output, Provider, declared, done
from tests.test_deferred_opening_outcome import debug_logs, pending
from tests.test_native_manual_tools import next_request

rotated_responses = responses


def route_default_provider(monkeypatch, url):
    # Change only the endpoint of the default descriptor so native HTTP stays on loopback.
    original = NativeProvider.__init__

    def local_provider(self, **kwargs):
        original(self, base_url=url, **kwargs)

    monkeypatch.setattr(NativeProvider, "__init__", local_provider)


def rotate_during_native_open(monkeypatch):
    def rotate(create):
        async def open_with_rotation(*args, **kwargs):
            monkeypatch.setenv("OPENAI_API_KEY", "synthetic-during-open")
            return await create(*args, **kwargs)

        return open_with_rotation

    for name in ("create_engine", "create_checkpoint_engine"):
        monkeypatch.setattr(_native, name, rotate(getattr(_native, name)))


@pytest.mark.parametrize("change", ["rotate", "remove"])
async def test_session_uses_current_process_environment(responses, change):
    # A separate interpreter keeps direct environment changes out of the test worker.
    program = dedent(
        """
        import os
        import sys
        from agents import Agent, NativeProvider

        if sys.argv[1] == "rotate":
            os.putenv("OPENAI_API_KEY", "synthetic-rotated")
        else:
            os.unsetenv("OPENAI_API_KEY")
        agent = Agent(instructions="Render.", provider=NativeProvider(base_url=sys.argv[2]))
        try:
            print(agent.run_sync("Render.").final_output)
        except RuntimeError as error:
            print(error)
        """
    )
    responses.respond(assistant_output("finished"))
    child = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", program, change, responses.url],
        env={**os.environ, "OPENAI_API_KEY": "synthetic-cached"},
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if change == "rotate":
        assert child.stdout.strip() == "finished"
        assert [row["authorization"] for row in responses.request_headers] == [
            "Bearer synthetic-rotated"
        ]
    else:
        assert child.stdout.strip() == "Provide api_key or set OPENAI_API_KEY"
        assert not responses.requests


@pytest.mark.parametrize("source", ["explicit", "padded", "environment", "default"])
async def test_active_key_in_projection_never_reaches_save_or_submit(
    responses, monkeypatch, caplog, source
):
    key = "synthetic-inner\tspace key"
    padded = "\u2003\t" + key + "\n\u00a0"
    monkeypatch.setenv(
        "OPENAI_API_KEY", padded if source in ("environment", "default") else "unused"
    )
    if source == "default":
        connection = None
        route_default_provider(monkeypatch, responses.url)
    else:
        explicit = {"explicit": key, "padded": padded, "environment": None}[source]
        connection = NativeProvider(base_url=responses.url, api_key=explicit)

    journal, backend = Journal(), Provider([done()])

    def project(artifact):
        # The journal holds an artifact reference. Its public view contains the secret.
        assert artifact["public"] == "public-report"
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-at-delivery")
        return Output(report="Report: " + key)

    jobs, tool = declared(journal, backend, project=project)
    agent = Agent(instructions="Render.", tools=[tool], provider=connection)
    async with timeout(15), agent.session() as live:
        request = await pending(live, tool, responses)
        assert all(row["authorization"] == "Bearer " + key for row in responses.request_headers)
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-before-restore")
        responses.respond(assistant_output("finished"))
        async with debug_logs(caplog, "synthetic-inner"):
            with pytest.raises(
                ValueError, match="Deferred job operation could not be completed"
            ) as caught:
                async with jobs.start(live, request) as (_, turn):
                    assert isinstance(turn, Turn)
                    await turn.get_result()
            assert caught.value.__context__ is caught.value.__cause__ is None
    (row,) = journal.rows.values()
    assert row.update is not None and row.update.status == "completed"
    assert "synthetic-inner" not in json.dumps(row.update.value)
    assert row.result is None and "save_result" not in journal.log and "deliver" not in journal.log
    assert backend.calls == [("start", 2)] and len(responses.requests) == 1
    assert "synthetic-inner" not in journal.state_json
    assert "synthetic-inner" not in json.dumps(responses.requests)
    if connection is not None:
        await connection.aclose()


async def test_wire_credential_survives_native_open_and_implicit_restore(responses, monkeypatch):
    key = "synthetic-opening-key"
    monkeypatch.setenv("OPENAI_API_KEY", key)
    route_default_provider(monkeypatch, responses.url)
    rotate_during_native_open(monkeypatch)
    entries = []

    class HookedAgent(Agent):
        @asynccontextmanager
        async def session(self, **kwargs):
            if kwargs.get("state") is not None:
                entries.append(self)
                monkeypatch.setenv("OPENAI_API_KEY", "synthetic-in-hook")
            async with super().session(**kwargs) as session:
                yield session

    jobs, tool = declared(Journal(), Provider([done()]))
    agent = HookedAgent(instructions="Render.", tools=[tool])
    async with timeout(15), agent.session() as live:
        request = await pending(live, tool, responses)
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-before-restore")
        responses.respond(assistant_output("finished"))
        async with jobs.start(live, request) as (restored, turn):
            assert isinstance(turn, Turn) and (await turn.get_result()).final_output == "finished"
            restored_provider = restored._owned_provider
            assert restored_provider is not None
            with pytest.raises(RuntimeError, match="open or opening"):
                await restored_provider.aclose()
    assert entries == [agent] and len(responses.request_headers) == 2
    assert all(row["authorization"] == "Bearer " + key for row in responses.request_headers)
    assert agent.provider is None
    with pytest.raises(RuntimeError, match="closed"):
        async with agent.session(provider=restored_provider):
            pytest.fail("restored implicit provider leaked")


async def test_concurrent_explicit_restore_uses_current_provider(responses, rotated_responses):
    original = NativeProvider(base_url=responses.url, api_key="synthetic-original")
    current = NativeProvider(base_url=rotated_responses.url, api_key="synthetic-rotated")
    jobs, tool = declared(Journal(), Provider([done()]))
    entering = False

    class HookedAgent(Agent):
        @asynccontextmanager
        async def session(self, **kwargs):
            nonlocal entering
            state = kwargs.get("state")
            if state is not None and not entering:
                entering = True
                checkpoint = SessionState.from_json(state.to_json())
                await asyncio.create_task(restore_explicitly(checkpoint))
            async with super().session(**kwargs) as session:
                yield session

    async def restore_explicitly(state):
        async with agent.session(state=state) as session:
            request = await next_request(session.interactions(), tool)
            await request.respond(Output(report="manual"))
            assert (await session.restored_turn.get_result()).final_output == "explicit"

    agent = HookedAgent(instructions="Open.", tools=[tool], provider=current)
    async with timeout(15), agent.session(provider=original) as live:
        request = await pending(live, tool, responses)
        responses.respond(assistant_output("implicit"))
        rotated_responses.respond(assistant_output("explicit"))
        async with jobs.start(live, request) as (_, turn):
            assert isinstance(turn, Turn) and (await turn.get_result()).final_output == "implicit"
    assert [row["authorization"] for row in responses.request_headers] == [
        "Bearer synthetic-original",
        "Bearer synthetic-original",
    ]
    assert [row["authorization"] for row in rotated_responses.request_headers] == [
        "Bearer synthetic-rotated"
    ]
    await original.aclose()
    await current.aclose()


async def test_explicit_restore_rechecks_saved_bytes_with_its_current_key(responses, monkeypatch):
    report = "previously-public-report"
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-first-key")
    journal, backend = Journal(), Provider([done(report)])
    jobs, tool = declared(journal, backend)
    agent = Agent(
        instructions="Render.", tools=[tool], provider=NativeProvider(base_url=responses.url)
    )
    journal.forbidden.add("deliver")
    async with timeout(15), agent.session() as live:
        request = await pending(live, tool, responses)
        with pytest.raises(PermissionError):
            async with jobs.start(live, request):
                pytest.fail("Delivery was denied")
    (row,) = journal.rows.values()
    saved = row.result
    assert saved is not None and json.loads(saved) == {"report": report}
    journal.forbidden.clear()
    monkeypatch.setenv("OPENAI_API_KEY", report)
    async with (
        timeout(15),
        agent.session(state=SessionState.from_json(journal.state_json)) as restored,
    ):
        request = await next_request(restored.interactions(), tool)
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-after-restore")
        responses.respond(assistant_output("finished"))
        with pytest.raises(ValueError, match="Deferred job operation could not be completed"):
            outcome = await jobs.advance(restored, request)
            assert isinstance(outcome, Turn)
            await outcome.get_result()
    assert row.result == saved and journal.log.count("save_result") == 1
    assert journal.log[-1] == "claim-deliver" and backend.calls == [("start", 2)]
    assert len(responses.requests) == 1
    assert agent.provider is not None
    await agent.provider.aclose()


@pytest.mark.parametrize(
    ("explicit", "ambient", "message"),
    [
        (None, None, "Provide api_key or set OPENAI_API_KEY"),
        (None, " \u3000\t", "Provide api_key or set OPENAI_API_KEY"),
        (" \u3000\t", "synthetic-fallback", "api_key must not be empty"),
    ],
    ids=["missing", "blank-environment", "blank-explicit"],
)
async def test_missing_credential_diagnostics(responses, monkeypatch, explicit, ambient, message):
    if ambient is None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    else:
        monkeypatch.setenv("OPENAI_API_KEY", ambient)
    provider = NativeProvider(base_url=responses.url, api_key=explicit)
    with pytest.raises(RuntimeError, match=message):
        async with Agent(instructions="Render.", provider=provider).session():
            pytest.fail("Opened without a usable credential")
    assert not responses.requests
    await provider.aclose()


@pytest.mark.skipif(not os.supports_bytes_environ, reason="Needs a byte-oriented environment")
async def test_non_utf8_environment_key_is_absent(responses, monkeypatch):
    monkeypatch.setitem(os.environb, b"OPENAI_API_KEY", b"synthetic-\xff")
    with pytest.raises(RuntimeError, match="Provide api_key or set OPENAI_API_KEY"):
        async with Agent(
            instructions="Render.", provider=NativeProvider(base_url=responses.url)
        ).session():
            pytest.fail("Opened with invalid environment text")


async def test_missing_credential_cannot_be_selected_during_native_open(responses, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    route_default_provider(monkeypatch, responses.url)
    rotate_during_native_open(monkeypatch)
    agent = Agent(instructions="Render.")
    with pytest.raises(RuntimeError, match="Provide api_key or set OPENAI_API_KEY"):
        async with agent.session():
            pytest.fail("Adopted a credential set after opening began")
    assert agent.provider is None


async def test_native_trim_keeps_non_whitespace_controls(responses, monkeypatch):
    # Rust keeps the record separator. Opening validates presence before any HTTP request.
    monkeypatch.setenv("OPENAI_API_KEY", " \x1c\t")
    provider = NativeProvider(base_url=responses.url)
    async with Agent(instructions="Render.", provider=provider).session():
        pass
    assert not responses.requests
    await provider.aclose()


def assert_private_traceback(error, marker, owner="_open"):
    frames = [
        (frame, line)
        for frame, line in traceback.walk_tb(error.__traceback__)
        if frame.f_globals["__name__"].startswith(("agents.", "_agents_codex."))
    ]
    assert owner in {frame.f_code.co_name for frame, _line in frames}
    captured = traceback.StackSummary.extract(frames, capture_locals=True)
    assert marker not in "".join(captured.format())


@pytest.mark.parametrize("explicit", [True, False], ids=["explicit", "environment"])
async def test_closed_provider_keeps_key_out_of_open_traceback(monkeypatch, explicit):
    marker = "synthetic-closed-session-key"
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-unused" if explicit else marker)
    provider = NativeProvider(api_key=marker if explicit else None)
    await provider.aclose()
    with pytest.raises(RuntimeError, match="^NativeProvider is closed$") as caught:
        async with Agent(instructions="Open.", provider=provider).session():
            pytest.fail("Closed provider opened")
    assert_private_traceback(caught.value, marker)


@pytest.mark.parametrize("invalid", ["\udc80", "\udc80\udc81"], ids=["character", "range"])
async def test_invalid_key_encoding_keeps_input_out_of_native_error(invalid):
    marker = "synthetic-invalid-session-key"
    key = marker + invalid + marker
    provider = NativeProvider(api_key=key)
    with pytest.raises(UnicodeEncodeError) as expected:
        key.encode()
    with pytest.raises(UnicodeEncodeError) as caught:
        async with Agent(instructions="Open.", provider=provider).session():
            pytest.fail("Invalid credential opened a session")
    error = caught.value
    assert str(error) == str(expected.value)
    assert marker not in error.object and marker not in repr(error.args)
    if len(invalid) > 1:
        assert invalid not in error.object
    assert error.__context__ is error.__cause__ is None
    assert_private_traceback(error, marker)
    await provider.aclose()


@pytest.fixture
def tracked_key(monkeypatch):
    # Native returns a plain string. A weak reference observes its Python lifetime.
    class Key(str):
        pass

    marker = "synthetic-selected-session-key"
    monkeypatch.setenv("OPENAI_API_KEY", marker)
    read = _native._read_openai_api_key_from_env
    refs = []

    def read_key():
        key = Key(read())
        refs.append(weakref.ref(key))
        return key

    monkeypatch.setattr(_native, "_read_openai_api_key_from_env", read_key)
    return marker, refs


async def test_unpublished_session_releases_selected_key(monkeypatch, tracked_key):
    marker, refs = tracked_key

    def fail(session):
        # A profiler may materialize the opening frame's locals without keeping its own copy.
        len(sys._getframe(1).f_locals)
        raise RuntimeError("Reader startup failed")

    monkeypatch.setattr(Session, "_start_readers", fail)
    agent = Agent(instructions="Open.")
    with pytest.raises(RuntimeError, match="Reader startup failed") as caught:
        async with agent.session():
            pytest.fail("Unpublished session entered")
    assert len(refs) == 1 and refs[0]() is None
    assert_private_traceback(caught.value, marker)
    assert agent.provider is None


async def test_cancelled_native_open_releases_selected_key(monkeypatch, tracked_key):
    marker, refs = tracked_key
    create = _native.create_engine

    def cancel_after_submission(*args, **kwargs):
        operation = create(*args, **kwargs)
        asyncio.current_task().cancel("Interrupted opening")
        return operation

    monkeypatch.setattr(_native, "create_engine", cancel_after_submission)
    agent = Agent(instructions="Open.")

    async def open_session():
        with pytest.raises(asyncio.CancelledError, match="Interrupted opening") as caught:
            async with agent.session():
                pytest.fail("Cancelled session entered")
        return caught.value

    error = await asyncio.create_task(open_session())
    assert_private_traceback(error, marker)
    assert len(refs) == 1 and refs[0]() is None
    assert agent.provider is None


@pytest.mark.parametrize(
    ("phase", "error_type", "message"),
    [
        ("prepare", PermissionError, "Deferred checkpoint preparation was denied"),
        ("task", RuntimeError, "Restore task unavailable"),
        ("restore", RuntimeError, "NativeProvider is closed"),
    ],
)
async def test_failed_deferred_start_releases_opening_key(
    responses, tracked_key, phase, error_type, message
):
    marker, refs = tracked_key
    loop = asyncio.get_running_loop()
    original_factory = loop.get_task_factory()
    rejected = []

    def refuse(loop, work, **kwargs):
        # Other I/O tasks may start after prepare. Reject only the restore owner.
        if work.__qualname__ != "_restored_session.<locals>.own":
            if original_factory is not None:
                return original_factory(loop, work, **kwargs)
            return asyncio.Task(work, loop=loop, **kwargs)
        loop.set_task_factory(original_factory)
        rejected.append(work)
        raise RuntimeError("Restore task unavailable")

    class Store(Journal):
        async def prepare(self, state_json, bindings):
            await super().prepare(state_json, bindings)
            if phase == "task":
                loop.set_task_factory(refuse)

    journal, backend = Store(), Provider([])
    jobs, tool = declared(journal, backend)

    class HookedAgent(Agent):
        @asynccontextmanager
        async def session(self, **kwargs):
            if kwargs.get("state") is not None:
                await live_scope.aclose()
                await self.provider.aclose()
            async with super().session(**kwargs) as session:
                yield session

    agent = HookedAgent(
        instructions="Open.", tools=[tool], provider=NativeProvider(base_url=responses.url)
    )
    if phase == "prepare":
        journal.forbidden.add("prepare")
    async with timeout(15), AsyncExitStack() as live_scope:
        live = await live_scope.enter_async_context(agent.session())
        request = await pending(live, tool, responses)
        try:
            with pytest.raises(error_type, match=f"^{message}$") as caught:
                async with jobs.start(live, request):
                    pytest.fail("Failed recovery entered")
        finally:
            loop.set_task_factory(original_factory)
        if phase == "task":
            (work,) = rejected
            assert inspect.getcoroutinestate(work) == inspect.CORO_CLOSED
        assert len(refs) == 1 and refs[0]() is None
        assert_private_traceback(caught.value, marker, "start")
    assert not backend.calls and len(responses.requests) == 1
    await agent.provider.aclose()
