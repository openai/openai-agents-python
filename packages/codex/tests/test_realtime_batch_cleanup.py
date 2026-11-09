"""Failed native cleanup keeps pending admissions owned for a later retry."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from _agents_codex.realtime import RealtimeConfig, RealtimeSession, _FunctionCall
from agents import Session, Turn, TurnStoppedError
from tests._asyncio import timeout


@pytest.mark.parametrize("operation", ["interrupt", "close"])
async def test_failed_cleanup_does_not_wait_for_unconfirmed_admission(
    operation: str, monkeypatch
) -> None:
    entered, release, acknowledged = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def result():
        return None

    async def cancel():
        raise AssertionError("A response must not cancel the shared root")

    turn = cast(Turn[None], SimpleNamespace(turn_id="root-a", get_result=result, cancel=cancel))

    async def submit(**kwargs):
        entered.set()
        await release.wait()
        return turn

    session = cast(
        Session[Any, Any],
        SimpleNamespace(
            _submit_realtime_tools=submit,
            _check_callback_operation=lambda _: None,
        ),
    )
    voice = RealtimeSession(session, config=RealtimeConfig())
    session._realtime = voice
    voice._live = True
    voice._ready.set_result(None)
    voice._start = asyncio.create_task(result())
    owned = _FunctionCall("response-a", {"call-a": "lookup"})
    voice._functions["call-a"] = owned
    owned.task = asyncio.create_task(voice._run_functions(owned))
    failure = RuntimeError("native cleanup was not acknowledged")
    attempts = 0

    async def call(kind, **kwargs):
        nonlocal attempts
        assert kind == (operation if attempts == 0 else "close")
        attempts += 1
        if attempts == 1:
            raise failure
        acknowledged.set()
        return {
            "retired_response_ids": ["response-a"],
            "cancelled_response_id": "response-a",
        }

    monkeypatch.setattr(voice, "_call", call)

    async def cleanup():
        if operation == "interrupt":
            await voice.interrupt("response-a")
        else:
            voice._live = False
            await voice._close()

    async with timeout(5):
        await entered.wait()
        with pytest.raises(RuntimeError) as caught:
            await cleanup()
        assert caught.value is failure
        assert voice._functions["call-a"] is owned
        assert owned.submission is not None and not owned.submission.done()
        # An uncertain interrupt retires media admission. Close retries the
        # retained native cleanup without reopening the failed owner.
        assert not voice._live
        retry = asyncio.create_task(voice.aclose())
        await acknowledged.wait()
        assert attempts == 2
        assert not retry.done()
        release.set()
        await retry
        await asyncio.gather(owned.task, return_exceptions=True)
        await asyncio.gather(*voice._function_roots.values())
        assert not voice._functions


async def test_late_admission_observes_an_already_stopped_shared_root() -> None:
    stopped = TurnStoppedError("shared root stopped")

    async def result():
        raise stopped

    async def cancel():
        raise AssertionError("A response must not cancel the shared root")

    turn = cast(Turn[None], SimpleNamespace(turn_id="root-a", get_result=result, cancel=cancel))

    async def submit(**kwargs):
        return turn

    session = cast(Session[Any, Any], SimpleNamespace(_submit_realtime_tools=submit))
    voice = RealtimeSession(session, config=RealtimeConfig())
    voice._live = True
    async with timeout(5):
        await voice._monitor_function_root(turn)
        await voice._function_roots["root-a"]
        owned = _FunctionCall("response-b", {"call-b": "lookup"})
        voice._functions["call-b"] = owned
        await voice._run_functions(owned)
        assert owned.completed.result() is stopped
        assert not voice._functions


async def test_normal_root_completion_waits_for_the_media_batch_receipt() -> None:
    root_finished = asyncio.Event()

    async def result():
        root_finished.set()

    turn = cast(Turn[None], SimpleNamespace(turn_id="root-a", get_result=result))

    async def submit(**kwargs):
        return turn

    session = cast(Session[Any, Any], SimpleNamespace(_submit_realtime_tools=submit))
    voice = RealtimeSession(session, config=RealtimeConfig())
    voice._live = True
    owned = _FunctionCall("response-a", {"call-a": "lookup"})
    voice._functions["call-a"] = owned
    owned.task = asyncio.create_task(voice._run_functions(owned))
    async with timeout(5):
        await root_finished.wait()
        try:
            assert not owned.completed.done()
            assert voice._queue.empty()
        finally:
            voice._finish_function_batch({"response_id": "response-a", "target": None})
            await owned.task
        assert not voice._functions


async def test_graph_closure_releases_cleanup_already_waiting_after_failed_interrupt(
    monkeypatch,
) -> None:
    reading_root = asyncio.Event()

    async def result():
        reading_root.set()
        await asyncio.Future()

    async def cancel():
        raise AssertionError("A response must not cancel the shared root")

    turn = cast(Turn[None], SimpleNamespace(turn_id="root-a", get_result=result, cancel=cancel))

    async def submit(**kwargs):
        return turn

    session = cast(
        Session[Any, Any],
        SimpleNamespace(
            _submit_realtime_tools=submit,
            _check_callback_operation=lambda _: None,
        ),
    )
    voice = RealtimeSession(session, config=RealtimeConfig())
    session._realtime = voice
    voice._live = True
    voice._ready.set_result(None)
    owned = _FunctionCall("response-a", {"call-a": "lookup"})
    voice._functions["call-a"] = owned
    owned.task = asyncio.create_task(voice._run_functions(owned))

    async def call(kind, **kwargs):
        raise RuntimeError("native interrupt was not acknowledged")

    monkeypatch.setattr(voice, "_call", call)
    async with timeout(5):
        await reading_root.wait()
        with pytest.raises(RuntimeError, match="not acknowledged"):
            await voice.interrupt("response-a")
        assert owned.cancellation is not None and not owned.cancellation.done()
        voice._session_closing()
        await voice._native_shutdown()
        assert owned.cancellation.done()
        assert not voice._functions
        assert all(task.done() for task in voice._function_roots.values())
