"""Python admission doubles and provider-wire regressions through the native owner."""

import asyncio
import base64
import json
from collections import UserDict
from contextlib import aclosing
from copy import deepcopy
from types import MappingProxyType
from typing import Any

import pytest

from agents import Agent, NativeProvider, function_tool
from agents.realtime import (
    RealtimeAudio,
    RealtimeConfig,
    RealtimeDetachedFunctionCall,
    RealtimeResponse,
    RealtimeToolCall,
)
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
            forwarded = dict(frame.get("response", {}))
            if overrides:
                metadata = forwarded.pop("metadata")
                assert set(metadata) == {"codex_response_id"}
                assert isinstance(metadata["codex_response_id"], str)
                assert metadata["codex_response_id"]
            assert forwarded == overrides
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
        "tool_choice": {"type": "function", "name": "native_lookup"},
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

        issued = False

        async def detached_calls(data: Any, *args: Any, **kwargs: Any) -> None:
            nonlocal issued
            if data.get("type") == "response.done" and not issued:
                issued = True
                response_id = data["response"]["id"]
                calls = []
                for index, name in enumerate(("application_lookup", "native_lookup")):
                    item = {
                        "id": f"detached-item-{index}",
                        "type": "function_call",
                        "name": name,
                        "call_id": f"detached-call-{index}",
                        "arguments": '{"value":"synthetic"}',
                    }
                    calls.append(item)
                    completed = {
                        "type": "response.output_item.done",
                        "response_id": response_id,
                        "output_index": index,
                        "item": item,
                    }
                    await send_json(completed)
                    await send_json(completed)
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
        response_id = None
        async for event in events:
            seen.append(event)
            if isinstance(event, RealtimeResponse) and event.status == "done":
                response_id = event.response_id
                break
        assert not any(isinstance(event, RealtimeToolCall) for event in seen)
        assert response_id is not None
        calls = [event for event in seen if isinstance(event, RealtimeDetachedFunctionCall)]
        assert calls == [
            RealtimeDetachedFunctionCall(
                response_id,
                "detached-item-1",
                "detached-call-1",
                "native_lookup",
                '{"value":"synthetic"}',
            )
        ]
        assert not any(
            item.provider_item_id in {call.item_id for call in calls} for item in voice.history
        )
        assert not any(
            item.get("id") in {call.item_id for call in calls}
            for item in await voice.retrieve_conversation_history()
        )
        followup = {
            "conversation": "none",
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": "synthetic result",
                }
                for call in calls
            ],
            "output_modalities": ["text"],
        }
        await voice.send_event(
            {"type": "response.create", "event_id": "detached-result", "response": followup}
        )
        forwarded = await peer.frame(lambda frame: frame.get("event_id") == "detached-result")
        assert forwarded["response"] == {**followup, "tools": [], "tool_choice": "none"}
        async for event in events:
            assert not isinstance(event, (RealtimeDetachedFunctionCall, RealtimeToolCall))
            if isinstance(event, RealtimeResponse) and event.status == "done":
                break
        assert not any(
            item.provider_item_id in {call.item_id for call in calls} for item in voice.history
        )
        assert not any(
            item.get("id") in {call.item_id for call in calls}
            for item in await voice.retrieve_conversation_history()
        )
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


