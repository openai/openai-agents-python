from __future__ import annotations

import asyncio

import pytest

from agents.sandbox.session.pty_types import (
    PTY_EMPTY_YIELD_TIME_MS_MIN,
    PTY_YIELD_TIME_MS_MIN,
    _settle_pty_cleanup,
    allocate_pty_process_id,
    clamp_pty_yield_time_ms,
    process_id_to_prune_from_meta,
    resolve_pty_write_yield_time_ms,
)


def test_clamp_pty_yield_time_ms_enforces_minimum() -> None:
    assert clamp_pty_yield_time_ms(0) == PTY_YIELD_TIME_MS_MIN


def test_resolve_pty_write_yield_time_ms_uses_longer_poll_for_empty_input() -> None:
    assert (
        resolve_pty_write_yield_time_ms(yield_time_ms=PTY_YIELD_TIME_MS_MIN, input_empty=True)
        == PTY_EMPTY_YIELD_TIME_MS_MIN
    )
    assert (
        resolve_pty_write_yield_time_ms(yield_time_ms=PTY_YIELD_TIME_MS_MIN, input_empty=False)
        == PTY_YIELD_TIME_MS_MIN
    )


def test_allocate_pty_process_id_avoids_used_ids() -> None:
    used = {1000, 1001, 1002}
    allocated = allocate_pty_process_id(used)
    assert allocated not in used


def test_process_id_to_prune_from_meta_prefers_exited_unprotected_sessions() -> None:
    meta = [(1001 + i, float(100 - i), False) for i in range(8)]
    meta.append((2001, 1.0, True))
    meta.append((2002, 2.0, False))

    assert process_id_to_prune_from_meta(meta) == 2001


@pytest.mark.asyncio
async def test_settle_pty_cleanup_preserves_cancel_reason_when_cleanup_fails() -> None:
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def cleanup() -> None:
        cleanup_started.set()
        await cleanup_release.wait()
        raise RuntimeError("synthetic cleanup failure")

    task = asyncio.create_task(_settle_pty_cleanup(cleanup()))
    await cleanup_started.wait()
    task.cancel("route-A")
    cleanup_release.set()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await task

    assert exc_info.value.args == ("route-A",)


@pytest.mark.asyncio
async def test_settle_pty_cleanup_preserves_initial_cancel_reason_when_cleanup_fails() -> None:
    async def cleanup() -> None:
        raise RuntimeError("synthetic cleanup failure")

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await _settle_pty_cleanup(
            cleanup(),
            initial_cancellation=asyncio.CancelledError("startup-cancel"),
        )

    assert exc_info.value.args == ("startup-cancel",)
