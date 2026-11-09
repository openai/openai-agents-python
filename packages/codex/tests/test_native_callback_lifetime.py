"""Native callback retirement joins only the exact application invocation."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from _agents_codex.session import Session, _call_callback, _Submission
from tests._asyncio import timeout


def callback_owner():
    released = []
    session = object.__new__(Session)
    session._native = cast(
        Any,
        SimpleNamespace(
            id="thread",
            _release_python_callback=lambda raw: released.append(json.loads(raw)) or True,
            _enter_python_callback=lambda raw: True,
            _python_callback_cancelled=lambda raw: False,
            _check_step=lambda *args: None,
        ),
    )
    state = _Submission(output=cast(Any, None))
    session._active = state
    session._child_callbacks = {}
    return session, state, released


def receipt(invocation: str):
    return {
        "kind": "tool",
        "request": {
            "_sdk_python_tool": {"thread": "thread", "generation": 1, "slot": 0},
            "turnId": "root",
            "callId": "call",
        },
        "invocation": invocation,
    }


async def test_exact_callback_cancel_waits_for_cleanup_and_preserves_sibling():
    session, state, released = callback_owner()
    entered, cleaning, release, sibling_release = (asyncio.Event() for _ in range(4))

    async def callback():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    async def sibling_callback():
        await sibling_release.wait()

    task = session._start_native_callback(state, callback(), receipt("1"))
    sibling = session._start_native_callback(state, sibling_callback(), receipt("2"))
    try:
        async with timeout(5):
            await entered.wait()
            session._cancel_native_callback(receipt("1"))
            await cleaning.wait()
            session._cancel_native_callback(receipt("1"))
            await asyncio.sleep(0)
            assert released == []
            assert not task.done() and not sibling.done()
            assert not state.failure.done() and not state.stopping
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.sleep(0)
            assert released == [receipt("1")]
            assert not sibling.done()
            sibling_release.set()
            await sibling
            await asyncio.sleep(0)
            assert released == [receipt("1"), receipt("2")]
    finally:
        release.set()
        sibling_release.set()
        task.cancel()
        await asyncio.gather(task, sibling, return_exceptions=True)


async def test_callback_cancelled_before_entry_still_releases_receipt():
    session, state, released = callback_owner()
    entered = []

    async def callback():
        entered.append(True)

    task = session._start_native_callback(state, callback(), receipt("1"))
    session._cancel_native_callback(receipt("1"))
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    assert entered == []
    assert released == [receipt("1")]
    assert not state.failure.done()


async def test_retired_delivery_never_enters_application_callback():
    session, state, released = callback_owner()
    session._native._enter_python_callback = lambda raw: False
    effects = []

    async def callback():
        effects.append("entered")

    task = session._start_native_callback(state, callback(), receipt("1"))
    await task
    await asyncio.sleep(0)
    assert effects == []
    assert released == [receipt("1")]
    assert not state.failure.done()


@pytest.mark.parametrize("entry", ["direct", "sync", "eager"])
async def test_revocation_before_cancel_event_cannot_fail_shared_root(entry):
    session, state, released = callback_owner()
    session._closing = False
    revoked = False
    effects = []

    def check(_request):
        nonlocal revoked
        revoked = True
        raise RuntimeError("Native invocation was retired")

    session._native._check_python_tool = check
    session._native._python_callback_cancelled = lambda raw: revoked

    async def callback():
        if entry == "sync":
            await _call_callback(
                lambda: effects.append("entered"),
                lambda: session._admit_tool(state, "original request"),
            )
        elif session._admit_tool(state, "original request"):
            effects.append("entered")

    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    try:
        if entry == "eager":
            factory = getattr(asyncio, "eager_task_factory", None)
            if factory is None:
                pytest.skip("Eager task factory requires Python 3.12")
            loop.set_task_factory(factory)
        task = session._start_native_callback(state, callback(), receipt("1"))
        await task
    finally:
        loop.set_task_factory(previous_factory)
    await asyncio.sleep(0)
    assert revoked and effects == []
    assert not state.failure.done() and state.cleanup_error is None
    assert released == [receipt("1")]


async def test_callback_releases_its_issuing_native_handle():
    session, state, released = callback_owner()

    async def callback():
        pass

    task = session._start_native_callback(state, callback(), receipt("1"))
    session._native = cast(Any, SimpleNamespace())
    await task
    await asyncio.sleep(0)
    assert released == [receipt("1")]
    assert state.cleanup_error is None


async def test_scoped_preparation_cancellation_does_not_fail_shared_root():
    session, state, released = callback_owner()
    entered, cleaning, release = (asyncio.Event() for _ in range(3))
    decisions = []

    async def callback(_request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    key = {"kind": "preparation", "thread": "thread", "id": "7"}
    task = session._start_native_callback(
        state,
        session._run_owned_context_callback(
            state,
            {"step_id": 7, "thread": "thread", "instance": None},
            lambda: (callback, None),
            lambda _: decisions.append("accepted"),
            lambda: decisions.append("rejected"),
        ),
        key,
    )
    try:
        async with timeout(5):
            await entered.wait()
            session._cancel_native_callback(key)
            await cleaning.wait()
            assert released == [] and not state.failure.done()
            release.set()
            await task
            await asyncio.sleep(0)
            assert decisions == ["rejected"]
            assert released == [key]
            assert not state.failure.done() and not state.stopping
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("cancel_notice", [False, True])
async def test_reader_releases_unclaimed_callback_before_waiting_again(cancel_notice, monkeypatch):
    session, _, released = callback_owner()
    session._active = None
    session._realtime = None
    session._startup_failure = None
    session._native_agents = None
    session._sealing = True
    message = (
        {"type": "_sdk_callback_cancel", "receipt": receipt("1")}
        if cancel_notice
        else {"type": "dynamic_tool_call_request", "_sdk_callback": receipt("1")}
    )
    delivered = False

    async def next_event():
        nonlocal delivered
        if delivered:
            assert released == [receipt("1")]
            return "null"
        delivered = True
        return json.dumps({"id": "root", "msg": message})

    async def retire():
        pass

    session._native._next_session_event = next_event
    monkeypatch.setattr(session, "_retire_url_completions", retire)
    monkeypatch.setattr(session, "_finish_failed_observations", lambda: None)
    await session._read_events()
    assert released == [receipt("1")]
