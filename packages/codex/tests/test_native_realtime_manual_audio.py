"""Proposed actual-binding regressions; run only after matching Core is installed."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest

from agents import Agent, NativeProvider
from agents.realtime import RealtimeAudioInput, RealtimeConfig
from tests._asyncio import timeout
from tests.test_native_realtime import realtime_peer as realtime_peer
from tests.test_native_realtime_adapter import SocketAdapter
from tests.test_native_realtime_transports import call_peer as call_peer


class SendHookAdapter(SocketAdapter):
    """Capture a stable send proxy before the native adapter connects."""

    send_hook = None
    forward = None

    async def connect(self, context):
        connection = await super().connect(context)
        forward = connection.send
        self.forward = forward

        async def dispatch(frame):
            hook = self.send_hook
            if hook is None:
                await forward(frame)
            else:
                await hook(frame)

        connection.send = dispatch
        return connection


def adapter_for(peer, custom):
    if not custom:
        return None
    return SendHookAdapter(
        peer.responses.url.replace("http://", "ws://") + "/realtime?model=fixture-realtime"
    )


async def wire_fence(peer, owner):
    await owner.send_text("Audio ordering fence")
    item = await peer.frame(lambda frame: frame["type"] == "conversation.item.create")
    await peer.frame(
        lambda frame: frame["type"] == "response.create",
        after=peer.frames.index(item) + 1,
    )


@pytest.mark.parametrize("custom", [False, True], ids=["native", "custom"])
@pytest.mark.parametrize("codec", ["pcm16", "g711_ulaw", "g711_alaw"])
@pytest.mark.parametrize("commit", [False, True], ids=["append-only", "commit"])
async def test_audio_commit_follows_all_bounded_frames(realtime_peer, custom, codec, commit):
    peer = realtime_peer
    width = 2 if codec == "pcm16" else 1
    source = bytes(range(256)) * (width * 129)  # Two frames, complete PCM samples.
    immutable = bytes(source)
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-manual-commit",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
        session.realtime(
            config=RealtimeConfig(
                model="fixture-realtime",
                adapter=adapter_for(peer, custom),
                settings={"input_format": codec, "turn_detection": None},
            )
        ) as owner,
    ):
        await peer.frame(lambda frame: frame["type"] == "session.update")
        start = len(peer.frames)
        audio = source if codec == "pcm16" else RealtimeAudioInput(source, codec)
        if commit:
            await owner.send_audio(audio, commit=True)
        else:
            await owner.send_audio(audio)  # The unchanged default never commits.
        await wire_fence(peer, owner)
        outgoing = [
            frame
            for frame in peer.frames[start:]
            if frame["type"].startswith("input_audio_buffer.")
        ]
        assert [frame["type"] for frame in outgoing] == ["input_audio_buffer.append"] * 2 + (
            ["input_audio_buffer.commit"] if commit else []
        )
        assert (
            b"".join(
                base64.b64decode(frame["audio"])
                for frame in outgoing
                if frame["type"] == "input_audio_buffer.append"
            )
            == immutable
        )
        assert source == immutable
        assert all(
            len(base64.b64decode(frame["audio"])) <= 32768 * width
            for frame in outgoing
            if frame["type"] == "input_audio_buffer.append"
        )
        # Only the explicit text fence requests a response; committing audio does not.
        assert sum(frame["type"] == "response.create" for frame in peer.frames[start:]) == 1


async def test_invalid_or_rejected_audio_never_commits(realtime_peer):
    peer = realtime_peer
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-rejected-commit",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
        session.realtime(
            config=RealtimeConfig(model="fixture-realtime", settings={"turn_detection": None})
        ) as owner,
    ):
        await peer.frame(lambda frame: frame["type"] == "session.update")
        start = len(peer.frames)
        invalid: Any
        for invalid in (None, 0, 1, "yes"):
            with pytest.raises(TypeError, match="commit must be a bool"):
                await owner.send_audio(b"\0\0", commit=invalid)
        with pytest.raises(RuntimeError, match="accepted input format"):
            await owner.send_audio(RealtimeAudioInput(b"\0", "g711_ulaw"), commit=True)
        await wire_fence(peer, owner)
        assert not any(
            frame["type"].startswith("input_audio_buffer.") for frame in peer.frames[start:]
        )


async def test_closed_and_stale_audio_commit_cannot_reach_replacement(realtime_peer):
    peer = realtime_peer
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-stale-commit",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
    ):
        async with session.realtime(config=RealtimeConfig(model="fixture-realtime")) as old:
            old_generation = old._generation
        await peer.disconnected.wait()
        async with session.realtime(config=RealtimeConfig(model="fixture-realtime")) as current:
            await peer.frame(
                lambda frame: frame["type"] == "session.update",
                after=len(peer.frames) - 1,
            )
            start = len(peer.frames)
            with pytest.raises(RuntimeError):
                await old.send_audio(b"\0\0", commit=True)
            with pytest.raises(RuntimeError):
                await old.send_message("retired")
            with pytest.raises(RuntimeError):
                await old.request_response()
            for operation, extra in (
                ("audio_commit", {}),
                ("response", {}),
                (
                    "input",
                    {
                        "content": [{"type": "input_text", "text": "retired"}],
                        "trigger_response": True,
                    },
                ),
            ):
                with pytest.raises(RuntimeError):
                    await session._native.call(
                        f"realtime_conversation_{operation}_sdk",
                        json.dumps({"generation": old_generation, **extra}),
                    )
            await wire_fence(peer, current)
            assert not any(
                frame["type"].startswith("input_audio_buffer.") for frame in peer.frames[start:]
            )


async def test_partial_custom_audio_cannot_commit_after_close(realtime_peer, monkeypatch):
    peer = realtime_peer
    adapter = adapter_for(peer, True)
    entered, release = asyncio.Event(), asyncio.Event()
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-retired-commit",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime", adapter=adapter)) as owner,
    ):
        await peer.frame(lambda frame: frame["type"] == "session.update")
        original = adapter.forward
        assert original is not None
        count = 0

        async def gated(frame):
            nonlocal count
            if json.loads(frame)["type"] == "input_audio_buffer.append":
                count += 1
                if count == 2:
                    entered.set()
                    await release.wait()
            await original(frame)

        monkeypatch.setattr(adapter, "send_hook", gated)
        start = len(peer.frames)
        sending = asyncio.create_task(owner.send_audio(b"\0\0" * 33000, commit=True))
        try:
            await entered.wait()
            await owner.aclose()
        finally:
            release.set()
            await asyncio.gather(sending, return_exceptions=True)
        assert not any(
            frame["type"] == "input_audio_buffer.commit" for frame in peer.frames[start:]
        )


async def test_concurrent_manual_commits_keep_each_audio_buffer_contiguous(
    realtime_peer, monkeypatch
):
    peer = realtime_peer
    adapter = adapter_for(peer, True)
    entered, release, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    first_audio, second_audio = b"\0\0" * 33000, b"\1\0" * 33000
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-ordered-commits",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime", adapter=adapter)) as owner,
    ):
        await peer.frame(lambda frame: frame["type"] == "session.update")
        original = adapter.forward
        assert original is not None
        count = 0

        async def gated(frame):
            nonlocal count
            if json.loads(frame)["type"] == "input_audio_buffer.append":
                count += 1
                if count == 2:
                    entered.set()
                    await release.wait()
            await original(frame)

        monkeypatch.setattr(adapter, "send_hook", gated)
        start = len(peer.frames)
        first = asyncio.create_task(owner.send_audio(first_audio, commit=True))
        second = None
        try:
            await entered.wait()

            async def send_second():
                second_started.set()
                await owner.send_audio(second_audio, commit=True)

            second = asyncio.create_task(send_second())
            await second_started.wait()
            release.set()
            await asyncio.gather(first, second)
            await wire_fence(peer, owner)
            outgoing = [
                frame
                for frame in peer.frames[start:]
                if frame["type"].startswith("input_audio_buffer.")
            ]
            assert [frame["type"] for frame in outgoing] == [
                "input_audio_buffer.append",
                "input_audio_buffer.append",
                "input_audio_buffer.commit",
            ] * 2
            assert (
                b"".join(base64.b64decode(frame["audio"]) for frame in outgoing[:2]) == first_audio
            )
            assert (
                b"".join(base64.b64decode(frame["audio"]) for frame in outgoing[3:5])
                == second_audio
            )
        finally:
            release.set()
            await asyncio.gather(
                *(task for task in (first, second) if task is not None),
                return_exceptions=True,
            )


async def test_empty_commit_drains_previously_appended_audio(realtime_peer):
    peer = realtime_peer
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-empty-commit",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
        session.realtime(
            config=RealtimeConfig(model="fixture-realtime", settings={"turn_detection": None})
        ) as owner,
    ):
        await peer.frame(lambda frame: frame["type"] == "session.update")
        start = len(peer.frames)
        await owner.send_audio(b"\0\0" * 2400)
        with pytest.raises(ValueError, match="nonempty"):
            await owner.send_audio(b"")
        await owner.send_audio(b"", commit=True)
        await wire_fence(peer, owner)
        outgoing = [
            frame
            for frame in peer.frames[start:]
            if frame["type"].startswith("input_audio_buffer.")
        ]
        assert [frame["type"] for frame in outgoing] == [
            "input_audio_buffer.append",
            "input_audio_buffer.commit",
        ]


async def test_cancelled_pending_commit_joins_before_later_text(realtime_peer, monkeypatch):
    peer = realtime_peer
    adapter = adapter_for(peer, True)
    entered, release, text_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-cancel-commit",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime", adapter=adapter)) as owner,
    ):
        await peer.frame(lambda frame: frame["type"] == "session.update")
        original = adapter.forward
        assert original is not None

        async def gated(frame):
            if json.loads(frame)["type"] == "input_audio_buffer.commit":
                entered.set()
                await release.wait()
            await original(frame)

        monkeypatch.setattr(adapter, "send_hook", gated)
        start = len(peer.frames)
        sending = asyncio.create_task(owner.send_audio(b"\0\0" * 2400, commit=True))
        later = None
        try:
            await entered.wait()
            sending.cancel()

            async def send_later():
                text_started.set()
                await owner.send_text("Must follow the owned commit")

            later = asyncio.create_task(send_later())
            await text_started.wait()
            assert not sending.done(), "Cancellation returned before its owned commit settled"
            assert not later.done()
            release.set()
            results = await asyncio.gather(sending, later, return_exceptions=True)
            assert isinstance(results[0], asyncio.CancelledError)
            assert results[1] is None
            await peer.frame(lambda frame: frame["type"] == "response.create", after=start)
            assert [frame["type"] for frame in peer.frames[start:]] == [
                "input_audio_buffer.append",
                "input_audio_buffer.commit",
                "conversation.item.create",
                "response.create",
            ]
        finally:
            release.set()
            await asyncio.gather(
                *(task for task in (sending, later) if task is not None),
                return_exceptions=True,
            )


async def test_sip_commit_controls_provider_buffer_without_client_media(call_peer):
    from agents.realtime import RealtimeSIP, RealtimeWebRTC
    from tests.test_native_realtime_transports import OFFER_SDP

    peer = call_peer
    async with (
        timeout(30),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-native-call-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
    ):
        async with session.realtime(config=RealtimeConfig(transport=RealtimeWebRTC(OFFER_SDP))):
            pass
        peer.sessions["rtc_external_sip"] = peer.sessions["rtc_native_1"]
        async with session.realtime(
            config=RealtimeConfig(transport=RealtimeSIP("rtc_external_sip"))
        ) as owner:
            with pytest.raises(RuntimeError, match="SIP calls receive audio from the provider"):
                await owner.send_audio(b"\0\0", commit=True)
            await owner.send_audio(b"", commit=True)
            await owner.send_event({"type": "input_audio_buffer.clear", "event_id": "sip-clear"})
            await owner.send_event({"type": "input_audio_buffer.commit", "event_id": "sip-commit"})
            with pytest.raises(RuntimeError, match="SIP"):
                await owner.send_event({"type": "input_audio_buffer.append", "audio": "AAA="})
        await peer.disconnected.wait()
        assert [frame["type"] for frame in peer.frames] == [
            "input_audio_buffer.commit",
            "input_audio_buffer.clear",
            "input_audio_buffer.commit",
        ]
        assert peer.frames[1]["event_id"] == "sip-clear"
        assert peer.frames[2]["event_id"] == "sip-commit"


@pytest.mark.parametrize(
    "kind",
    [
        "input_audio_buffer.append",
        "input_audio_buffer.clear",
        "input_audio_buffer.commit",
    ],
)
async def test_raw_input_adapter_send_rejects_nested_text(realtime_peer, monkeypatch, kind):
    peer = realtime_peer
    adapter = adapter_for(peer, True)
    rejected = asyncio.Event()
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-raw-adapter-reentry",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime", adapter=adapter)) as owner,
    ):
        assert adapter is not None and adapter.forward is not None
        await peer.frame(lambda frame: frame["type"] == "session.update")
        cursor = len(peer.frames)

        async def send(frame):
            if json.loads(frame)["type"] == kind:
                with pytest.raises(RuntimeError, match="adapter callback cannot"):
                    await owner.send_text("Nested adapter text")
                rejected.set()
            await adapter.forward(frame)

        monkeypatch.setattr(adapter, "send_hook", send)
        event = {"type": kind, "event_id": "raw-adapter-reentry"}
        if kind == "input_audio_buffer.append":
            event["audio"] = "AAA="
        await owner.send_event(event)
        await rejected.wait()
        frame = await peer.frame(lambda frame: frame["type"] == kind, after=cursor)
        assert frame["event_id"] == "raw-adapter-reentry"
        assert not any(
            frame["type"] == "conversation.item.create" for frame in peer.frames[cursor:]
        )
