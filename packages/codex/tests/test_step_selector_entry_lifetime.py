"""Delayed child step callbacks must recheck their exact native owner at entry."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from _agents_codex.session import _ChildCallbacks
from agents import ChildInvocation, StopTurn
from agents.session import Session


def owner(callback):
    state = _ChildCallbacks(
        handle=ChildInvocation(
            session_id="session",
            child_id="child-thread",
            instance_id="child-instance",
            turn_id="child-turn",
        )
    )
    key = (41, "child-thread", "child-instance")
    checks = []
    native = SimpleNamespace(live=True)

    def check(*identity):
        assert identity == key
        checks.append(identity)
        if not native.live:
            raise RuntimeError("Expired step selection")

    def reply(step_id, selected, thread, instance):
        check(step_id, thread, instance)

    native._check_step = check
    native._reply_step = reply
    session = SimpleNamespace(
        _native=native,
        _native_agents=SimpleNamespace(before_step=callback),
        _catalog=SimpleNamespace(generation=7),
        _context=None,
    )
    session._select_step_body = Session._select_step_body.__get__(session, Session)
    message = {
        "step_id": key[0],
        "thread": key[1],
        "instance": key[2],
        "generation": 7,
        "defaults": [],
        "candidates": [],
    }
    return session, state, message, native, checks


def expire(state, native, expiry):
    if expiry == "terminal":
        state.terminal.set_result({"type": "turn_complete", "_sdk_child_turn_failed": False})
    else:
        native.live = False
    # Completion deliberately stays pending while the settlement owner joins us.
    assert not state.completion.done()


@pytest.mark.parametrize("expiry", ["terminal", "nonce"])
async def test_child_step_selector_queued_worker_rechecks_owner(expiry):
    entered = []
    session, state, message, native, checks = owner(lambda *a, **kw: entered.append(True))
    loop = asyncio.get_running_loop()
    queued = asyncio.Event()
    release = Event()
    original = loop.run_in_executor
    with ThreadPoolExecutor(max_workers=1) as pool:
        blocker = pool.submit(release.wait)

        def enqueue(executor, callback, *args):
            future = original(pool, callback, *args)
            queued.set()
            return future

        with patch.object(loop, "run_in_executor", enqueue):
            task = asyncio.create_task(Session._select_step(session, state, message))
            try:
                await asyncio.wait_for(queued.wait(), 5)
                expire(state, native, expiry)
            finally:
                release.set()
            await asyncio.wait_for(task, 5)
        assert blocker.result(timeout=5)
    assert not entered
    assert checks and not state.completion.done()
    assert not state.failure.done()


@pytest.mark.parametrize("expiry", ["terminal", "nonce"])
async def test_child_step_selector_returned_coroutine_rechecks_owner(expiry):
    loop = asyncio.get_running_loop()
    entered = []
    returned = []

    async def body():
        entered.append(True)

    def select(*args, **kwargs):
        # This is queued before the executor's result notification on the same loop.
        loop.call_soon_threadsafe(expire, state, native, expiry)
        result = body()
        returned.append(result)
        return result

    session, state, message, native, checks = owner(select)
    await asyncio.wait_for(Session._select_step(session, state, message), 5)
    assert returned and returned[0].cr_frame is None
    assert not entered
    assert checks and not state.completion.done()
    assert not state.failure.done()


async def test_terminal_child_step_selector_never_enters_async_callback():
    entered = []

    async def select(*args, **kwargs):
        entered.append(True)

    session, state, message, native, checks = owner(select)
    expire(state, native, "terminal")
    await asyncio.wait_for(Session._select_step(session, state, message), 5)
    assert not entered
    assert checks and not state.completion.done()
    assert not state.failure.done()


async def test_started_child_step_selector_is_joined_after_nonce_retirement():
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = Event()
    effects = []

    def select(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=5)
        effects.append("joined")

    session, state, message, native, checks = owner(select)
    task = asyncio.create_task(Session._select_step(session, state, message))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        expire(state, native, "nonce")
        assert not task.done()
    finally:
        release.set()
    await asyncio.wait_for(task, 5)
    assert effects == ["joined"]
    assert checks and not state.completion.done()
    assert not state.failure.done()


@pytest.mark.parametrize("expiry", ["terminal", "nonce"])
async def test_late_child_step_stop_cannot_claim_retired_owner(expiry):
    # The double controls retirement after entry, which public child handles do not expose.
    entered = asyncio.Event()
    release = asyncio.Event()

    async def select(*args, **kwargs):
        entered.set()
        await release.wait()
        return StopTurn("No longer admitted")

    session, state, message, native, checks = owner(select)
    task = asyncio.create_task(Session._select_step(session, state, message))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        expire(state, native, expiry)
    finally:
        release.set()
    await asyncio.wait_for(task, 5)
    assert state.policy_stop is None
    assert not state.failure.done()
    assert checks


def settlement(session, state):
    observations = []
    session.id = state.handle.session_id
    session._observations = SimpleNamespace(notify=lambda: None, append=observations.append)
    session._retire_questions = Session._retire_questions
    session._finish_failed_observations = lambda: None

    async def stop(owner):
        owner.stopping = True

    session._stop_child_callback_owner = stop
    return observations


async def test_expired_child_step_reply_preserves_later_native_cancelled_outcome():
    effects = []
    session, state, message, native, checks = owner(lambda *a, **kw: effects.append(True))
    observations = settlement(session, state)
    loop = asyncio.get_running_loop()
    queued = asyncio.Event()
    release = Event()
    original = loop.run_in_executor
    with ThreadPoolExecutor(max_workers=1) as pool:
        blocker = pool.submit(release.wait)

        def enqueue(executor, callback, *args):
            future = original(pool, callback, *args)
            queued.set()
            return future

        with patch.object(loop, "run_in_executor", enqueue):
            task = asyncio.create_task(Session._select_step(session, state, message))
            state.callbacks.add(task)
            try:
                await asyncio.wait_for(queued.wait(), 5)
                expire(state, native, "nonce")
            finally:
                release.set()
            await asyncio.wait_for(task, 5)
        assert blocker.result(timeout=5)
    assert not state.terminal.done()  # Native retirement preceded receipt delivery.
    # Match the real turn_aborted observer after the late selector has settled.
    state.stopping = True
    state.terminal.set_result({"type": "turn_aborted"})
    await asyncio.wait_for(Session._settle_child_callbacks(session, state), 5)
    assert not effects
    assert not state.failure.done()
    assert state.completion.done()
    assert state.cleanup_error is None
    assert [(item.status, item.error_type) for item in observations] == [
        ("cancelled", "CancelledError")
    ]


async def test_expired_child_step_reply_keeps_real_callback_failure():
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = Event()
    error = LookupError("original callback failure")

    def select(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=5)
        raise error

    session, state, message, native, checks = owner(select)
    observations = settlement(session, state)
    task = asyncio.create_task(Session._select_step(session, state, message))
    state.callbacks.add(task)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        expire(state, native, "nonce")
    finally:
        release.set()
    await asyncio.wait_for(task, 5)
    state.stopping = True
    state.terminal.set_result({"type": "turn_aborted"})
    await asyncio.wait_for(Session._settle_child_callbacks(session, state), 5)
    assert state.failure.result() is error
    assert state.completion.done()
    assert state.cleanup_error is None
    assert [(item.status, item.error_type) for item in observations] == [("failed", "LookupError")]
