"""Terminal host bookkeeping releases capacity without evicting active work."""

from contextvars import copy_context
from types import SimpleNamespace
from typing import Any, cast

import pytest

from _agents_codex.realtime import RealtimeConfig, RealtimeSession
from agents import Session


def owner() -> RealtimeSession:
    session = SimpleNamespace(_check_callback_operation=lambda _: None)
    realtime = RealtimeSession(cast(Session[Any, Any], session), config=RealtimeConfig())
    realtime._live = True
    realtime._ready.set_result(None)
    realtime._context = copy_context()
    realtime._HANDOFF_LIMIT = 4
    return realtime


def request(realtime: RealtimeSession, call_id: str) -> None:
    realtime._payload(
        {"FunctionCallRequested": {"call_id": call_id, "name": "echo", "response_id": call_id}},
        None,
    )


def drain(realtime: RealtimeSession) -> None:
    while not realtime._queue.empty():
        realtime._dequeue()


@pytest.mark.parametrize("finish_first", [False, True])
async def test_completed_function_rollover_preserves_active_requests(monkeypatch, finish_first):
    realtime = owner()

    async def submit(**kwargs):
        return SimpleNamespace(turn_id="shared-root")

    async def monitor(turn):
        pass

    monkeypatch.setattr(realtime._session, "_submit_realtime_tools", submit, raising=False)
    monkeypatch.setattr(realtime, "_monitor_function_root", monitor)
    request(realtime, "active")
    for index in range(12):
        call_id = f"call-{index}"
        request(realtime, call_id)
        realtime._start_function_batch({"response_id": call_id, "function_call_ids": [call_id]})
        batch = realtime._functions[call_id]
        finished = {"FunctionCallFinished": {"call_id": call_id, "succeeded": True}}
        if finish_first:
            realtime._payload(finished, None)
        realtime._finish_function_batch({"response_id": call_id})
        await batch.task
        if not finish_first:
            realtime._payload(finished, None)
        # Recent duplicates stay suppressed through both terminal paths.
        queued = realtime._queue.qsize()
        realtime._payload(finished, None)
        request(realtime, call_id)
        assert realtime._queue.qsize() == queued
        assert "active" in realtime._pending_functions
        assert "active" in realtime._seen
        assert len(realtime._seen) <= 4
        if index == 11:
            assert realtime._identity_bytes == 4 * sum(
                3 * len(key) + len("echo") for key in realtime._seen
            )
        drain(realtime)
    assert "call-0" not in realtime._function_names
    assert "call-0" not in realtime._finished_functions


async def test_completed_delegation_rollover_releases_identity(monkeypatch):
    import _agents_codex.realtime as module

    class Workflow:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, text):
            yield "done"

        async def cancel(self):
            pass

    async def complete(*args, **kwargs):
        pass

    realtime = owner()
    monkeypatch.setattr(module, "NativeSessionVoiceWorkflow", Workflow)
    monkeypatch.setattr(realtime, "_sync_active_profile", complete)
    monkeypatch.setattr(realtime, "_call", complete)
    for index in range(12):
        handoff_id = f"handoff-{index}"
        payload = {"HandoffRequested": {"handoff_id": handoff_id, "response_id": handoff_id}}
        realtime._payload(payload, "prepared input")
        await realtime._delegations[handoff_id].task
        realtime._payload(payload, "prepared input")
        assert handoff_id not in realtime._delegations
        assert len(realtime._seen) <= 4
        assert realtime._identity_bytes == 8 * sum(map(len, realtime._seen))
        drain(realtime)


async def test_repeated_native_retirements_release_old_response_capacity(monkeypatch):
    realtime = owner()

    async def receipt(operation, **kwargs):
        return {"retired_response_ids": [kwargs["response_id"]], "cancelled_response_id": None}

    monkeypatch.setattr(realtime, "_call", receipt)
    for index in range(12):
        response_id = f"response-{index}"
        await realtime.interrupt(response_id)
        assert response_id in realtime._guardrail_trips
        assert len(realtime._guardrail_trips) <= 4
        assert realtime._identity_bytes == 4 * sum(map(len, realtime._guardrail_trips))
        drain(realtime)


async def test_failed_native_interrupt_fails_owner_closed(monkeypatch):
    realtime = owner()
    request(realtime, "pending")

    async def fail(operation, **kwargs):
        raise RuntimeError("synthetic native rejection")

    monkeypatch.setattr(realtime, "_call", fail)
    with pytest.raises(RuntimeError, match="synthetic native rejection"):
        await realtime.interrupt("pending")
    assert not realtime._live
    assert realtime._terminal.done()
    with pytest.raises(RuntimeError):
        await realtime.interrupt("another")


async def test_retirement_rollover_preserves_running_guardrail():
    import asyncio

    realtime = owner()
    realtime._guardrail_items[("held", "item", None, "text")] = SimpleNamespace(
        task=asyncio.current_task(), snapshot=SimpleNamespace(response_id="held")
    )
    realtime._retire_response_output("held")
    for index in range(12):
        realtime._retire_response_output(f"retired-{index}")
        assert "held" in realtime._guardrail_trips
        assert len(realtime._guardrail_trips) <= 4
    assert realtime._identity_bytes == 4 * sum(map(len, realtime._guardrail_trips))


async def test_terminal_request_byte_pressure_releases_only_terminal_reservations(monkeypatch):
    realtime = owner()
    monkeypatch.setattr(realtime, "_BUFFER_BYTES_LIMIT", 400)
    request(realtime, "active")
    for index in range(12):
        call_id = f"terminal-{index}" + "x" * 12
        request(realtime, call_id)
        realtime._retire_pending_functions(call_id)
        assert "active" in realtime._pending_functions
        assert "active" in realtime._seen
        assert realtime._identity_bytes == 4 * sum(
            3 * len(key) + len("echo") for key in realtime._seen
        )
        assert realtime._identity_bytes <= 400
