"""Python admission doubles and provider-wire regressions through the native owner."""

import asyncio
import json
from collections import UserDict
from contextlib import aclosing
from types import MappingProxyType
from typing import Any

import pytest

from agents import Agent, NativeProvider, function_tool
from agents.realtime import RealtimeConfig, RealtimeResponse, RealtimeToolCall
from tests._asyncio import timeout
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer
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


@pytest.mark.parametrize(
    "kind",
    [
        "input_audio_buffer.append",
        "input_audio_buffer.clear",
        "input_audio_buffer.commit",
        "response.create",
    ],
)
async def test_raw_input_rejects_nested_adapter_text_until_native_acceptance(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    voice, _ = owner()
    entered, release = asyncio.Event(), asyncio.Event()
    operations: list[str] = []

    async def call(operation: str, **params: Any) -> None:
        operations.append(operation)
        if operation == "send_event":
            with voice._callback_scope(adapter=True):
                with pytest.raises(RuntimeError, match="adapter callback cannot"):
                    await voice.send_text("Nested adapter input")
            entered.set()
            await release.wait()

    monkeypatch.setattr(voice, "_call", call)
    async with timeout(5):
        await voice._input_lock.acquire()
        sending = asyncio.create_task(voice.send_event({"type": kind}))
        try:
            await asyncio.sleep(0)
            with voice._callback_scope(adapter=True):
                with pytest.raises(RuntimeError, match="adapter callback cannot"):
                    await voice.send_text("Nested input while raw dispatch is queued")
        finally:
            voice._input_lock.release()
        try:
            await entered.wait()
            sending.cancel()
            await asyncio.sleep(0)
            assert not sending.done()
            with voice._callback_scope(adapter=True):
                with pytest.raises(RuntimeError, match="adapter callback cannot"):
                    await voice.send_text("Nested input while cancelled caller joins")
        finally:
            release.set()
            await asyncio.gather(sending, return_exceptions=True)
        assert sending.cancelled()
        with voice._callback_scope(adapter=True):
            await voice.send_text("Nested text after raw acceptance")
        assert operations == ["send_event", "text"]
        assert voice._pending_audio_commits == 0


async def test_raw_unknown_provider_event_preserves_payload_on_native_wire(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    event = {
        "type": "provider.custom_control",
        "event_id": "application-custom-control",
        "payload": {"nested": [{"enabled": True, "value": None}, [1, "synthetic"]]},
    }
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-provider-extension",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
    ):
        cursor = len(peer.frames)
        assert await voice.send_event(event) is None
        frame = await peer.frame(
            lambda frame: frame.get("event_id") == event["event_id"], after=cursor
        )
        assert frame == event
        assert peer.responses.requests == []


async def test_raw_malformed_known_event_never_reaches_native_wire(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-malformed-response",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
    ):
        cursor = len(peer.frames)
        with pytest.raises(RuntimeError):
            await voice.send_event(
                {"type": "response.create", "event_id": "invalid-response-shape", "response": []}
            )
        await voice.send_event({"type": "response.create", "event_id": "valid-after-rejection"})
        await peer.frame(
            lambda frame: frame.get("event_id") == "valid-after-rejection", after=cursor
        )
        assert not any(
            frame.get("event_id") == "invalid-response-shape" for frame in peer.frames[cursor:]
        )
        assert not peer.disconnected.is_set()


async def test_raw_response_tool_choice_override_preserves_next_response_default(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer

    @function_tool
    def lookup() -> str:
        raise AssertionError("The provider fixture must not request a tool")

    named_choice = {"type": "function", "name": "lookup"}
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-response-tool-choice",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", tools=[lookup], provider=provider).session() as session,
        session.realtime(
            config=RealtimeConfig(model="fixture-realtime", settings={"tool_choice": named_choice})
        ) as voice,
        aclosing(voice.events()) as events,
    ):
        initial = await peer.frame(lambda frame: frame["type"] == "session.update")
        assert initial["session"]["tool_choice"] == named_choice
        lookup_tool = next(tool for tool in initial["session"]["tools"] if tool["name"] == "lookup")
        cursor = len(peer.frames)
        for event_id, overrides in (
            ("one-response-without-tools", {"tool_choice": "none"}),
            ("one-response-with-lookup", {"tools": [lookup_tool], "tool_choice": named_choice}),
            ("next-response-default", {}),
        ):
            await voice.send_event(
                {"type": "response.create", "event_id": event_id, "response": overrides}
            )
            frame = await peer.frame(
                lambda frame, event_id=event_id: frame.get("event_id") == event_id, after=cursor
            )
            cursor = peer.frames.index(frame, cursor) + 1
            assert frame.get("response", {}) == overrides
            async for event in events:
                if isinstance(event, RealtimeResponse) and event.status == "done":
                    break
        assert len([frame for frame in peer.frames if frame["type"] == "session.update"]) == 1


