"""A managed observer bounds queued work without counting its active callback."""

from __future__ import annotations

import asyncio

import pytest

from _agents_codex._managed_tool_stream import _ManagedToolStream
from agents import NativeRoleToolInvocation
from agents.observation import RawProviderEvent, TurnStarted
from tests._asyncio import timeout


def origin() -> NativeRoleToolInvocation:
    return {
        "tool_name": "research",
        "role": "worker",
        "input": "original",
        "native_call_id": "call",
        "parent_thread_id": "root",
        "parent_turn_id": "parent",
        "child_thread_id": "child",
        "child_turn_id": "turn",
        "cell_id": None,
        "runtime_tool_call_id": None,
    }


def event(sequence: int) -> TurnStarted:
    return TurnStarted(
        session_id="session", submission_id="child", turn_id="turn", sequence=sequence
    )


@pytest.mark.parametrize("limit", [1, None])
async def test_managed_worker_excludes_the_running_handler(limit: int | None) -> None:
    started, release = asyncio.Event(), asyncio.Event()
    received = []
    overflows = []

    async def observe(value):
        received.append(value["event"].sequence)
        assert value["invocation"]["input"] == "original"
        value["invocation"]["input"] = "detached mutation"
        if len(received) == 1:
            started.set()
            await release.wait()

    stream = _ManagedToolStream(origin(), "child", observe, limit, lambda: overflows.append(True))
    async with timeout(5):
        stream.append(event(1))
        await started.wait()
        count = 2 if limit == 1 else 1100
        for sequence in range(2, count + 1):
            stream.append(event(sequence))
        assert not overflows
        stream.finish()
        release.set()
        await stream.drain()
    assert received == list(range(1, count + 1))


async def test_managed_worker_overflow_cancels_the_owned_handler() -> None:
    started = asyncio.Event()
    overflows = []

    async def observe(_value):
        started.set()
        await asyncio.Event().wait()

    stream = _ManagedToolStream(origin(), "child", observe, 1, lambda: overflows.append(True))
    async with timeout(5):
        stream.append(event(1))
        await started.wait()
        stream.append(event(2))
        stream.append(event(3))
        with pytest.raises(RuntimeError, match="pending event limit"):
            await stream.drain()
    assert overflows == [True]
    stream.append(event(4))


async def test_managed_worker_logs_handler_errors_and_keeps_order() -> None:
    received = []

    def observe(value):
        received.append(value["event"].sequence)
        if len(received) == 1:
            raise ValueError("handler failure")

    stream = _ManagedToolStream(origin(), "child", observe, None, lambda: None)
    stream.append(event(1))
    stream.append(event(2))
    stream.finish()
    async with timeout(5):
        await stream.drain()
    assert received == [1, 2]


async def test_managed_worker_bounds_utf8_bytes_with_a_stalled_handler() -> None:
    started = asyncio.Event()
    overflows = []
    received = []

    async def observe(value):
        received.append(value["event"].sequence)
        started.set()
        await asyncio.Event().wait()

    stream = _ManagedToolStream(origin(), "child", observe, None, lambda: overflows.append(True))
    # Three events fit a character budget but exceed the retained UTF-8 budget.
    payload = {"text": "é" * (6 * 1024 * 1024)}
    async with timeout(5):
        for sequence in range(1, 4):
            stream.append(
                RawProviderEvent(
                    session_id="session",
                    submission_id="child",
                    turn_id="turn",
                    sequence=sequence,
                    source="responses",
                    transport="sse",
                    payload=payload,
                )
            )
            if sequence == 1:
                await started.wait()
        with pytest.raises(RuntimeError, match="retained byte limit"):
            await stream.drain()
    assert overflows == [True]
    assert received == [1]


async def test_managed_worker_rejects_one_oversized_first_event() -> None:
    received = []
    overflows = []
    stream = _ManagedToolStream(
        origin(),
        "child",
        lambda value: received.append(value),
        None,
        lambda: overflows.append(True),
    )
    stream.append(
        RawProviderEvent(
            session_id="session",
            submission_id="child",
            turn_id="turn",
            source="responses",
            transport="sse",
            payload={"text": "é" * (17 * 1024 * 1024)},
        )
    )
    async with timeout(5):
        with pytest.raises(RuntimeError, match="retained byte limit"):
            await stream.drain()
    assert overflows == [True]
    assert received == []


async def test_managed_worker_logs_no_callback_exception_payload(caplog) -> None:
    def observe(_value):
        try:
            raise ValueError("private chained child payload")
        except ValueError as cause:
            raise RuntimeError("private callback payload") from cause

    stream = _ManagedToolStream(origin(), "child", observe, None, lambda: None)
    stream.append(event(1))
    stream.finish()
    async with timeout(5):
        await stream.drain()
    [record] = [record for record in caplog.records if record.name == "agents.native.managed_tool"]
    assert record.getMessage() == "Managed child stream callback failed"
    assert record.exc_info is None
    assert record.exc_text is None
    assert "private" not in caplog.text
