from __future__ import annotations

import asyncio
from collections import deque

import pytest

from agents.sandbox.session import pty_output as pty_output_module
from agents.sandbox.session.pty_output import (
    _incomplete_utf8_suffix_length,
    collect_pty_output,
)


@pytest.mark.asyncio
async def test_collect_pty_output_waits_for_notification() -> None:
    output_chunks: deque[bytes] = deque()
    output_lock = asyncio.Lock()
    output_notify = asyncio.Event()
    done = False

    async def produce_output() -> None:
        nonlocal done
        await asyncio.sleep(0)
        async with output_lock:
            output_chunks.append(b"notified output")
        done = True
        output_notify.set()

    producer_task = asyncio.create_task(produce_output())
    output, original_token_count, output_closed = await collect_pty_output(
        output_chunks=output_chunks,
        output_lock=output_lock,
        output_notify=output_notify,
        is_done=lambda: done,
        yield_time_ms=500,
        max_output_tokens=None,
    )
    await producer_task

    assert output == b"notified output"
    assert original_token_count is None
    assert output_closed is True


@pytest.mark.asyncio
async def test_collect_pty_output_drains_chunks_added_when_done() -> None:
    output_chunks = deque([b"before done"])

    def mark_done() -> bool:
        output_chunks.append(b" after done")
        return True

    output, original_token_count, output_closed = await collect_pty_output(
        output_chunks=output_chunks,
        output_lock=asyncio.Lock(),
        output_notify=asyncio.Event(),
        is_done=mark_done,
        yield_time_ms=500,
        max_output_tokens=None,
    )

    assert output == b"before done after done"
    assert original_token_count is None
    assert output_closed is True


@pytest.mark.asyncio
async def test_collect_pty_output_drains_chunks_queued_when_wait_times_out() -> None:
    output_chunks: deque[bytes] = deque()

    class TimeoutAfterQueueing:
        async def wait(self) -> None:
            output_chunks.append(b"queued at timeout")
            raise asyncio.TimeoutError

        def clear(self) -> None:
            pass

    output, original_token_count, output_closed = await collect_pty_output(
        output_chunks=output_chunks,
        output_lock=asyncio.Lock(),
        output_notify=TimeoutAfterQueueing(),  # type: ignore[arg-type]
        is_done=lambda: False,
        yield_time_ms=500,
        max_output_tokens=None,
    )

    assert output == b"queued at timeout"
    assert original_token_count is None
    assert output_closed is False
    assert not output_chunks


@pytest.mark.parametrize(
    ("character", "split"),
    [
        pytest.param("é", 1, id="two-byte-1"),
        pytest.param("€", 1, id="three-byte-1"),
        pytest.param("€", 2, id="three-byte-2"),
        pytest.param("😀", 1, id="four-byte-1"),
        pytest.param("😀", 2, id="four-byte-2"),
        pytest.param("😀", 3, id="four-byte-3"),
    ],
)
@pytest.mark.asyncio
async def test_collect_pty_output_preserves_valid_utf8_at_every_split(
    character: str,
    split: int,
) -> None:
    encoded = character.encode("utf-8")
    output_chunks: deque[bytes] = deque([b"a" + encoded[:split]])
    output_lock = asyncio.Lock()
    output_notify = asyncio.Event()
    done = False

    first, _, first_closed = await collect_pty_output(
        output_chunks=output_chunks,
        output_lock=output_lock,
        output_notify=output_notify,
        is_done=lambda: done,
        yield_time_ms=0,
        max_output_tokens=None,
    )

    output_chunks.append(encoded[split:] + b"b")
    done = True
    second, _, second_closed = await collect_pty_output(
        output_chunks=output_chunks,
        output_lock=output_lock,
        output_notify=output_notify,
        is_done=lambda: done,
        yield_time_ms=0,
        max_output_tokens=None,
    )

    assert first == b"a"
    assert first_closed is False
    assert first + second == ("a" + character + "b").encode("utf-8")
    assert second_closed is True
    assert not output_chunks


@pytest.mark.parametrize(
    "invalid_prefix",
    [
        pytest.param(b"\xe0\x80", id="e0-overlong"),
        pytest.param(b"\xed\xa0", id="ed-surrogate"),
        pytest.param(b"\xf0\x80", id="f0-overlong"),
        pytest.param(b"\xf4\x90", id="f4-out-of-range"),
    ],
)
@pytest.mark.asyncio
async def test_collect_pty_output_replaces_restricted_utf8_prefixes_without_carry(
    invalid_prefix: bytes,
) -> None:
    output_chunks: deque[bytes] = deque([b"prompt" + invalid_prefix])

    output, _, output_closed = await collect_pty_output(
        output_chunks=output_chunks,
        output_lock=asyncio.Lock(),
        output_notify=asyncio.Event(),
        is_done=lambda: False,
        yield_time_ms=0,
        max_output_tokens=None,
    )

    assert output.decode("utf-8") == "prompt��"
    assert output_closed is False
    assert not output_chunks


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        pytest.param(b"", 0, id="empty"),
        pytest.param(b"abc", 0, id="ascii"),
        pytest.param(b"\xc3", 1, id="two-byte-lead"),
        pytest.param(b"\xe2\x82", 2, id="three-byte-prefix"),
        pytest.param(b"\xf0\x9f\x98", 3, id="four-byte-prefix"),
        pytest.param(b"\xe0\x80", 0, id="e0-restricted"),
        pytest.param(b"\xe0\xa0", 2, id="e0-valid"),
        pytest.param(b"\xed\xa0", 0, id="ed-restricted"),
        pytest.param(b"\xed\x9f", 2, id="ed-valid"),
        pytest.param(b"\xf0\x80", 0, id="f0-restricted"),
        pytest.param(b"\xf0\x90", 2, id="f0-valid"),
        pytest.param(b"\xf4\x90", 0, id="f4-restricted"),
        pytest.param(b"\xf4\x8f", 2, id="f4-valid"),
        pytest.param(b"\x80\x80\x80", 0, id="orphan-continuations"),
    ],
)
def test_incomplete_utf8_suffix_length_accepts_only_completable_sequences(
    data: bytes,
    expected: int,
) -> None:
    assert _incomplete_utf8_suffix_length(data) == expected