async def test_raw_detached_explicit_tools_remain_application_controlled(
    realtime_peer: RealtimePeer, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = realtime_peer
    effects: list[str] = []

    @function_tool
    def native_lookup() -> str:
        effects.append("native lookup")
        return "Native execution must not run for detached provider calls."

    tools = [
        {
            "type": "function",
            "name": "application_lookup",
            "description": "The application owns this function.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "type": "function",
            "name": "native_lookup",
            "parameters": {"type": "object", "properties": {}},
        },
    ]
    response = {
        "conversation": "none",
        "input": [],
        "output_modalities": ["text"],
        "tools": tools,
        "tool_choice": {"type": "function", "name": "application_lookup"},
    }
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-detached-explicit-tools",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Listen.", tools=[native_lookup], provider=provider
        ).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
        aclosing(voice.events()) as events,
    ):
        assert peer.socket is not None
        send_json = peer.socket.send_json

        async def detached_calls(data: Any, *args: Any, **kwargs: Any) -> None:
            if data.get("type") == "response.done":
                response_id = data["response"]["id"]
                calls = []
                for index, name in enumerate(("application_lookup", "native_lookup")):
                    item = {
                        "id": f"detached-item-{index}",
                        "type": "function_call",
                        "name": name,
                        "call_id": f"detached-call-{index}",
                        "arguments": "{}",
                    }
                    calls.append(item)
                    await send_json(
                        {
                            "type": "response.output_item.done",
                            "response_id": response_id,
                            "output_index": index,
                            "item": item,
                        }
                    )
                data = {**data, "response": {**data["response"], "output": calls}}
            await send_json(data, *args, **kwargs)

        monkeypatch.setattr(peer.socket, "send_json", detached_calls)
        cursor = len(peer.frames)
        await voice.send_event(
            {"type": "response.create", "event_id": "detached-explicit-tools", "response": response}
        )
        frame = await peer.frame(
            lambda frame: frame.get("event_id") == "detached-explicit-tools", after=cursor
        )
        assert frame["response"] == response
        seen = []
        async for event in events:
            seen.append(event)
            if isinstance(event, RealtimeResponse) and event.status == "done":
                break
        assert not any(isinstance(event, RealtimeToolCall) for event in seen)
    assert effects == []
    assert peer.responses.requests == []
    assert not any(
        frame.get("item", {}).get("type") == "function_call_output" for frame in peer.frames
    )


async def test_bulk_history_replays_completed_function_pair_without_native_dispatch(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    effects: list[str] = []

    @function_tool
    def lookup(key: str) -> str:
        effects.append(key)
        return "A completed application call must not execute again."

    call = {
        "id": "application-function-call",
        "type": "function_call",
        "status": "completed",
        "name": "lookup",
        "call_id": "application-call",
        "arguments": '{"key":"already-completed"}',
    }
    output = {
        "id": "application-function-output",
        "type": "function_call_output",
        "status": "completed",
        "call_id": "application-call",
        "output": "Application-owned completed result.",
    }
    replacement = [call, output]
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-bulk-function-replay",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", tools=[lookup], provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
    ):
        assert peer.socket is not None
        cursor = len(peer.frames)
        start = cursor
        updating = asyncio.create_task(voice.update_history(replacement))
        try:
            predecessor = "root"
            for item in replacement:
                frame = await peer.frame(
                    lambda frame: frame["type"] == "conversation.item.create", after=cursor
                )
                cursor = peer.frames.index(frame, cursor) + 1
                assert frame["item"] == item
                assert frame["previous_item_id"] == predecessor
                assert not updating.done()
                await peer.socket.send_json(
                    {
                        "type": "conversation.item.created",
                        "previous_item_id": predecessor,
                        "item": item,
                    }
                )
                predecessor = item["id"]
            assert await updating is None
            retrieving = asyncio.create_task(voice.retrieve_conversation_history())
            try:
                for item in replacement:
                    request = await peer.frame(
                        lambda frame: frame["type"] == "conversation.item.retrieve", after=cursor
                    )
                    cursor = peer.frames.index(request, cursor) + 1
                    assert request["item_id"] == item["id"]
                    await peer.socket.send_json(
                        {"type": "conversation.item.retrieved", "item": item}
                    )
                assert await retrieving == replacement
            finally:
                if not retrieving.done():
                    await voice.aclose()
                await asyncio.gather(retrieving, return_exceptions=True)
            creations = [
                frame
                for frame in peer.frames[start:]
                if frame["type"] == "conversation.item.create"
            ]
            assert [frame["item"] for frame in creations] == replacement
            assert not any(frame["type"] == "response.create" for frame in peer.frames[start:])
        finally:
            if not updating.done():
                await voice.aclose()
            await asyncio.gather(updating, return_exceptions=True)
    assert effects == []
    assert peer.responses.requests == []