async def test_raw_response_prompt_and_audio_overrides_preserve_session_defaults(
    realtime_peer: RealtimePeer, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = realtime_peer
    session_prompt = {"id": "pmpt_session_default"}
    pcm = {"type": "audio/pcm", "rate": 24000}
    cases: list[tuple[dict[str, Any], str, int]] = [
        (
            {
                "prompt": {
                    "id": "pmpt_one_response",
                    "version": "2",
                    "variables": {"topic": "rainbows"},
                },
                "audio": {
                    "output": {"voice": {"id": "voice_synthetic"}, "format": {"type": "audio/pcmu"}}
                },
            },
            "g711_ulaw",
            8000,
        ),
        (
            {
                "prompt": {"id": "pmpt_without_optional_fields"},
                "audio": {"output": {"voice": "marin", "format": {"type": "audio/pcma"}}},
            },
            "g711_alaw",
            8000,
        ),
        (
            {"prompt": None, "audio": {"output": {"voice": "alloy", "format": pcm}}},
            "pcm16",
            24000,
        ),
        ({}, "pcm16", 24000),
    ]
    async with (
        timeout(25),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-response-prompt-audio",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
        session.realtime(
            config=RealtimeConfig(
                model="fixture-realtime",
                voice="alloy",
                settings={"output_format": "pcm16", "prompt": {"id": "pmpt_session_default"}},
            )
        ) as voice,
        aclosing(voice.events()) as events,
    ):
        initial = await peer.frame(lambda frame: frame["type"] == "session.update")
        assert initial["session"]["prompt"] == session_prompt
        assert initial["session"]["audio"]["output"]["format"] == pcm
        assert initial["session"]["audio"]["output"]["voice"] == "alloy"
        assert peer.socket is not None
        send_json = peer.socket.send_json

        async def audio_before_done(data: Any, *args: Any, **kwargs: Any) -> None:
            if data.get("type") == "response.done":
                response_id = data["response"]["id"]
                await send_json(
                    {
                        "type": "response.output_audio.delta",
                        "response_id": response_id,
                        "item_id": f"audio-{response_id}",
                        "content_index": 0,
                        "output_index": 0,
                        "delta": base64.b64encode(b"\x01\x02").decode("ascii"),
                    }
                )
            await send_json(data, *args, **kwargs)

        monkeypatch.setattr(peer.socket, "send_json", audio_before_done)
        cursor = len(peer.frames)
        for index, (overrides, codec, rate) in enumerate(cases):
            event = {
                "type": "response.create",
                "event_id": f"response-prompt-audio-{index}",
                "response": overrides,
            }
            assert await voice.send_event(event) is None
            frame = await peer.frame(
                lambda frame, event_id=event["event_id"]: frame.get("event_id") == event_id,
                after=cursor,
            )
            cursor = peer.frames.index(frame, cursor) + 1
            forwarded = dict(frame.get("response", {}))
            if overrides:
                metadata = forwarded.pop("metadata")
                assert set(metadata) == {"codex_response_id"}
                assert isinstance(metadata["codex_response_id"], str)
                assert metadata["codex_response_id"]
            assert forwarded == overrides
            audio = []
            async for observed in events:
                if isinstance(observed, RealtimeAudio):
                    audio.append(observed)
                elif isinstance(observed, RealtimeResponse) and observed.status == "done":
                    assert len(audio) == 1
                    assert audio[0].response_id == observed.response_id
                    break
            assert [(chunk.data, chunk.format, chunk.sample_rate) for chunk in audio] == [
                (b"\x01\x02", codec, rate)
            ]
        assert len([frame for frame in peer.frames if frame["type"] == "session.update"]) == 1
        assert peer.responses.requests == []


async def test_raw_response_prompt_and_audio_validation_rejects_before_wire(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    invalid = [
        {"prompt": []},
        {"prompt": {"id": ""}},
        {"prompt": {"id": "pmpt_bad_version", "version": 2}},
        {"prompt": {"id": "pmpt_bad_variables", "variables": []}},
        {"audio": {"output": {"voice": 42}}},
        {"audio": {"output": {"voice": "  "}}},
        {"audio": {"output": {"voice": {"id": ""}}}},
        {"audio": {"output": {"format": {"type": "audio/pcm", "rate": 16000}}}},
        {"audio": {"output": {"format": {"type": "audio/opus"}}}},
    ]
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-invalid-response-options",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
    ):
        cursor = len(peer.frames)
        rejected_ids = set()
        for index, response in enumerate(invalid):
            event_id = f"invalid-prompt-audio-{index}"
            rejected_ids.add(event_id)
            with pytest.raises(RuntimeError):
                await voice.send_event(
                    {"type": "response.create", "event_id": event_id, "response": response}
                )
        await voice.send_event({"type": "response.create", "event_id": "valid-after-options"})
        await peer.frame(lambda frame: frame.get("event_id") == "valid-after-options", after=cursor)
        assert not any(frame.get("event_id") in rejected_ids for frame in peer.frames[cursor:])
        assert not peer.disconnected.is_set()


async def test_raw_session_tools_replace_exact_declarations_and_preserve_sparse_updates(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer

    @function_tool
    def lookup(key: str) -> str:
        raise AssertionError("The fixture has not been requesting native tools")

    @function_tool
    def other() -> str:
        raise AssertionError("The fixture has not been requesting native tools")

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-raw-session-tools",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Listen.", tools=[lookup, other], provider=provider
        ).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
    ):
        initial = await peer.frame(lambda frame: frame["type"] == "session.update")
        declaration = next(tool for tool in initial["session"]["tools"] if tool["name"] == "lookup")
        assert {"lookup", "other"} <= {tool["name"] for tool in initial["session"]["tools"]}
        cursor = len(peer.frames)
        await voice.send_event(
            {
                "type": "session.update",
                "event_id": "replace-native-tools",
                "session": {"tools": [declaration]},
            }
        )
        replaced = await peer.frame(
            lambda frame: frame.get("event_id") == "replace-native-tools", after=cursor
        )
        assert replaced["session"]["tools"] == [declaration]
        altered = deepcopy(declaration)
        altered["parameters"] = {"type": "object", "properties": {"forged": {"type": "string"}}}
        with pytest.raises(RuntimeError):
            await voice.send_event(
                {
                    "type": "session.update",
                    "event_id": "altered-native-tool",
                    "session": {"tools": [altered]},
                }
            )
        await voice.send_event(
            {
                "type": "session.update",
                "event_id": "sparse-instructions",
                "session": {"instructions": "Still listen."},
            }
        )
        sparse = await peer.frame(
            lambda frame: frame.get("event_id") == "sparse-instructions", after=cursor
        )
        assert "tools" not in sparse["session"]
        await voice.send_event(
            {"type": "session.update", "event_id": "clear-native-tools", "session": {"tools": []}}
        )
        cleared = await peer.frame(
            lambda frame: frame.get("event_id") == "clear-native-tools", after=cursor
        )
        assert cleared["session"]["tools"] == []
        assert not any(
            frame.get("event_id") == "altered-native-tool" for frame in peer.frames[cursor:]
        )
        assert peer.responses.requests == []


