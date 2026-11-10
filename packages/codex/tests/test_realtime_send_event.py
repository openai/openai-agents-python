"""Python admission and forwarding; these doubles do not verify Core behavior."""

import asyncio
import json
from collections import UserDict
from types import MappingProxyType
from typing import Any

import pytest

from tests._asyncio import timeout
from tests.test_realtime_provider_history import owner


@pytest.mark.parametrize("retrieval", [False, True])
async def test_send_event_captures_mapping_and_returns_native_result(
    monkeypatch: pytest.MonkeyPatch, retrieval: bool
) -> None:
    voice, _ = owner()
    entered, release = asyncio.Event(), asyncio.Event()
    requests: list[tuple[str, dict[str, Any]]] = []
    result = {"id": "provider-item", "content": [{"audio": "AQI="}]} if retrieval else None

    async def call(operation: str, request: str) -> str:
        requests.append((operation, json.loads(request)))
        entered.set()
        await release.wait()
        return json.dumps(result)

    monkeypatch.setattr(voice._session._native, "call", call)
    metadata = {"trace": "original"}
    event = UserDict({"type": "response.create", "response": MappingProxyType(metadata)})
    async with timeout(5):
        sending = asyncio.create_task(voice.send_event(event))
        try:
            await entered.wait()
            metadata["trace"] = "mutated"
            event["type"] = "mutated"
        finally:
            release.set()
        assert await sending == result
    assert requests == [
        (
            "realtime_conversation_send_event_sdk",
            {
                "generation": voice._generation,
                "event": {"type": "response.create", "response": {"trace": "original"}},
            },
        )
    ]


async def test_send_event_cancellation_keeps_admission_until_native_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice, _ = owner()
    entered, release, accepted = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def call(operation: str, request: str) -> str:
        entered.set()
        await release.wait()
        accepted.set()
        return "null"

    monkeypatch.setattr(voice._session._native, "call", call)
    event = {"type": "response.create", "response": {"instructions": "a" * 1_100_000}}
    async with timeout(5):
        sending = asyncio.create_task(voice.send_event(event))
        try:
            await entered.wait()
            sending.cancel()
            await asyncio.sleep(0)
            assert not sending.done()
            with pytest.raises(ValueError, match="input queue"):
                await voice.send_event(event)
            assert not accepted.is_set()
        finally:
            release.set()
            await asyncio.gather(sending, return_exceptions=True)
        assert sending.cancelled()
        assert accepted.is_set()
        assert await voice.send_event(event) is None


async def test_send_event_rejects_oversize_before_json_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice, requests = owner()

    def unexpected(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Oversized event must be rejected before serialization")

    monkeypatch.setattr("_agents_codex.realtime.json.dumps", unexpected)
    with pytest.raises(ValueError, match="8 MiB"):
        await voice.send_event({"type": "response.create", "payload": "a" * 2_100_000})
    assert requests == []


async def test_send_event_rejects_adapter_reentry_and_closed_owner() -> None:
    voice, requests = owner()
    with voice._callback_scope(adapter=True):
        with pytest.raises(RuntimeError, match="adapter callback cannot"):
            await voice.send_event({"type": "response.cancel"})
    voice._live = False
    with pytest.raises(RuntimeError):
        await voice.send_event({"type": "response.cancel"})
    assert requests == []


async def test_send_event_requires_mapping() -> None:
    voice, requests = owner()
    with pytest.raises(TypeError, match="Mapping"):
        await voice.send_event([])  # type: ignore[arg-type]
    assert requests == []


@pytest.mark.parametrize("rejected", [False, True], ids=["accepted", "native-rejection"])
async def test_raw_cancel_retires_only_receipted_playback_after_acceptance(
    monkeypatch: pytest.MonkeyPatch, rejected: bool
) -> None:
    from _agents_codex.realtime import RealtimeAudio, RealtimePlaybackInterrupted

    voice, requests = owner()
    voice._session._check_callback_operation = lambda _: None  # type: ignore[method-assign]
    old = RealtimeAudio(b"old", 24000, 1, response_id="old-response")
    newer = RealtimeAudio(b"new", 24000, 1, response_id="new-response")
    voice._emit(old)
    voice._emit(newer)
    entered, release = asyncio.Event(), asyncio.Event()

    async def call(operation: str, request: str) -> str:
        requests.append((operation, json.loads(request)))
        entered.set()
        await release.wait()
        if rejected:
            raise RuntimeError("Synthetic stale cancel rejection")
        return json.dumps(
            {
                "retired_response_ids": ["old-response"],
                "cancelled_response_id": "old-response",
                "joined_root_turn_ids": [],
            }
        )

    monkeypatch.setattr(voice._session._native, "call", call)
    event = {
        "type": "response.cancel",
        "event_id": "cancel-event",
        "response_id": "old-response",
    }
    async with timeout(5):
        sending = asyncio.create_task(voice.send_event(event))
        try:
            await entered.wait()
            assert voice._queue.qsize() == 2
        finally:
            release.set()
        if rejected:
            with pytest.raises(RuntimeError, match="stale cancel"):
                await sending
            assert voice._dequeue() == old
            assert voice._dequeue() == newer
            assert voice._live
        else:
            assert await sending is None
            assert voice._dequeue() == newer
            assert voice._dequeue() == RealtimePlaybackInterrupted(
                "old-response", None, ("old-response",), "old-response"
            )
        assert voice._queue.empty()
    assert requests == [
        ("realtime_conversation_send_event_sdk", {"generation": voice._generation, "event": event})
    ]


async def test_raw_cancel_rejects_callback_before_native_dispatch() -> None:
    from _agents_codex.session import Session

    voice, requests = owner()
    voice._session._check_callback_operation = lambda operation: Session._check_callback_operation(
        voice._session, operation
    )  # type: ignore[method-assign]
    with voice._callback_scope():
        with pytest.raises(RuntimeError, match="callback cannot"):
            await voice.send_event({"type": "response.cancel"})
    assert requests == []


@pytest.mark.parametrize(
    "kind",
    [
        "input_audio_buffer.append",
        "input_audio_buffer.clear",
        "input_audio_buffer.commit",
        "response.create",
    ],
)
async def test_raw_input_waits_for_all_typed_audio_chunks(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    voice, _ = owner()
    entered, release = asyncio.Event(), asyncio.Event()
    operations: list[str] = []

    async def call(operation: str, **params: Any) -> None:
        operations.append(operation)
        if operation == "audio" and not entered.is_set():
            entered.set()
            await release.wait()

    monkeypatch.setattr(voice, "_call", call)
    async with timeout(5):
        audio = asyncio.create_task(voice.send_audio(b"\x01\x02" * 32769))
        raw: asyncio.Task[Any] | None = None
        try:
            await entered.wait()
            raw = asyncio.create_task(voice.send_event({"type": kind}))
            await asyncio.sleep(0)
            assert operations == ["audio"]
        finally:
            release.set()
            await asyncio.gather(audio, *([raw] if raw else []))
        assert operations == ["audio", "audio", "send_event"]
