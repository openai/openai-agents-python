from __future__ import annotations

import asyncio
import contextlib
from collections import deque

import pytest

from agents.sandbox.session.pty_output import (
    close_pty_tail,
    collect_pty_output,
    flush_pty_tail,
)
from agents.sandbox.session.pty_types import truncate_text_by_tokens


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
    output, original_token_count = await collect_pty_output(
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


@pytest.mark.asyncio
async def test_collect_pty_output_drains_chunks_added_when_done() -> None:
    output_chunks = deque([b"before done"])

    def mark_done() -> bool:
        output_chunks.append(b" after done")
        return True

    output, original_token_count = await collect_pty_output(
        output_chunks=output_chunks,
        output_lock=asyncio.Lock(),
        output_notify=asyncio.Event(),
        is_done=mark_done,
        yield_time_ms=500,
        max_output_tokens=None,
    )

    assert output == b"before done after done"
    assert original_token_count is None


async def _one_window(
    chunks: deque[bytes],
    lock: asyncio.Lock,
    notify: asyncio.Event,
    done: dict[str, bool],
) -> bytes:
    notify.set()
    collected, _ = await collect_pty_output(
        output_chunks=chunks,
        output_lock=lock,
        output_notify=notify,
        is_done=lambda: done["value"],
        yield_time_ms=1,
        max_output_tokens=None,
    )
    return collected


# one, two, three and four byte characters, so every sequence width is split
SPLIT_TEXT = "aé☃\U0001d11eb"


@pytest.mark.asyncio
@pytest.mark.parametrize("split", range(1, len(SPLIT_TEXT.encode("utf-8"))))
async def test_collect_pty_output_keeps_a_character_split_across_windows(split: int) -> None:
    text = SPLIT_TEXT
    raw = text.encode("utf-8")

    chunks: deque[bytes] = deque()
    lock = asyncio.Lock()
    notify = asyncio.Event()
    done = {"value": False}

    chunks.append(raw[:split])
    first = await _one_window(chunks, lock, notify, done)
    chunks.append(raw[split:])
    done["value"] = True
    second = await _one_window(chunks, lock, notify, done)

    assert (first + second).decode("utf-8") == text


@pytest.mark.asyncio
async def test_collect_pty_output_replaces_a_truncated_character_once_done() -> None:
    # the stream ends mid character, so there is no later window to complete it
    chunks: deque[bytes] = deque([b"hi " + "é".encode()[:1]])

    collected, _ = await collect_pty_output(
        output_chunks=chunks,
        output_lock=asyncio.Lock(),
        output_notify=asyncio.Event(),
        is_done=lambda: True,
        yield_time_ms=1,
        max_output_tokens=None,
    )

    assert collected.decode("utf-8") == "hi �"
    assert not chunks


@pytest.mark.asyncio
async def test_collect_pty_output_leaves_complete_multibyte_output_alone() -> None:
    chunks: deque[bytes] = deque(["héllo".encode()])

    collected, _ = await collect_pty_output(
        output_chunks=chunks,
        output_lock=asyncio.Lock(),
        output_notify=asyncio.Event(),
        is_done=lambda: True,
        yield_time_ms=1,
        max_output_tokens=None,
    )

    assert collected.decode("utf-8") == "héllo"


# a lead byte that no character can start with, an overlong form, a value past the
# end of the range, a lone continuation byte, and half of a surrogate pair
@pytest.mark.asyncio
@pytest.mark.parametrize("garbage", [b"\xff", b"\xc0", b"\xc1", b"\xf5", b"\x80", b"\xed\xa0\x80"])
async def test_collect_pty_output_does_not_hold_back_bytes_that_start_no_character(
    garbage: bytes,
) -> None:
    # the process is still running, but these bytes will never be completed by a later
    # window, so holding them would hide the output until it exits
    chunks: deque[bytes] = deque([b"ok " + garbage])
    lock = asyncio.Lock()
    notify = asyncio.Event()
    done = {"value": False}

    collected = await _one_window(chunks, lock, notify, done)

    assert collected.decode("utf-8").startswith("ok �")
    assert not chunks


# ED A0..BF leads a surrogate, so no third byte can complete it. the decoder still buffers
# these, and they are the only prefixes it buffers that can never become a character
@pytest.mark.asyncio
@pytest.mark.parametrize("second", [0xA0, 0xAF, 0xBF])
async def test_collect_pty_output_does_not_hold_back_a_surrogate_lead(second: int) -> None:
    chunks: deque[bytes] = deque([b"ok " + bytes([0xED, second])])
    lock = asyncio.Lock()
    notify = asyncio.Event()
    done = {"value": False}

    collected = await _one_window(chunks, lock, notify, done)

    assert collected.decode("utf-8") == "ok \ufffd\ufffd"
    assert not chunks


@pytest.mark.asyncio
@pytest.mark.parametrize("second", [0x80, 0x9F])
async def test_collect_pty_output_still_holds_a_valid_lead_below_the_surrogates(
    second: int,
) -> None:
    # ED 80..9F is U+D000..U+D7FF, which is a real character, so it must still be waited for
    raw = bytes([0xED, second, 0x80])
    chunks: deque[bytes] = deque([raw[:2]])
    lock = asyncio.Lock()
    notify = asyncio.Event()
    done = {"value": False}

    first = await _one_window(chunks, lock, notify, done)
    assert first == b""

    chunks.append(raw[2:])
    done["value"] = True
    second_window = await _one_window(chunks, lock, notify, done)

    assert (first + second_window).decode("utf-8") == raw.decode("utf-8")


def test_close_pty_tail_replaces_the_leftover_and_leaves_a_clean_session_alone() -> None:
    finished, count = close_pty_tail(
        leftover="\u00e9".encode()[:1],
        output=b"hi ",
        original_token_count=None,
        max_output_tokens=None,
    )
    assert finished.decode("utf-8") == "hi \ufffd"

    unchanged, same = close_pty_tail(
        leftover=b"",
        output=b"hi ",
        original_token_count=count,
        max_output_tokens=None,
    )
    assert unchanged == b"hi "
    assert same == count


def test_close_pty_tail_applies_the_token_cap_to_what_it_adds() -> None:
    # the window already truncated to the cap, so the tail cannot be appended past it
    capped, _ = close_pty_tail(
        leftover="\u00e9".encode()[:1],
        output=b"",
        original_token_count=None,
        max_output_tokens=0,
    )
    uncapped, _ = close_pty_tail(
        leftover="\u00e9".encode()[:1],
        output=b"",
        original_token_count=None,
        max_output_tokens=None,
    )

    assert uncapped.decode("utf-8") == "\ufffd"
    assert len(capped) <= len(uncapped)


@pytest.mark.asyncio
async def test_flush_pty_tail_drains_what_the_session_still_holds() -> None:
    chunks: deque[bytes] = deque(["\u00e9".encode()[:1]])
    lock = asyncio.Lock()

    flushed, _ = await flush_pty_tail(
        output_chunks=chunks,
        output_lock=lock,
        output=b"hi ",
        original_token_count=None,
        max_output_tokens=None,
    )

    assert flushed.decode("utf-8") == "hi \ufffd"
    assert not chunks


@pytest.mark.asyncio
async def test_collect_pty_output_keeps_the_tail_when_a_window_is_cancelled() -> None:
    # the lead byte has already left the deque by the time the window decodes, so this is the
    # only copy of it. a producer holding the lock must not be able to turn a cancelled call
    # into a lost character for the session that carries on
    raw = "\u00e9".encode()
    chunks: deque[bytes] = deque([raw[:1]])
    lock = asyncio.Lock()
    notify = asyncio.Event()
    done = {"value": False}

    async def window(yield_time_ms: int) -> bytes:
        notify.set()
        collected, _ = await collect_pty_output(
            output_chunks=chunks,
            output_lock=lock,
            output_notify=notify,
            is_done=lambda: done["value"],
            yield_time_ms=yield_time_ms,
            max_output_tokens=None,
        )
        return collected

    task = asyncio.create_task(window(120))
    await asyncio.sleep(0.02)
    assert not chunks

    await lock.acquire()
    await asyncio.sleep(0.2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    lock.release()

    assert list(chunks) == [raw[:1]]

    chunks.append(raw[1:])
    done["value"] = True
    assert (await window(60)).decode("utf-8") == "\u00e9"


@pytest.mark.asyncio
async def test_collect_pty_output_puts_a_drained_window_back_when_cancelled() -> None:
    # the window drains the lead byte a previous one requeued, then the call is cancelled while
    # it waits. the session lives on, so those bytes have to go back or its next read reports a
    # replacement character for output that did arrive
    raw = "\u00e9".encode()
    chunks: deque[bytes] = deque([raw[:1]])
    lock = asyncio.Lock()
    notify = asyncio.Event()
    done = {"value": False}

    async def window(yield_time_ms: int) -> bytes:
        notify.set()
        collected, _ = await collect_pty_output(
            output_chunks=chunks,
            output_lock=lock,
            output_notify=notify,
            is_done=lambda: done["value"],
            yield_time_ms=yield_time_ms,
            max_output_tokens=None,
        )
        return collected

    task = asyncio.create_task(window(60_000))
    await asyncio.sleep(0.05)
    assert not chunks

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert list(chunks) == [raw[:1]]

    chunks.append(raw[1:])
    done["value"] = True
    assert (await window(60)).decode("utf-8") == "\u00e9"


def test_close_pty_tail_leaves_a_window_that_already_hit_the_cap_alone() -> None:
    # the window truncated, so its output is at the cap and the count describes the source.
    # folding a tail in here would truncate a second time and recount the shortened display
    display, count = truncate_text_by_tokens("a" * 100, 10)
    assert count is not None

    output, kept = close_pty_tail(
        leftover="\u00e9".encode()[:1],
        output=display.encode(),
        original_token_count=count,
        max_output_tokens=10,
    )

    assert output == display.encode()
    assert kept == count


def test_close_pty_tail_still_folds_the_tail_into_an_untruncated_window() -> None:
    display, count = truncate_text_by_tokens("hi ", 10)
    assert count is None

    output, recounted = close_pty_tail(
        leftover="\u00e9".encode()[:1],
        output=display.encode(),
        original_token_count=count,
        max_output_tokens=10,
    )

    assert output.decode("utf-8") == "hi \ufffd"
    assert recounted is None
