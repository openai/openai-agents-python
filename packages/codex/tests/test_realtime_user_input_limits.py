"""Bound user input before copying content or dispatching native JSON."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from _agents_codex.realtime import RealtimeConfig, RealtimeSession
from agents import Session

_LIMIT = 1024 * 1024


def owner() -> tuple[RealtimeSession, list[dict[str, Any]]]:
    requests: list[dict[str, Any]] = []

    async def call(operation: str, request: str) -> str:
        assert len(request) < 8 * 1024 * 1024
        requests.append(json.loads(request))
        return "{}"

    session = cast(Session[Any, Any], SimpleNamespace(_native=SimpleNamespace(call=call)))
    voice = RealtimeSession(session, config=RealtimeConfig())
    voice._live = True
    voice._ready.set_result(None)
    return voice, requests


class OversizedText(str):
    def encode(self, *args: Any, **kwargs: Any) -> bytes:
        raise AssertionError("Oversized input must not be encoded")

    def strip(self, *args: Any, **kwargs: Any) -> str:
        raise AssertionError("Oversized input must not be copied")


@pytest.mark.parametrize("kind", ["text", "message", "image", "multipart", "unicode", "parts"])
async def test_oversized_input_has_no_native_dispatch(kind: str) -> None:
    voice, requests = owner()
    oversized = OversizedText("x" * (_LIMIT + 1))
    if kind == "text":
        sending = voice.send_text(oversized)
    elif kind == "message":
        sending = voice.send_message(oversized)
    else:
        content: list[Any]
        if kind == "image":
            content = [{"type": "input_image", "image_url": oversized}]
        elif kind == "multipart":
            content = [
                {"type": "input_text", "text": "x" * (_LIMIT // 2)},
                {"type": "input_image", "image_url": "y" * (_LIMIT // 2 + 1)},
            ]
        elif kind == "unicode":
            content = [{"type": "input_text", "text": "\U0001f600" * (_LIMIT // 4 + 1)}]
        else:
            content = [{"type": "input_text", "text": "x"}] * 1025
        sending = voice.send_message({"type": "message", "role": "user", "content": content})
    with pytest.raises(ValueError, match="input exceeds"):
        await sending
    assert requests == []
    await voice.send_text("still usable")
    assert requests[0]["text"] == "still usable"


@pytest.mark.parametrize(
    "text",
    ["x" * _LIMIT, "\x00" * _LIMIT, "\U0001f600" * (_LIMIT // 4)],
    ids=["ascii", "json-escapes", "unicode"],
)
async def test_input_at_utf8_boundary_preserves_contents(text: str) -> None:
    voice, requests = owner()
    await voice.send_text(text)
    await voice.send_message(text, trigger_response=False)
    assert requests[0]["text"] == text
    assert requests[1]["content"] == [{"type": "input_text", "text": text}]


async def test_aggregate_and_part_boundaries_preserve_all_parts() -> None:
    voice, requests = owner()
    content: list[Any] = [
        {"type": "input_text", "text": "x" * (_LIMIT // 2)},
        {"type": "input_image", "image_url": "y" * (_LIMIT // 2), "detail": "low"},
    ]
    await voice.send_message({"type": "message", "role": "user", "content": content})
    assert requests[0]["content"] == content
    parts: list[Any] = [{"type": "input_text", "text": "x"}] * 1024
    await voice.send_message({"type": "message", "role": "user", "content": parts})
    assert requests[1]["content"] == parts


class ExtraFields(dict[str, Any]):
    def __iter__(self):
        raise AssertionError("Unexpected keys must not be copied")


@pytest.mark.parametrize("part", [False, True])
async def test_extra_mapping_fields_are_rejected_before_copying(part: bool) -> None:
    voice, requests = owner()
    extra = ExtraFields({"type": "input_text", "text": "x", "extra": 1, "other": 2})
    message: Any = {"type": "message", "role": "user", "content": [extra]} if part else extra
    with pytest.raises(ValueError):
        await voice.send_message(message)
    assert requests == []


@pytest.mark.parametrize("fail", [False, True])
async def test_pending_input_count_is_bounded_and_released_after_settlement(fail: bool) -> None:
    voice, _ = owner()
    entered, release = asyncio.Event(), asyncio.Event()
    native = cast(Any, voice._session)._native
    original = native.call

    async def stalled(operation: str, request: str) -> str:
        if not entered.is_set():
            entered.set()
            await release.wait()
            if fail:
                raise RuntimeError("stalled write failed")
        return await original(operation, request)

    native.call = stalled
    first = asyncio.create_task(voice.send_text("first"))
    await entered.wait()
    queued = []
    for i in range(127):
        send = (
            voice.send_text("queued")
            if i % 4 == 0
            else voice.send_message("queued")
            if i % 4 == 1
            else voice.send_audio(b"\x00\x00")
            if i % 4 == 2
            else voice.request_response()
        )
        queued.append(asyncio.create_task(send))
    await asyncio.sleep(0)
    extra = asyncio.create_task(voice.request_response())
    try:
        with pytest.raises(ValueError, match="input queue"):
            await asyncio.wait_for(asyncio.shield(extra), 0.2)
    finally:
        release.set()
        outcomes = await asyncio.gather(first, *queued, extra, return_exceptions=True)
    assert all(result is None for result in outcomes[1:-1])
    assert isinstance(outcomes[0], RuntimeError) if fail else outcomes[0] is None
    await voice.send_text("reused after settlement")


async def test_pending_input_bytes_are_bounded_and_cancelled_waiters_release_capacity() -> None:
    voice, _ = owner()
    entered, release = asyncio.Event(), asyncio.Event()
    native = cast(Any, voice._session)._native
    original = native.call

    async def stalled(operation: str, request: str) -> str:
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await original(operation, request)

    native.call = stalled
    first = asyncio.create_task(voice.send_text("x" * _LIMIT))
    await entered.wait()
    queued = [
        asyncio.create_task(
            voice.send_message(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": "x" * _LIMIT}],
                }
            )
        )
        for _ in range(6)
    ]
    queued.append(asyncio.create_task(voice.send_audio(b"\x00" * _LIMIT)))
    await asyncio.sleep(0)
    extra = asyncio.create_task(voice.send_text("x"))
    replacement = None
    try:
        with pytest.raises(ValueError, match="input queue"):
            await asyncio.wait_for(asyncio.shield(extra), 0.2)
        queued[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued[0]
        replacement = asyncio.create_task(voice.send_text("y" * _LIMIT))
        await asyncio.sleep(0)
        assert not replacement.done()
    finally:
        release.set()
        outcomes = await asyncio.gather(
            first, *queued, extra, *([replacement] if replacement else []), return_exceptions=True
        )
    assert outcomes[0] is None
    assert isinstance(outcomes[1], asyncio.CancelledError)
    assert all(result is None for result in outcomes[2:8])
    assert outcomes[-1] is None
    await voice.send_message("reused after cancellation")
