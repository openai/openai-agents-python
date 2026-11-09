"""Interrupt receipts join sealed roots without waiting for unrelated work."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from _agents_codex.realtime import RealtimeConfig, RealtimeSession, _FunctionCall
from agents import Session, Turn
from tests._asyncio import timeout


@pytest.mark.parametrize("late_admission", [False, True])
async def test_interrupt_joins_only_receipted_root_after_admission_cleanup(
    late_admission: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    admission_entered = asyncio.Event()
    release_admission = asyncio.Event()
    root_entered = asyncio.Event()
    release_a, release_b, release_c = asyncio.Event(), asyncio.Event(), asyncio.Event()
    acknowledged = asyncio.Event()

    async def result_a() -> None:
        root_entered.set()
        await release_a.wait()

    async def result_b() -> None:
        await release_b.wait()

    async def result_c() -> None:
        await release_c.wait()

    async def cancel() -> None:
        raise AssertionError("A response must not cancel the shared root")

    a = cast(Turn[None], SimpleNamespace(turn_id="root-a", get_result=result_a, cancel=cancel))
    b = cast(Turn[None], SimpleNamespace(turn_id="root-b", get_result=result_b, cancel=cancel))
    c = cast(Turn[None], SimpleNamespace(turn_id="root-c", get_result=result_c, cancel=cancel))

    async def submit(**kwargs: Any) -> Turn[None]:
        admission_entered.set()
        await release_admission.wait()
        return a

    session = cast(
        Session[Any, Any],
        SimpleNamespace(_submit_realtime_tools=submit, _check_callback_operation=lambda _: None),
    )
    voice = RealtimeSession(session, config=RealtimeConfig())
    session._realtime = voice
    voice._live = True
    voice._ready.set_result(None)
    await voice._monitor_function_root(b)
    owned = _FunctionCall("response-a", {"call-a": "lookup"})
    voice._functions["call-a"] = owned
    if not late_admission:
        release_admission.set()
    owned.task = asyncio.create_task(voice._run_functions(owned))

    async def call(kind: str, **kwargs: Any) -> dict[str, Any]:
        assert kind == "interrupt"
        await voice._monitor_function_root(c)
        acknowledged.set()
        return {
            "retired_response_ids": ["response-a"],
            "cancelled_response_id": "response-a",
            "joined_root_turn_ids": ["root-a", "already-settled-legacy-root"],
        }

    monkeypatch.setattr(voice, "_call", call)
    stopping: asyncio.Task[None] | None = None
    async with timeout(5):
        try:
            await admission_entered.wait()
            if not late_admission:
                await root_entered.wait()
            stopping = asyncio.create_task(voice.interrupt("response-a"))
            await acknowledged.wait()
            release_admission.set()
            await root_entered.wait()
            # Native completion precedes the managed Turn's final cleanup.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(stopping), 0.05)
            assert not voice._functions
            release_a.set()
            await stopping
            assert voice._function_roots["root-a"].done()
            assert not voice._function_roots["root-b"].done()
            assert not voice._function_roots["root-c"].done()
        finally:
            release_admission.set()
            release_a.set()
            release_b.set()
            release_c.set()
            if stopping is not None:
                await asyncio.gather(stopping, return_exceptions=True)
            await asyncio.gather(owned.task, return_exceptions=True)
            await asyncio.gather(*voice._function_roots.values(), return_exceptions=True)


async def test_legacy_interrupt_receipt_does_not_join_an_unrelated_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()

    async def result() -> None:
        await release.wait()

    turn = cast(Turn[None], SimpleNamespace(turn_id="root-b", get_result=result))
    session = cast(Session[Any, Any], SimpleNamespace(_check_callback_operation=lambda _: None))
    voice = RealtimeSession(session, config=RealtimeConfig())
    voice._live = True
    voice._ready.set_result(None)
    await voice._monitor_function_root(turn)

    async def call(kind: str, **kwargs: Any) -> dict[str, Any]:
        return {"retired_response_ids": [], "cancelled_response_id": None}

    monkeypatch.setattr(voice, "_call", call)
    try:
        async with timeout(5):
            await voice.interrupt("response-a")
        assert not voice._function_roots["root-b"].done()
    finally:
        release.set()
        await voice._function_roots["root-b"]


@pytest.mark.parametrize("joined", [None, "root-a", [""], [7], ["root-a", "root-a"]])
async def test_interrupt_rejects_malformed_joined_root_receipt(
    joined: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = cast(Session[Any, Any], SimpleNamespace(_check_callback_operation=lambda _: None))
    voice = RealtimeSession(session, config=RealtimeConfig())
    voice._live = True
    voice._ready.set_result(None)

    async def call(kind: str, **kwargs: Any) -> dict[str, Any]:
        return {
            "retired_response_ids": [],
            "cancelled_response_id": None,
            "joined_root_turn_ids": joined,
        }

    monkeypatch.setattr(voice, "_call", call)
    async with timeout(5):
        with pytest.raises(RuntimeError, match="invalid completion receipt"):
            await voice.interrupt()