@pytest.mark.asyncio
async def test_collect_pty_output_checks_deadline_before_next_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 0.0
    poll_count = 0
    settle_count = 0

    def monotonic() -> float:
        return now

    async def poll_output(_deadline: float) -> None:
        nonlocal now, poll_count
        poll_count += 1
        now = 0.1

    async def settle_output() -> None:
        nonlocal settle_count
        settle_count += 1

    async def wait_for_output(_remaining_s: float) -> None:
        nonlocal now
        now = 0.3

    monkeypatch.setattr(pty_output_module.time, "monotonic", monotonic)

    output, _, output_closed = await collect_pty_output(
        output_chunks=deque(),
        output_lock=asyncio.Lock(),
        output_notify=asyncio.Event(),
        is_done=lambda: False,
        yield_time_ms=250,
        max_output_tokens=None,
        poll_output=poll_output,
        settle_output=settle_output,
        wait_for_output=wait_for_output,
    )

    assert output == b""
    assert output_closed is False
    assert poll_count == 1
    assert settle_count == 1


@pytest.mark.asyncio
async def test_collect_pty_output_settles_terminal_carry_once_across_repeated_reads() -> None:
    output_chunks: deque[bytes] = deque([b"tail\xe2\x82"])
    output_lock = asyncio.Lock()
    output_notify = asyncio.Event()
    done = False

    first, _, first_closed = await collect_pty_output(
        output_chunks=output_chunks,
        output_lock=output_lock,
        output_notify=output_notify,
        is_done=lambda: done,
        yield_time_ms=0,
        max_output_tokens=None,
    )
    done = True
    terminal, _, terminal_closed = await collect_pty_output(
        output_chunks=output_chunks,
        output_lock=output_lock,
        output_notify=output_notify,
        is_done=lambda: done,
        yield_time_ms=0,
        max_output_tokens=None,
    )
    repeated, _, repeated_closed = await collect_pty_output(
        output_chunks=output_chunks,
        output_lock=output_lock,
        output_notify=output_notify,
        is_done=lambda: done,
        yield_time_ms=0,
        max_output_tokens=None,
    )

    assert first == b"tail"
    assert first_closed is False
    assert terminal.decode("utf-8") == "�"
    assert terminal_closed is True
    assert repeated == b""
    assert repeated_closed is True
    assert not output_chunks


@pytest.mark.asyncio
async def test_collect_pty_output_restores_carry_when_next_collection_is_cancelled() -> None:
    output_chunks: deque[bytes] = deque([b"\xc3"])
    output_lock = asyncio.Lock()
    output_notify = asyncio.Event()
    done = False

    first, _, first_closed = await collect_pty_output(
        output_chunks=output_chunks,
        output_lock=output_lock,
        output_notify=output_notify,
        is_done=lambda: done,
        yield_time_ms=0,
        max_output_tokens=None,
    )

    wait_started = asyncio.Event()

    async def wait_for_output(_remaining_s: float) -> None:
        wait_started.set()
        await asyncio.Event().wait()

    cancelled = asyncio.create_task(
        collect_pty_output(
            output_chunks=output_chunks,
            output_lock=output_lock,
            output_notify=output_notify,
            is_done=lambda: done,
            yield_time_ms=1_000,
            max_output_tokens=None,
            wait_for_output=wait_for_output,
        )
    )
    await wait_started.wait()
    await output_lock.acquire()
    cancelled.cancel()
    await asyncio.sleep(0)
    cancelled.cancel()
    await asyncio.sleep(0)
    cancelled.cancel()
    await asyncio.sleep(0)

    assert cancelled.done() is False
    assert not output_chunks

    output_lock.release()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    assert first == b""
    assert first_closed is False
    assert list(output_chunks) == [b"\xc3"]

    output_chunks.append(b"\xa9")
    done = True
    terminal, _, terminal_closed = await collect_pty_output(
        output_chunks=output_chunks,
        output_lock=output_lock,
        output_notify=output_notify,
        is_done=lambda: done,
        yield_time_ms=0,
        max_output_tokens=None,
    )

    assert terminal == "é".encode()
    assert terminal_closed is True
    assert not output_chunks