async def test_raw_session_caller_function_uses_public_notice_and_exact_output_ack(
    realtime_peer: RealtimePeer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agents.realtime import RealtimeConversationItemObserved

    peer = realtime_peer
    declaration = {
        "type": "function",
        "name": "application_echo",
        "description": "An application-owned synthetic echo.",
        "parameters": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    }
    item = {
        "id": "application-call-item",
        "type": "function_call",
        "status": "completed",
        "name": "application_echo",
        "call_id": "application-call",
        "arguments": '{"value":"synthetic"}',
    }
    async with (
        timeout(25),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-caller-function",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
        aclosing(voice.events()) as events,
    ):
        assert peer.socket is not None
        send_json = peer.socket.send_json
        issued = False

        async def issue_application_call(data: Any, *args: Any, **kwargs: Any) -> None:
            nonlocal issued
            if data.get("type") == "response.done" and not issued:
                issued = True
                response_id = data["response"]["id"]
                await send_json(
                    {
                        "type": "response.output_item.done",
                        "response_id": response_id,
                        "output_index": 0,
                        "item": item,
                    }
                )
                await send_json({"type": "conversation.item.done", "item": item})
                await send_json({"type": "conversation.item.done", "item": item})
                await send_json(
                    {
                        "type": "input_audio_buffer.speech_started",
                        "audio_start_ms": 0,
                        "item_id": "synthetic-speech",
                    }
                )
                data = {**data, "response": {**data["response"], "output": [item]}}
            await send_json(data, *args, **kwargs)

        monkeypatch.setattr(peer.socket, "send_json", issue_application_call)
        cursor = len(peer.frames)
        await voice.send_event(
            {
                "type": "session.update",
                "event_id": "install-application-function",
                "session": {
                    "tools": [declaration],
                    "audio": {
                        "input": {
                            "turn_detection": {
                                "type": "server_vad",
                                "interrupt_response": False,
                            }
                        }
                    },
                },
            }
        )
        installed = await peer.frame(
            lambda frame: frame.get("event_id") == "install-application-function", after=cursor
        )
        assert installed["session"]["tools"] == [declaration]
        await voice.send_event(
            {
                "type": "response.create",
                "response": {"tool_choice": {"type": "function", "name": "application_echo"}},
            }
        )
        completed_notices = []
        async for event in events:
            assert not isinstance(event, RealtimeToolCall)
            if (
                isinstance(event, RealtimeConversationItemObserved)
                and event.item_id == item["id"]
                and event.status == "done"
            ):
                assert event.kind == "function_call"
                assert event.call_id == item["call_id"]
                completed_notices.append(event)
            if isinstance(event, RealtimeResponse) and event.status == "done":
                break
        assert len(completed_notices) == 1

        output_arrived, release_output = asyncio.Event(), asyncio.Event()

        async def acknowledge_items() -> None:
            after = cursor
            while True:
                request = await peer.frame(
                    lambda frame: (
                        frame["type"] in {"conversation.item.retrieve", "conversation.item.create"}
                    ),
                    after=after,
                )
                after = peer.frames.index(request, after) + 1
                if request["type"] == "conversation.item.retrieve":
                    assert request["item_id"] == item["id"]
                    await send_json({"type": "conversation.item.retrieved", "item": item})
                else:
                    if request.get("event_id") == "accepted-application-output":
                        output_arrived.set()
                        await release_output.wait()
                        await send_json(
                            {
                                "type": "conversation.item.added",
                                "previous_item_id": None,
                                "item": item,
                            }
                        )
                    await send_json({"type": "conversation.item.created", "item": request["item"]})

        acknowledgements = asyncio.create_task(acknowledge_items())
        sending: asyncio.Task[dict[str, Any] | None] | None = None
        try:
            retrieved = await voice.retrieve_conversation_item(item["id"])
            assert retrieved["name"] == "application_echo"
            assert json.loads(retrieved["arguments"]) == {"value": "synthetic"}
            output = {
                "type": "function_call_output",
                "id": "application-output-item",
                "call_id": item["call_id"],
                "output": '{"echo":"synthetic"}',
            }
            with pytest.raises(RuntimeError):
                await voice.send_event(
                    {
                        "type": "conversation.item.create",
                        "event_id": "unrelated-application-output",
                        "item": {**output, "id": "unrelated-output-item", "call_id": "unrelated"},
                    }
                )
            sending = asyncio.create_task(
                voice.send_event(
                    {
                        "type": "conversation.item.create",
                        "event_id": "accepted-application-output",
                        "item": output,
                    }
                )
            )
            await output_arrived.wait()
            assert not sending.done()
            release_output.set()
            assert await sending is None
            with pytest.raises(RuntimeError):
                await voice.send_event(
                    {
                        "type": "conversation.item.create",
                        "event_id": "duplicate-application-output",
                        "item": {**output, "id": "duplicate-output-item"},
                    }
                )
            outputs = [
                frame
                for frame in peer.frames[cursor:]
                if frame["type"] == "conversation.item.create"
                and frame["item"]["type"] == "function_call_output"
            ]
            assert [frame.get("event_id") for frame in outputs] == ["accepted-application-output"]
            assert outputs[0]["item"] == {**output, "status": "completed"}
            assert peer.responses.requests == []
        finally:
            release_output.set()
            if sending is not None:
                await asyncio.gather(sending, return_exceptions=True)
            acknowledgements.cancel()
            await asyncio.gather(acknowledgements, return_exceptions=True)
