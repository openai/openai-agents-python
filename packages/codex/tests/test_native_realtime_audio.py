"""Audio encoding stays bounded before native admission and transport I/O."""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from _agents_codex.realtime import RealtimeAudioInput, RealtimeConfig, RealtimeSession
from agents import Agent, NativeProvider, Session
from tests._asyncio import timeout
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer
from tests.test_native_realtime_adapter import (
    AdapterPeer,
    SocketAdapter,
    adapter_peer as adapter_peer,
)


def live_owner(monkeypatch: pytest.MonkeyPatch) -> tuple[RealtimeSession, list[dict[str, Any]]]:
    owner = RealtimeSession(cast(Session[Any, Any], SimpleNamespace()), config=RealtimeConfig())
    owner._live = True
    owner._ready.set_result(None)
    frames: list[dict[str, Any]] = []

    async def call(operation: str, **params: Any) -> None:
        assert operation == "audio"
        frames.append(params["frame"])

    monkeypatch.setattr(owner, "_call", call)
    return owner, frames


@pytest.mark.parametrize("codec,width", [("pcm16", 2), ("g711_ulaw", 1), ("g711_alaw", 1)])
@pytest.mark.parametrize("tail", [-1, 0, 1])
async def test_audio_encoding_boundary_preserves_bytes_and_frame_samples(
    monkeypatch: pytest.MonkeyPatch, codec: Any, width: int, tail: int
) -> None:
    owner, frames = live_owner(monkeypatch)
    limit = 65536 if width == 2 else 32768
    data = b"\x12\x34" * ((limit + tail * width) // 2)
    if len(data) != limit + tail * width:
        data += b"\x56"
    encode = base64.b64encode
    encoded_sizes: list[int] = []

    def bounded_encode(chunk: Any) -> bytes:
        assert isinstance(chunk, memoryview) and chunk.readonly and chunk.obj is data
        encoded_sizes.append(len(chunk))
        assert len(chunk) <= limit
        return encode(chunk)

    monkeypatch.setattr("_agents_codex.realtime.base64.b64encode", bounded_encode)
    await owner.send_audio(RealtimeAudioInput(data, codec))
    assert encoded_sizes == ([limit, width] if tail == 1 else [len(data)])
    assert b"".join(base64.b64decode(frame["data"]) for frame in frames) == data
    assert [frame["samples_per_channel"] for frame in frames] == [
        size // width for size in encoded_sizes
    ]
    assert all(
        frame["format"] == codec
        and frame["sample_rate"] == (24000 if width == 2 else 8000)
        and frame["num_channels"] == 1
        and frame["item_id"] is None
        for frame in frames
    )


@pytest.mark.parametrize(
    "audio,error",
    [
        (RealtimeAudioInput(b"\0" * 65537, "pcm16"), ValueError),
        (RealtimeAudioInput(b"", "pcm16"), ValueError),
        (RealtimeAudioInput(cast(bytes, bytearray(b"\0\0")), "pcm16"), ValueError),
        (bytearray(b"\0\0"), TypeError),
        (memoryview(b"\0\0"), TypeError),
    ],
)
async def test_invalid_audio_is_rejected_before_any_encoding(
    monkeypatch: pytest.MonkeyPatch, audio: Any, error: type[Exception]
) -> None:
    owner, frames = live_owner(monkeypatch)

    def forbidden_encode(_: Any) -> bytes:
        raise AssertionError("Invalid input must not reach Base64 encoding")

    monkeypatch.setattr("_agents_codex.realtime.base64.b64encode", forbidden_encode)
    with pytest.raises(error):
        await owner.send_audio(audio)
    assert not frames


async def test_audio_waits_before_encoding_next_chunk_and_keeps_calls_contiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, frames = live_owner(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    data = b"A\0" * (32768 + 1)
    second = b"B\0"
    encode = base64.b64encode
    seen: list[object] = []

    def encoding(chunk: Any) -> bytes:
        seen.append(chunk.obj)
        return encode(chunk)

    async def call(operation: str, **params: Any) -> None:
        frames.append(params["frame"])
        if len(frames) == 1:
            entered.set()
            await release.wait()

    monkeypatch.setattr("_agents_codex.realtime.base64.b64encode", encoding)
    monkeypatch.setattr(owner, "_call", call)
    async with timeout(5):
        a = asyncio.create_task(owner.send_audio(data))
        await entered.wait()
        b = asyncio.create_task(owner.send_audio(second))
        try:
            await asyncio.sleep(0)
            assert seen == [data] and len(frames) == 1
            release.set()
            await asyncio.gather(a, b)
            assert [base64.b64decode(frame["data"]) for frame in frames] == [
                data[:65536],
                data[65536:],
                second,
            ]
        finally:
            release.set()
            for task in (a, b):
                if not task.done():
                    task.cancel()
            await asyncio.gather(a, b, return_exceptions=True)


async def test_close_fences_next_chunk_and_waiting_audio_before_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, frames = live_owner(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    # Fill the 8 MiB pending-input budget with room for the two-byte waiter.
    # Only the first 64 KiB may be encoded before admission resumes.
    data = b"A\0" * (4 * 1024 * 1024 - 1)
    encoded_sizes: list[int] = []
    encode = base64.b64encode

    def encoding(chunk: Any) -> bytes:
        encoded_sizes.append(len(chunk))
        return encode(chunk)

    async def call(operation: str, **params: Any) -> None:
        frames.append(params["frame"])
        entered.set()
        await release.wait()

    monkeypatch.setattr("_agents_codex.realtime.base64.b64encode", encoding)
    monkeypatch.setattr(owner, "_call", call)
    async with timeout(5):
        a = asyncio.create_task(owner.send_audio(data))
        await entered.wait()
        b = asyncio.create_task(owner.send_audio(b"B\0"))
        try:
            await asyncio.sleep(0)
            owner._live = False
            release.set()
            results = await asyncio.gather(a, b, return_exceptions=True)
            assert all(isinstance(error, RuntimeError) for error in results)
            assert encoded_sizes == [65536] and len(frames) == 1
        finally:
            release.set()
            for task in (a, b):
                if not task.done():
                    task.cancel()
            await asyncio.gather(a, b, return_exceptions=True)


async def test_cancelled_audio_releases_call_lock_after_admission_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, frames = live_owner(monkeypatch)
    entered, cleanup_started, finish_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def call(operation: str, **params: Any) -> None:
        frames.append(params["frame"])
        if len(frames) == 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await finish_cleanup.wait()

    monkeypatch.setattr(owner, "_call", call)
    async with timeout(5):
        a = asyncio.create_task(owner.send_audio(b"A\0" * 32769))
        await entered.wait()
        b = asyncio.create_task(owner.send_audio(b"B\0"))
        try:
            a.cancel()
            await cleanup_started.wait()
            assert len(frames) == 1
            finish_cleanup.set()
            results = await asyncio.gather(a, b, return_exceptions=True)
            assert isinstance(results[0], asyncio.CancelledError) and results[1] is None
            assert [base64.b64decode(frame["data"]) for frame in frames] == [b"A\0" * 32768, b"B\0"]
        finally:
            finish_cleanup.set()
            for task in (a, b):
                if not task.done():
                    task.cancel()
            await asyncio.gather(a, b, return_exceptions=True)


async def test_codec_update_does_not_relabel_remaining_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, frames = live_owner(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    accepted = "pcm16"

    async def call(operation: str, **params: Any) -> None:
        assert params["frame"]["format"] == "pcm16"
        if accepted != "pcm16":
            raise ValueError("Accepted native codec changed")
        frames.append(params["frame"])
        entered.set()
        await release.wait()

    monkeypatch.setattr(owner, "_call", call)
    async with timeout(5):
        sending = asyncio.create_task(owner.send_audio(b"A\0" * 65537))
        try:
            await entered.wait()
            accepted = "g711_ulaw"
            release.set()
            with pytest.raises(ValueError, match="codec changed"):
                await sending
            assert len(frames) == 1
        finally:
            release.set()
            if not sending.done():
                sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)


@pytest.mark.parametrize("custom", [False, True])
@pytest.mark.parametrize("codec,width", [("pcm16", 2), ("g711_ulaw", 1), ("g711_alaw", 1)])
async def test_local_provider_audio_frames_are_bounded_on_builtin_and_custom_io(
    realtime_peer: RealtimePeer, adapter_peer: AdapterPeer, custom: bool, codec: Any, width: int
) -> None:
    peer = adapter_peer if custom else realtime_peer
    adapter = SocketAdapter(adapter_peer.custom_url) if custom else None
    data = b"A\0" * 32769 if width == 2 else b"A" * 32769
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url, api_key="synthetic-audio-key", supports_websockets=False
        ) as backend,
        Agent(instructions="Listen.", provider=backend).session() as session,
        session.realtime(
            config=RealtimeConfig(
                model="fixture-realtime",
                settings={"input_format": codec, "output_format": codec},
                adapter=adapter,
            )
        ) as voice,
    ):
        start = len(peer.frames)
        await voice.send_audio(RealtimeAudioInput(data, codec))
        await peer.frame(
            lambda frame: (
                frame.get("type") == "input_audio_buffer.append"
                and len(base64.b64decode(frame["audio"])) == width
            ),
            after=start,
        )
        frames = [
            frame
            for frame in peer.frames[start:]
            if frame.get("type") == "input_audio_buffer.append"
        ]
        assert len(frames) == 2
        assert b"".join(base64.b64decode(frame["audio"]) for frame in frames) == data
        assert all(
            len(frame["audio"]) <= 87384 and len(json.dumps(frame)) < 8 * 1024 * 1024
            for frame in frames
        )


@pytest.mark.parametrize("done_before_playback", [False, True])
async def test_playback_capacity_is_reused_for_completed_audio(
    realtime_peer: RealtimePeer, done_before_playback: bool
) -> None:
    from contextlib import aclosing

    from agents.realtime import RealtimeAudio, RealtimePlaybackPosition, RealtimeResponse

    peer = realtime_peer
    async with (
        timeout(60),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-playback-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Help with the call.", provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as owner,
        aclosing(owner.events()) as events,
    ):
        assert peer.socket is not None
        # Exceed the production content capacity with sequential, consumed audio.
        for index in range(1030):
            response_id, item_id = f"played-{index}", f"audio-{index}"
            await peer.socket.send_json(
                {"type": "response.created", "response": {"id": response_id}}
            )
            await peer.socket.send_json(
                {
                    "type": "response.output_audio.delta",
                    "response_id": response_id,
                    "item_id": item_id,
                    "content_index": 0,
                    "output_index": 0,
                    "delta": base64.b64encode(bytes(480)).decode(),
                }
            )
            while not isinstance(await anext(events), RealtimeAudio):
                pass
            position = RealtimePlaybackPosition(response_id, item_id, 0, 10)
            if not done_before_playback:
                owner.report_playback(position)
            await peer.socket.send_json({"type": "response.done", "response": {"id": response_id}})
            while True:
                event = await anext(events)
                if isinstance(event, RealtimeResponse) and event.status == "done":
                    break
            owner.report_playback(position)
        # Recent completion remains available for a late playback report.
        owner.report_playback(position)
        assert not peer.responses.requests


@pytest.mark.parametrize("done,played", [(False, True), (True, False), (True, True)])
async def test_playback_capacity_preserves_unfinished_or_unplayed_audio(
    monkeypatch: pytest.MonkeyPatch, done: bool, played: bool
) -> None:
    from agents.realtime import RealtimePlaybackPosition

    owner, _ = live_owner(monkeypatch)
    monkeypatch.setattr(owner, "_HANDOFF_LIMIT", 1)

    def audio(response_id: str) -> None:
        owner._payload(
            {
                "AudioOut": {
                    "response_id": response_id,
                    "item_id": response_id,
                    "content_index": 0,
                    "sample_rate": 24000,
                    "num_channels": 1,
                    "format": "pcm16",
                    "data": base64.b64encode(bytes(480)).decode(),
                }
            },
            None,
        )
        while not owner._queue.empty():
            owner._dequeue()

    audio("first")
    if done:
        owner._payload({"ResponseDone": {"response_id": "first"}}, None)
    if played:
        owner.report_playback(RealtimePlaybackPosition("first", "first", 0, 10))
    if done and played:
        audio("second")
        with pytest.raises(ValueError, match="does not identify"):
            owner.report_playback(RealtimePlaybackPosition("first", "first", 0, 10))
        owner.report_playback(RealtimePlaybackPosition("second", "second", 0, 10))
    else:
        with pytest.raises(RuntimeError, match="playback item capacity"):
            audio("second")
        # Capacity pressure must not evict a position still needed for playback.
        owner.report_playback(RealtimePlaybackPosition("first", "first", 0, 10))


def receive_playback_audio(owner: RealtimeSession, response_id: str) -> None:
    owner._payload(
        {
            "AudioOut": {
                "response_id": response_id,
                "item_id": response_id,
                "content_index": 0,
                "sample_rate": 24000,
                "num_channels": 1,
                "format": "pcm16",
                "data": base64.b64encode(bytes(480)).decode(),
            }
        },
        None,
    )
    while not owner._queue.empty():
        owner._dequeue()


async def test_playback_reclaims_identity_bytes_for_completed_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.realtime import RealtimePlaybackPosition

    owner, _ = live_owner(monkeypatch)
    monkeypatch.setattr(owner, "_BUFFER_BYTES_LIMIT", 1600)
    for index in range(20):
        response_id = f"{index:02}-" + "x" * 57
        receive_playback_audio(owner, response_id)
        owner._payload({"ResponseDone": {"response_id": response_id}}, None)
        owner.report_playback(RealtimePlaybackPosition(response_id, response_id, 0, 10))
    owner.report_playback(RealtimePlaybackPosition(response_id, response_id, 0, 10))
    with pytest.raises(ValueError, match="does not identify"):
        old_id = "00-" + "x" * 57
        owner.report_playback(RealtimePlaybackPosition(old_id, old_id, 0, 10))


async def test_playback_reclaims_unplayed_audio_after_authoritative_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents.realtime import RealtimePlaybackPosition

    owner, _ = live_owner(monkeypatch)
    monkeypatch.setattr(owner, "_HANDOFF_LIMIT", 1)
    owner._session = cast(
        Session[Any, Any], SimpleNamespace(_check_callback_operation=lambda _: None)
    )

    async def interrupt_receipt(operation: str, **params: Any) -> dict[str, Any]:
        assert operation == "interrupt" and params["response_id"] == "retired"
        return {"retired_response_ids": ["retired"], "cancelled_response_id": "retired"}

    monkeypatch.setattr(owner, "_call", interrupt_receipt)
    receive_playback_audio(owner, "retired")
    await owner.interrupt("retired")
    receive_playback_audio(owner, "next")
    owner.report_playback(RealtimePlaybackPosition("next", "next", 0, 10))
    with pytest.raises(ValueError, match="does not identify"):
        owner.report_playback(RealtimePlaybackPosition("retired", "retired", 0, 10))
