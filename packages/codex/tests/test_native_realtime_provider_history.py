"""Public provider configuration and history operations through the native engine."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from typing import Any

import pytest

from agents import Agent, NativeProvider, function_tool
from agents.realtime import RealtimeConfig, RealtimeConversationItemObserved, RealtimeSIP
from tests._asyncio import timeout
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer
from tests.test_native_realtime_transports import CallPeer, call_peer as call_peer


async def test_native_sip_projection_matches_connection_configuration(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer

    @function_tool
    async def lookup(city: str) -> str:
        """Find a synthetic city forecast."""
        return city

    def no_voice_credentials() -> str:
        raise AssertionError("Call-accept projection must not resolve voice credentials")

    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-sip-projection-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Use the captured forecast tool.", tools=[lookup], provider=provider
        ).session() as session,
    ):
        config = RealtimeConfig(
            model="fixture-realtime",
            voice="marin",
            instructions="Speak the synthetic forecast.",
            settings={
                "turn_detection": None,
                "transcription": {"model": "gpt-4o-transcribe", "language": "en"},
                "output_speed": 1.25,
                "max_output_tokens": 256,
                "parallel_tool_calls": False,
            },
            tools=(lookup,),
        )
        from dataclasses import replace

        projected = await session.build_realtime_sip_config(
            config=replace(config, api_key=no_voice_credentials)
        )
        assert peer.connections == 0
        assert peer.frames == []
        async with session.realtime(config=config):
            initial = await peer.frame(lambda frame: frame["type"] == "session.update")
            expected = {"model": "fixture-realtime", **initial["session"]}
            expected.pop("id", None)
            assert projected == expected
            assert "id" not in projected
            assert projected["type"] == "realtime"
            assert projected["model"] == "fixture-realtime"
            assert "Speak the synthetic forecast." in projected["instructions"]
            assert projected["audio"]["output"]["voice"] == "marin"
            assert projected["audio"]["output"]["speed"] == 1.25
            assert projected["audio"]["input"]["turn_detection"] is None
            assert projected["audio"]["input"]["transcription"]["language"] == "en"
            assert projected["max_output_tokens"] == 256
            tool = next(tool for tool in projected["tools"] if tool["name"] == "lookup")
            assert tool["parameters"]["properties"]["city"]["type"] == "string"
            assert "city" in tool["parameters"]["required"]


async def observe_item(
    peer: RealtimePeer | CallPeer,
    events: Any,
    item: dict[str, Any],
    *,
    previous_item_id: str | None = None,
    include_previous_item_id: bool = True,
) -> None:
    assert peer.socket is not None
    for kind in ("conversation.item.added", "conversation.item.done"):
        frame = {"type": kind, "item": item}
        if include_previous_item_id:
            frame["previous_item_id"] = previous_item_id
        await peer.socket.send_json(frame)
    async for event in events:
        if (
            isinstance(event, RealtimeConversationItemObserved)
            and event.item_id == item["id"]
            and event.status == "done"
        ):
            return
    pytest.fail("Voice events ended before the provider item was observed")


async def test_native_provider_item_retrieval_keeps_full_media_separate_from_audit(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    item = {
        "id": "provider-audio-item",
        "type": "message",
        "role": "user",
        "status": "completed",
        "content": [{"type": "input_audio", "audio": "AQI=", "transcript": "Synthetic audio"}],
    }
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-history-retrieval-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Keep provider audio separate from the audit.", provider=provider
        ).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
        aclosing(voice.events()) as events,
    ):
        await observe_item(peer, events, item)
        retrieval = asyncio.create_task(voice.retrieve_conversation_item(item["id"]))
        try:
            request = await peer.frame(lambda frame: frame["type"] == "conversation.item.retrieve")
            assert request["item_id"] == item["id"]
            assert not retrieval.done()
            assert peer.socket is not None
            await peer.socket.send_json({"type": "conversation.item.retrieved", "item": item})
            assert await retrieval == item
            assert "AQI=" not in repr(voice.history)
        finally:
            if not retrieval.done():
                await voice.aclose()
            await asyncio.gather(retrieval, return_exceptions=True)


@pytest.mark.parametrize("cancel", [False, True])
async def test_native_history_replacement_waits_for_ordered_provider_acknowledgements(
    realtime_peer: RealtimePeer, cancel: bool
) -> None:
    peer = realtime_peer
    first = {
        "id": "history-first",
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "Keep this message"}],
    }
    old = {
        "id": "history-old",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "Replace this message"}],
    }
    replacement = {
        "id": "history-new",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "Replacement message"}],
    }
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-history-replacement-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Edit provider messages safely.", provider=provider
        ).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
        aclosing(voice.events()) as events,
    ):
        await observe_item(peer, events, {**first, "status": "completed"})
        await observe_item(
            peer, events, {**old, "status": "completed"}, previous_item_id=first["id"]
        )
        start = len(peer.frames)
        updating = asyncio.create_task(voice.update_history([first, replacement]))
        try:
            for item in (first, old):
                frame = await peer.frame(
                    lambda frame: frame["type"] == "conversation.item.retrieve", after=start
                )
                start = peer.frames.index(frame, start) + 1
                assert frame["item_id"] == item["id"]
                assert peer.socket is not None
                await peer.socket.send_json({"type": "conversation.item.retrieved", "item": item})
            deleted = await peer.frame(
                lambda frame: frame["type"] == "conversation.item.delete", after=start
            )
            assert deleted["item_id"] == old["id"]
            start = peer.frames.index(deleted, start) + 1
            if cancel:
                updating.cancel()
                await asyncio.sleep(0)
            assert not updating.done()
            assert peer.socket is not None
            await peer.socket.send_json({"type": "conversation.item.deleted", "item_id": old["id"]})
            created = await peer.frame(
                lambda frame: frame["type"] == "conversation.item.create", after=start
            )
            start = peer.frames.index(created, start) + 1
            assert created["item"] == replacement
            assert created["previous_item_id"] == first["id"]
            assert not updating.done()
            await peer.socket.send_json({"type": "conversation.item.created", "item": replacement})
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await updating
            else:
                await updating
            retrieving = asyncio.create_task(voice.retrieve_conversation_history())
            try:
                for item in (first, replacement):
                    request = await peer.frame(
                        lambda frame: frame["type"] == "conversation.item.retrieve", after=start
                    )
                    start = peer.frames.index(request, start) + 1
                    assert request["item_id"] == item["id"]
                    await peer.socket.send_json(
                        {"type": "conversation.item.retrieved", "item": item}
                    )
                assert await retrieving == [first, replacement]
                if not cancel:
                    rejecting = asyncio.create_task(
                        voice.update_history(
                            [
                                first,
                                replacement,
                                {
                                    "id": "fabricated-tool-row",
                                    "type": "function_call",
                                    "name": "background_agent",
                                    "call_id": "fabricated-call",
                                    "arguments": "{}",
                                },
                            ]
                        )
                    )
                    try:
                        for item in (first, replacement):
                            request = await peer.frame(
                                lambda frame: frame["type"] == "conversation.item.retrieve",
                                after=start,
                            )
                            start = peer.frames.index(request, start) + 1
                            assert request["item_id"] == item["id"]
                            await peer.socket.send_json(
                                {"type": "conversation.item.retrieved", "item": item}
                            )
                        with pytest.raises(RuntimeError):
                            await rejecting
                        assert not any(
                            frame["type"]
                            in ("conversation.item.create", "conversation.item.delete")
                            for frame in peer.frames[start:]
                        )
                    finally:
                        if not rejecting.done():
                            await voice.aclose()
                        await asyncio.gather(rejecting, return_exceptions=True)
            finally:
                if not retrieving.done():
                    await voice.aclose()
                await asyncio.gather(retrieving, return_exceptions=True)
        finally:
            if not updating.done():
                await voice.aclose()
            await asyncio.gather(updating, return_exceptions=True)


async def test_native_provider_retrieval_reports_error_and_rejects_closed_owner(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    item = {
        "id": "retrieval-error-item",
        "type": "message",
        "role": "user",
        "status": "completed",
        "content": [{"type": "input_text", "text": "Synthetic message"}],
    }
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-history-error-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Handle provider retrieval errors.", provider=provider
        ).session() as session,
    ):
        async with (
            session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
            aclosing(voice.events()) as events,
        ):
            await observe_item(peer, events, item)
            start = len(peer.frames)
            retrieving = asyncio.create_task(voice.retrieve_conversation_item(item["id"]))
            try:
                await peer.frame(
                    lambda frame: frame["type"] == "conversation.item.retrieve", after=start
                )
                assert peer.socket is not None
                await peer.socket.send_json(
                    {
                        "type": "error",
                        "error": {
                            "type": "invalid_request_error",
                            "code": "synthetic_retrieval_rejected",
                            "message": "Synthetic provider retrieval rejection",
                        },
                    }
                )
                with pytest.raises(RuntimeError):
                    await retrieving
            finally:
                if not retrieving.done():
                    await voice.aclose()
                await asyncio.gather(retrieving, return_exceptions=True)
        async with session.realtime(config=RealtimeConfig(model="fixture-realtime")):
            start = len(peer.frames)
            with pytest.raises(RuntimeError):
                await voice.retrieve_conversation_item(item["id"])
            with pytest.raises(RuntimeError):
                await voice.update_history([])
            assert len(peer.frames) == start


@pytest.mark.parametrize(
    "vad",
    [
        {"type": "server_vad", "threshold": 0.5},
        {"type": "server_vad", "prefix_padding_ms": 100},
        {"type": "server_vad", "silence_duration_ms": 500},
    ],
)
async def test_native_sip_projection_preserves_explicit_vad_fields(
    realtime_peer: RealtimePeer, vad: Any
) -> None:
    peer = realtime_peer
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-sip-valid-vad-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Validate SIP configuration.", provider=provider).session() as session,
    ):
        projected = await session.build_realtime_sip_config(
            config=RealtimeConfig(model="fixture-realtime", settings={"turn_detection": vad})
        )
        actual = projected["audio"]["input"]["turn_detection"]
        assert all(actual[key] == value for key, value in vad.items())
        assert peer.connections == 0
        assert peer.frames == []


async def test_native_correcting_user_message_preserves_following_assistant_audio(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    user = {
        "id": "corrected-user",
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "Original question"}],
    }
    corrected = {**user, "content": [{"type": "input_text", "text": "Corrected question"}]}
    audio = {
        "id": "unchanged-assistant-audio",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "audio", "audio": "AQACAA==", "transcript": "hello"}],
    }
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-preserved-audio-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Preserve the accepted spoken answer.", provider=provider
        ).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
        aclosing(voice.events()) as events,
    ):
        await observe_item(peer, events, {**user, "status": "completed"})
        await observe_item(
            peer, events, {**audio, "status": "completed"}, previous_item_id=user["id"]
        )
        edit_start = len(peer.frames)
        cursor = edit_start
        updating = asyncio.create_task(voice.update_history([corrected, audio]))
        try:
            for item in (user, audio):
                request = await peer.frame(
                    lambda frame: frame["type"] == "conversation.item.retrieve", after=cursor
                )
                cursor = peer.frames.index(request, cursor) + 1
                assert request["item_id"] == item["id"]
                assert peer.socket is not None
                await peer.socket.send_json({"type": "conversation.item.retrieved", "item": item})
            deletion = await peer.frame(
                lambda frame: frame["type"] == "conversation.item.delete", after=cursor
            )
            cursor = peer.frames.index(deletion, cursor) + 1
            assert deletion["item_id"] == user["id"]
            assert peer.socket is not None
            await peer.socket.send_json(
                {"type": "conversation.item.deleted", "item_id": user["id"]}
            )
            creation = await peer.frame(
                lambda frame: frame["type"] == "conversation.item.create", after=cursor
            )
            cursor = peer.frames.index(creation, cursor) + 1
            assert creation["item"] == corrected
            assert creation["previous_item_id"] == "root"
            await peer.socket.send_json({"type": "conversation.item.created", "item": corrected})
            await updating
            mutations = [
                frame
                for frame in peer.frames[edit_start:]
                if frame["type"] in ("conversation.item.delete", "conversation.item.create")
            ]
            assert mutations == [deletion, creation]
            retrieving = asyncio.create_task(voice.retrieve_conversation_history())
            try:
                for item in (corrected, audio):
                    request = await peer.frame(
                        lambda frame: frame["type"] == "conversation.item.retrieve", after=cursor
                    )
                    cursor = peer.frames.index(request, cursor) + 1
                    assert request["item_id"] == item["id"]
                    await peer.socket.send_json(
                        {"type": "conversation.item.retrieved", "item": item}
                    )
                assert await retrieving == [corrected, audio]
            finally:
                if not retrieving.done():
                    await voice.aclose()
                await asyncio.gather(retrieving, return_exceptions=True)
        finally:
            if not updating.done():
                await voice.aclose()
            await asyncio.gather(updating, return_exceptions=True)


@pytest.mark.parametrize(
    ("role", "content"),
    [
        ("system", [{"type": "input_image", "image_url": "data:image/png;base64,AQACAA=="}]),
        ("assistant", [{"type": "input_audio", "audio": "AQACAA=="}]),
        pytest.param(
            "user",
            [{"type": "input_image", "image_url": "http://example.invalid/synthetic.png"}],
            id="user-http-image",
        ),
        pytest.param(
            "user",
            [{"type": "input_image", "image_url": "data:image/png;base64AQACAA=="}],
            id="user-malformed-image-data-uri",
        ),
        pytest.param(
            "user",
            [{"type": "input_image", "image_url": "data:image/png;base64,%%%"}],
            id="user-invalid-image-base64",
        ),
        pytest.param(
            "user",
            [{"type": "input_image", "image_url": "data:text/plain;base64,AQACAA=="}],
            id="user-unsupported-image-mime",
        ),
        pytest.param(
            "user",
            [{"type": "input_audio", "audio": "not-valid-base64!"}],
            id="user-invalid-audio-base64",
        ),
    ],
)
async def test_native_history_rejects_invalid_role_content_before_mutation(
    realtime_peer: RealtimePeer, role: str, content: list[dict[str, Any]]
) -> None:
    peer = realtime_peer
    original = {
        "id": "role-content-original",
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "Keep the original message"}],
    }
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-role-content-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Validate before editing.", provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
        aclosing(voice.events()) as events,
    ):
        await observe_item(peer, events, {**original, "status": "completed"})
        cursor = len(peer.frames)
        updating = asyncio.create_task(
            voice.update_history([{**original, "role": role, "content": content}])
        )
        try:
            request = await peer.frame(
                lambda frame: frame["type"] == "conversation.item.retrieve", after=cursor
            )
            assert request["item_id"] == original["id"]
            assert peer.socket is not None
            await peer.socket.send_json({"type": "conversation.item.retrieved", "item": original})
            with pytest.raises(RuntimeError):
                await updating
            assert not any(
                frame["type"] in ("conversation.item.delete", "conversation.item.create")
                for frame in peer.frames[cursor:]
            )
            assert not peer.disconnected.is_set()
        finally:
            if not updating.done():
                await voice.aclose()
            await asyncio.gather(updating, return_exceptions=True)


async def test_native_reordering_text_before_audio_preserves_unchanged_items(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    first = {
        "id": "reorder-text-a",
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "First question"}],
    }
    audio = {
        "id": "reorder-assistant-audio",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "audio", "audio": "AQACAA==", "transcript": "hello"}],
    }
    last = {
        "id": "reorder-text-b",
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "Second question"}],
    }
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-text-reorder-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Keep unchanged provider audio.", provider=provider
        ).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
        aclosing(voice.events()) as events,
    ):
        predecessor = None
        for item in (first, audio, last):
            await observe_item(
                peer, events, {**item, "status": "completed"}, previous_item_id=predecessor
            )
            predecessor = item["id"]
        edit_start = len(peer.frames)
        cursor = edit_start
        updating = asyncio.create_task(voice.update_history([last, first, audio]))
        try:
            for item in (first, audio, last):
                request = await peer.frame(
                    lambda frame: frame["type"] == "conversation.item.retrieve", after=cursor
                )
                cursor = peer.frames.index(request, cursor) + 1
                assert request["item_id"] == item["id"]
                assert peer.socket is not None
                await peer.socket.send_json({"type": "conversation.item.retrieved", "item": item})
            deletion = await peer.frame(
                lambda frame: frame["type"] == "conversation.item.delete", after=cursor
            )
            cursor = peer.frames.index(deletion, cursor) + 1
            assert deletion["item_id"] == last["id"]
            assert peer.socket is not None
            await peer.socket.send_json(
                {"type": "conversation.item.deleted", "item_id": last["id"]}
            )
            creation = await peer.frame(
                lambda frame: frame["type"] == "conversation.item.create", after=cursor
            )
            cursor = peer.frames.index(creation, cursor) + 1
            assert creation["item"] == last
            assert creation["previous_item_id"] == "root"
            await peer.socket.send_json({"type": "conversation.item.created", "item": last})
            await updating
            assert [
                frame
                for frame in peer.frames[edit_start:]
                if frame["type"] in ("conversation.item.delete", "conversation.item.create")
            ] == [deletion, creation]
            retrieving = asyncio.create_task(voice.retrieve_conversation_history())
            try:
                for item in (last, first, audio):
                    request = await peer.frame(
                        lambda frame: frame["type"] == "conversation.item.retrieve", after=cursor
                    )
                    cursor = peer.frames.index(request, cursor) + 1
                    assert request["item_id"] == item["id"]
                    await peer.socket.send_json(
                        {"type": "conversation.item.retrieved", "item": item}
                    )
                assert await retrieving == [last, first, audio]
            finally:
                if not retrieving.done():
                    await voice.aclose()
                await asyncio.gather(retrieving, return_exceptions=True)
        finally:
            if not updating.done():
                await voice.aclose()
            await asyncio.gather(updating, return_exceptions=True)


async def test_native_history_response_race_fails_edit_and_keeps_call_open(
    realtime_peer: RealtimePeer,
) -> None:
    from agents.realtime import RealtimeResponse

    peer = realtime_peer
    original = {
        "id": "race-original",
        "type": "message",
        "role": "user",
        "status": "completed",
        "content": [{"type": "input_text", "text": "Original synthetic context"}],
    }
    corrected = {**original, "content": [{"type": "input_text", "text": "Corrected context"}]}
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-response-race-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Keep the accepted call open.", provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
        aclosing(voice.events()) as events,
    ):
        await observe_item(peer, events, original)
        cursor = len(peer.frames)
        updating = asyncio.create_task(voice.update_history([corrected]))
        try:
            request = await peer.frame(
                lambda frame: frame["type"] == "conversation.item.retrieve", after=cursor
            )
            cursor = peer.frames.index(request, cursor) + 1
            assert peer.socket is not None
            await peer.socket.send_json(
                {"type": "response.created", "response": {"id": "racing-provider-response"}}
            )
            with pytest.raises(RuntimeError):
                await updating
            assert not peer.disconnected.is_set()
            assert not peer.socket.closed
            # Drain the already-written retrieval before accepting another history command.
            await peer.socket.send_json({"type": "conversation.item.retrieved", "item": original})
            await peer.socket.send_json(
                {"type": "response.done", "response": {"id": "racing-provider-response"}}
            )
            async for event in events:
                if (
                    isinstance(event, RealtimeResponse)
                    and event.response_id == "racing-provider-response"
                    and event.status == "done"
                ):
                    break
            assert not any(
                frame["type"] in ("conversation.item.delete", "conversation.item.create")
                for frame in peer.frames[cursor:]
            )
            retrieving = asyncio.create_task(voice.retrieve_conversation_item(original["id"]))
            try:
                request = await peer.frame(
                    lambda frame: frame["type"] == "conversation.item.retrieve", after=cursor
                )
                assert request["item_id"] == original["id"]
                await peer.socket.send_json(
                    {"type": "conversation.item.retrieved", "item": original}
                )
                assert await retrieving == original
                assert peer.connections == 1
                assert not peer.disconnected.is_set()
            finally:
                if not retrieving.done():
                    await voice.aclose()
                await asyncio.gather(retrieving, return_exceptions=True)
        finally:
            if not updating.done():
                await voice.aclose()
            await asyncio.gather(updating, return_exceptions=True)


async def test_native_cumulative_history_media_budget_preserves_live_connection(
    realtime_peer: RealtimePeer,
) -> None:
    peer = realtime_peer
    media = "AQAC" * 275_000
    rows = [
        {
            "id": f"budget-audio-{index}",
            "type": "message",
            "role": "user",
            "status": "completed",
            "content": [{"type": "input_audio", "audio": media, "transcript": "Synthetic audio"}],
        }
        for index in range(2)
    ]
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-cumulative-history-budget-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Keep provider retrieval bounded.", provider=provider
        ).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
        aclosing(voice.events()) as events,
    ):
        predecessor = None
        for row in rows:
            await observe_item(
                peer,
                events,
                {**row, "content": [{"type": "input_audio", "transcript": "Synthetic audio"}]},
                previous_item_id=predecessor,
            )
            predecessor = row["id"]
        cursor = len(peer.frames)
        retrieving = asyncio.create_task(voice.retrieve_conversation_history())
        try:
            for row in rows:
                request = await peer.frame(
                    lambda frame: frame["type"] == "conversation.item.retrieve", after=cursor
                )
                cursor = peer.frames.index(request, cursor) + 1
                assert request["item_id"] == row["id"]
                assert peer.socket is not None
                await peer.socket.send_json({"type": "conversation.item.retrieved", "item": row})
            with pytest.raises(RuntimeError):
                await retrieving
            assert not peer.disconnected.is_set()
            small = {
                "id": rows[0]["id"],
                "type": "message",
                "role": "user",
                "content": [{"type": "input_audio", "audio": "AQACAA=="}],
            }
            followup = asyncio.create_task(voice.retrieve_conversation_item(small["id"]))
            try:
                request = await peer.frame(
                    lambda frame: frame["type"] == "conversation.item.retrieve", after=cursor
                )
                assert request["item_id"] == small["id"]
                await peer.socket.send_json({"type": "conversation.item.retrieved", "item": small})
                assert await followup == small
                assert not peer.disconnected.is_set()
                assert peer.connections == 1
            finally:
                if not followup.done():
                    await voice.aclose()
                await asyncio.gather(followup, return_exceptions=True)
        finally:
            if not retrieving.done():
                await voice.aclose()
            await asyncio.gather(retrieving, return_exceptions=True)


@pytest.mark.parametrize("ack_predecessor", [None, "root"])
async def test_native_response_interrupts_create_and_drains_late_ack_without_next_edit(
    realtime_peer: RealtimePeer,
    ack_predecessor: str | None,
) -> None:
    from agents.realtime import RealtimeHistory, RealtimeResponse

    peer = realtime_peer
    old = {
        "id": "interrupted-old-user",
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "Original message"}],
    }
    first = {
        "id": "interrupted-first-new",
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "First replacement"}],
    }
    second = {
        "id": "interrupted-second-new",
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "Second replacement"}],
    }
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-mutation-interruption-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Preserve acknowledged edits.", provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
        aclosing(voice.events()) as events,
    ):
        await observe_item(peer, events, {**old, "status": "completed"})
        start = len(peer.frames)
        cursor = start
        updating = asyncio.create_task(voice.update_history([first, second]))
        try:
            request = await peer.frame(
                lambda frame: frame["type"] == "conversation.item.retrieve", after=cursor
            )
            cursor = peer.frames.index(request, cursor) + 1
            assert request["item_id"] == old["id"]
            assert peer.socket is not None
            await peer.socket.send_json({"type": "conversation.item.retrieved", "item": old})
            deletion = await peer.frame(
                lambda frame: frame["type"] == "conversation.item.delete", after=cursor
            )
            cursor = peer.frames.index(deletion, cursor) + 1
            assert deletion["item_id"] == old["id"]
            await peer.socket.send_json({"type": "conversation.item.deleted", "item_id": old["id"]})
            creation = await peer.frame(
                lambda frame: frame["type"] == "conversation.item.create", after=cursor
            )
            cursor = peer.frames.index(creation, cursor) + 1
            assert creation["item"] == first
            assert creation["previous_item_id"] == "root"
            async for event in events:
                if (
                    isinstance(event, RealtimeHistory)
                    and event.completed
                    and event.item.kind == "provider_item_deleted"
                    and event.item.provider_item_id == old["id"]
                ):
                    break
            assert any(
                item.kind == "provider_item_deleted" and item.provider_item_id == old["id"]
                for item in voice.history
            )
            await peer.socket.send_json(
                {"type": "response.created", "response": {"id": "interrupt-create-response"}}
            )
            with pytest.raises(RuntimeError):
                await updating
            assert not peer.disconnected.is_set()
            assert not peer.socket.closed
            # Added acknowledges the written create; Done completes its observed row.
            await observe_item(
                peer, events, {**first, "status": "completed"}, include_previous_item_id=False
            )
            await peer.socket.send_json(
                {"type": "response.done", "response": {"id": "interrupt-create-response"}}
            )
            async for event in events:
                if (
                    isinstance(event, RealtimeResponse)
                    and event.response_id == "interrupt-create-response"
                    and event.status == "done"
                ):
                    break
            assert [
                frame
                for frame in peer.frames[start:]
                if frame["type"] == "conversation.item.create"
            ] == [creation]
            retrieving = asyncio.create_task(voice.retrieve_conversation_history())
            try:
                request = await peer.frame(
                    lambda frame: frame["type"] == "conversation.item.retrieve", after=cursor
                )
                assert request["item_id"] == first["id"]
                await peer.socket.send_json({"type": "conversation.item.retrieved", "item": first})
                assert await retrieving == [first]
                assert any(
                    item.kind == "provider_item_deleted" and item.provider_item_id == old["id"]
                    for item in voice.history
                )
                assert not any(
                    frame["type"] == "conversation.item.create"
                    and frame["item"]["id"] == second["id"]
                    for frame in peer.frames[start:]
                )
                assert peer.connections == 1
                assert not peer.disconnected.is_set()
            finally:
                if not retrieving.done():
                    await voice.aclose()
                await asyncio.gather(retrieving, return_exceptions=True)
        finally:
            if not updating.done():
                await voice.aclose()
            await asyncio.gather(updating, return_exceptions=True)


@pytest.mark.parametrize("prefix", ["empty_observed", "known_predecessor", "unknown_predecessor"])
async def test_native_sip_history_preserves_unobserved_provider_prefix(
    call_peer: CallPeer, prefix: str
) -> None:
    peer = call_peer
    hidden_id = "sip-provider-hidden-prefix"
    original = {
        "id": "sip-observed-message",
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "Original observed message"}],
    }
    corrected = {**original, "content": [{"type": "input_text", "text": "Corrected message"}]}
    appended = {
        "id": "sip-new-message",
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "New application message"}],
    }
    audio = {
        "id": "sip-observed-audio",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "audio", "audio": "AQACAA==", "transcript": "hello"}],
    }
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-native-call-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Preserve the incoming call's existing context.", provider=provider
        ).session() as session,
    ):
        # The provider owns an existing prefix; no events enumerate its hidden items.
        peer.sessions["rtc_history_prefix"] = await session.build_realtime_sip_config()
        async with (
            session.realtime(
                config=RealtimeConfig(transport=RealtimeSIP("rtc_history_prefix"))
            ) as voice,
            aclosing(voice.events()) as events,
        ):
            if prefix == "known_predecessor":
                await observe_item(
                    peer,
                    events,
                    {**original, "status": "completed"},
                    previous_item_id=hidden_id,
                )
            elif prefix == "unknown_predecessor":
                assert peer.socket is not None
                for kind in ("conversation.item.added", "conversation.item.done"):
                    await peer.socket.send_json(
                        {"type": kind, "item": {**audio, "status": "completed"}}
                    )
                async for event in events:
                    if (
                        isinstance(event, RealtimeConversationItemObserved)
                        and event.item_id == audio["id"]
                        and event.status == "done"
                    ):
                        break
            cursor = len(peer.frames)
            start = cursor
            desired = (
                [appended]
                if prefix == "empty_observed"
                else [corrected]
                if prefix == "known_predecessor"
                else [appended, audio]
            )
            updating = asyncio.create_task(voice.update_history(desired))
            try:
                if prefix == "unknown_predecessor":
                    with pytest.raises(RuntimeError, match="ordered"):
                        await updating
                    assert len(peer.frames) == start
                    assert not peer.disconnected.is_set()
                    return
                if prefix != "empty_observed":
                    observed = original if prefix == "known_predecessor" else audio
                    request = await peer.frame(
                        lambda frame: frame["type"] == "conversation.item.retrieve", after=cursor
                    )
                    cursor = peer.frames.index(request, cursor) + 1
                    assert request["item_id"] == observed["id"]
                    assert peer.socket is not None
                    await peer.socket.send_json(
                        {"type": "conversation.item.retrieved", "item": observed}
                    )
                if prefix == "known_predecessor":
                    deletion = await peer.frame(
                        lambda frame: frame["type"] == "conversation.item.delete", after=cursor
                    )
                    cursor = peer.frames.index(deletion, cursor) + 1
                    assert deletion["item_id"] == original["id"]
                    assert peer.socket is not None
                    await peer.socket.send_json(
                        {"type": "conversation.item.deleted", "item_id": original["id"]}
                    )
                creation = await peer.frame(
                    lambda frame: frame["type"] == "conversation.item.create", after=cursor
                )
                created = appended if prefix == "empty_observed" else corrected
                assert creation["item"] == created
                if prefix == "empty_observed":
                    assert "previous_item_id" not in creation
                    assert not any(
                        frame["type"] == "conversation.item.retrieve"
                        for frame in peer.frames[start:]
                    )
                else:
                    assert creation["previous_item_id"] == hidden_id
                await observe_item(
                    peer,
                    events,
                    {**created, "status": "completed"},
                    previous_item_id=hidden_id,
                    include_previous_item_id=prefix != "empty_observed",
                )
                await updating
                assert not any(
                    frame["type"] == "conversation.item.delete" and frame["item_id"] == hidden_id
                    for frame in peer.frames[start:]
                )
                assert not peer.disconnected.is_set()
            finally:
                if not updating.done():
                    await voice.aclose()
                await asyncio.gather(updating, return_exceptions=True)


async def test_native_vad_item_interrupts_history_edit_before_response_without_closing_call(
    realtime_peer: RealtimePeer,
) -> None:
    from agents.realtime import RealtimeHistory, RealtimeResponse

    peer = realtime_peer
    original = {
        "id": "vad-race-original",
        "type": "message",
        "role": "user",
        "status": "completed",
        "content": [{"type": "input_text", "text": "Original synthetic context"}],
    }
    corrected = {**original, "content": [{"type": "input_text", "text": "Corrected context"}]}
    transcript = "A new synthetic caller utterance arrived during the edit."
    incoming = {
        "id": "vad-race-new-user",
        "type": "message",
        "role": "user",
        "status": "completed",
        "content": [{"type": "input_audio", "transcript": transcript}],
    }
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-vad-item-race-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Keep ordinary caller input and its audit.", provider=provider
        ).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
        aclosing(voice.events()) as events,
    ):
        await observe_item(peer, events, original)
        start = len(peer.frames)
        cursor = start
        updating = asyncio.create_task(voice.update_history([corrected]))
        try:
            request = await peer.frame(
                lambda frame: frame["type"] == "conversation.item.retrieve", after=cursor
            )
            cursor = peer.frames.index(request, cursor) + 1
            assert request["item_id"] == original["id"]
            assert peer.socket is not None
            # Provider VAD publishes the user item before its response.created event.
            await peer.socket.send_json(
                {
                    "type": "conversation.item.added",
                    "previous_item_id": original["id"],
                    "item": incoming,
                }
            )
            with pytest.raises(RuntimeError):
                await updating
            assert not peer.disconnected.is_set()
            assert not peer.socket.closed
            # Drain the written snapshot request while retaining the ordinary new item.
            await peer.socket.send_json({"type": "conversation.item.retrieved", "item": original})
            await peer.socket.send_json(
                {
                    "type": "conversation.item.done",
                    "previous_item_id": original["id"],
                    "item": incoming,
                }
            )
            await peer.socket.send_json(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": incoming["id"],
                    "transcript": transcript,
                }
            )
            await peer.socket.send_json(
                {"type": "response.created", "response": {"id": "vad-race-response"}}
            )
            await peer.socket.send_json(
                {"type": "response.done", "response": {"id": "vad-race-response"}}
            )
            observed, audited, response_done = False, False, False
            async for event in events:
                if (
                    isinstance(event, RealtimeConversationItemObserved)
                    and event.item_id == incoming["id"]
                ):
                    observed = True
                if (
                    isinstance(event, RealtimeHistory)
                    and event.completed
                    and event.item.kind == "transcript_segment"
                    and event.item.role == "user"
                    and event.item.text == transcript
                ):
                    audited = True
                if (
                    isinstance(event, RealtimeResponse)
                    and event.response_id == "vad-race-response"
                    and event.status == "done"
                ):
                    response_done = True
                if observed and audited and response_done:
                    break
            assert observed and audited and response_done
            assert any(item.role == "user" and item.text == transcript for item in voice.history)
            assert not any(
                frame["type"] in ("conversation.item.delete", "conversation.item.create")
                for frame in peer.frames[start:]
            )
            retrieving = asyncio.create_task(voice.retrieve_conversation_history())
            try:
                for item in (original, incoming):
                    request = await peer.frame(
                        lambda frame: frame["type"] == "conversation.item.retrieve", after=cursor
                    )
                    cursor = peer.frames.index(request, cursor) + 1
                    assert request["item_id"] == item["id"]
                    await peer.socket.send_json(
                        {"type": "conversation.item.retrieved", "item": item}
                    )
                assert await retrieving == [original, incoming]
                assert peer.connections == 1
                assert not peer.disconnected.is_set()
            finally:
                if not retrieving.done():
                    await voice.aclose()
                await asyncio.gather(retrieving, return_exceptions=True)
        finally:
            if not updating.done():
                await voice.aclose()
            await asyncio.gather(updating, return_exceptions=True)


async def test_native_history_read_timeout_drains_late_receipt_before_same_id_reuse(
    realtime_peer: RealtimePeer,
) -> None:
    from agents.realtime import RealtimeResponse

    peer = realtime_peer
    original = {
        "id": "timeout-item",
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "Original"}],
    }
    fresh = {**original, "content": [{"type": "input_text", "text": "Fresh retrieval"}]}
    async with (
        timeout(30),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-history-timeout-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Keep this voice connection active.", provider=provider
        ).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
        aclosing(voice.events()) as events,
    ):
        await observe_item(peer, events, original)
        cursor = len(peer.frames)
        retrieving = asyncio.create_task(voice.retrieve_conversation_item(original["id"]))
        try:
            request = await peer.frame(
                lambda frame: frame["type"] == "conversation.item.retrieve", after=cursor
            )
            cursor = peer.frames.index(request, cursor) + 1
            with pytest.raises(RuntimeError, match="timed out"):
                await retrieving
            assert not peer.disconnected.is_set()
            with pytest.raises(RuntimeError, match="pending"):
                await voice.retrieve_conversation_item(original["id"])
            assert len(peer.frames) == cursor
            assert peer.socket is not None
            await peer.socket.send_json(
                {"type": "response.created", "response": {"id": "timeout-voice-live"}}
            )
            await peer.socket.send_json(
                {"type": "response.done", "response": {"id": "timeout-voice-live"}}
            )
            async for event in events:
                if (
                    isinstance(event, RealtimeResponse)
                    and event.response_id == "timeout-voice-live"
                    and event.status == "done"
                ):
                    break
            await peer.socket.send_json({"type": "conversation.item.retrieved", "item": original})
            await peer.socket.send_json(
                {"type": "response.created", "response": {"id": "late-read-drained"}}
            )
            await peer.socket.send_json(
                {"type": "response.done", "response": {"id": "late-read-drained"}}
            )
            async for event in events:
                if (
                    isinstance(event, RealtimeResponse)
                    and event.response_id == "late-read-drained"
                    and event.status == "done"
                ):
                    break
            again = asyncio.create_task(voice.retrieve_conversation_item(original["id"]))
            try:
                request = await peer.frame(
                    lambda frame: frame["type"] == "conversation.item.retrieve", after=cursor
                )
                assert request["item_id"] == original["id"]
                assert not again.done()
                await peer.socket.send_json({"type": "conversation.item.retrieved", "item": fresh})
                assert await again == fresh
                assert peer.connections == 1
                assert not peer.disconnected.is_set()
            finally:
                if not again.done():
                    await voice.aclose()
                await asyncio.gather(again, return_exceptions=True)
        finally:
            if not retrieving.done():
                await voice.aclose()
            await asyncio.gather(retrieving, return_exceptions=True)


@pytest.mark.parametrize("unknown_position", ["named", "missing", "done_only"])
@pytest.mark.parametrize("first_predecessor", [None, "root"])
async def test_native_history_uses_provider_predecessors_and_rejects_unresolved_order(
    realtime_peer: RealtimePeer,
    unknown_position: str,
    first_predecessor: str | None,
) -> None:
    peer = realtime_peer

    def message(name: str) -> dict[str, Any]:
        return {
            "id": name,
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": name}],
        }

    first, second, inserted, unknown = (
        message(name) for name in ("order-a", "order-b", "order-c", "order-unknown")
    )
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-provider-order-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Observe provider conversation order.", provider=provider
        ).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
        aclosing(voice.events()) as events,
    ):
        await observe_item(peer, events, first, previous_item_id=first_predecessor)
        await observe_item(peer, events, second, previous_item_id=first["id"])
        await observe_item(peer, events, inserted, previous_item_id=first["id"])
        cursor = len(peer.frames)
        retrieving = asyncio.create_task(voice.retrieve_conversation_history())
        try:
            for item in (first, inserted, second):
                request = await peer.frame(
                    lambda frame: frame["type"] == "conversation.item.retrieve", after=cursor
                )
                cursor = peer.frames.index(request, cursor) + 1
                assert request["item_id"] == item["id"]
                assert peer.socket is not None
                await peer.socket.send_json({"type": "conversation.item.retrieved", "item": item})
            assert await retrieving == [first, inserted, second]
        finally:
            if not retrieving.done():
                await voice.aclose()
            await asyncio.gather(retrieving, return_exceptions=True)
        if unknown_position == "done_only":
            from agents.realtime import RealtimeResponse

            assert peer.socket is not None
            await peer.socket.send_json({"type": "conversation.item.done", "item": unknown})
            # An unseen Done has no thin observation; use a later provider response as a barrier.
            await peer.socket.send_json(
                {"type": "response.created", "response": {"id": "done-only-barrier"}}
            )
            await peer.socket.send_json(
                {"type": "response.done", "response": {"id": "done-only-barrier"}}
            )
            async for event in events:
                if (
                    isinstance(event, RealtimeResponse)
                    and event.response_id == "done-only-barrier"
                    and event.status == "done"
                ):
                    break
        else:
            await observe_item(
                peer,
                events,
                unknown,
                previous_item_id="unresolved-provider-item",
                include_previous_item_id=unknown_position != "missing",
            )
        with pytest.raises(RuntimeError, match="ordered"):
            await voice.retrieve_conversation_history()
        with pytest.raises(RuntimeError, match="ordered"):
            await voice.update_history([first, inserted, second, unknown])
        assert len(peer.frames) == cursor
        retrieving_one = asyncio.create_task(voice.retrieve_conversation_item(unknown["id"]))
        try:
            request = await peer.frame(
                lambda frame: frame["type"] == "conversation.item.retrieve", after=cursor
            )
            assert request["item_id"] == unknown["id"]
            assert peer.socket is not None
            await peer.socket.send_json({"type": "conversation.item.retrieved", "item": unknown})
            assert await retrieving_one == unknown
            assert peer.connections == 1
            assert not peer.disconnected.is_set()
        finally:
            if not retrieving_one.done():
                await voice.aclose()
            await asyncio.gather(retrieving_one, return_exceptions=True)


async def test_native_deleted_item_retrieval_rejects_locally_and_preserves_audit(
    realtime_peer: RealtimePeer,
) -> None:
    from agents.realtime import RealtimeHistory

    peer = realtime_peer
    item = {
        "id": "deleted-retrieval-user",
        "type": "message",
        "role": "user",
        "status": "completed",
        "content": [{"type": "input_text", "text": "Retain the deletion audit"}],
    }
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-deleted-retrieval-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Keep deletion audit history.", provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
        aclosing(voice.events()) as events,
    ):
        await observe_item(peer, events, item)
        cursor = len(peer.frames)
        deleting = asyncio.create_task(voice.delete_conversation_item(item["id"]))
        try:
            deletion = await peer.frame(
                lambda frame: frame["type"] == "conversation.item.delete", after=cursor
            )
            assert deletion["item_id"] == item["id"]
            assert peer.socket is not None
            await peer.socket.send_json(
                {"type": "conversation.item.deleted", "item_id": item["id"]}
            )
            await deleting
            async for event in events:
                if (
                    isinstance(event, RealtimeHistory)
                    and event.completed
                    and event.item.kind == "provider_item_deleted"
                    and event.item.provider_item_id == item["id"]
                ):
                    break
            audit = [
                entry
                for entry in voice.history
                if entry.kind == "provider_item_deleted" and entry.provider_item_id == item["id"]
            ]
            assert len(audit) == 1
            cursor = len(peer.frames)
            with pytest.raises(RuntimeError):
                await voice.retrieve_conversation_item(item["id"])
            assert await voice.retrieve_conversation_history() == []
            assert not any(
                frame["type"] == "conversation.item.retrieve" for frame in peer.frames[cursor:]
            )
            assert audit[0] in voice.history
            assert peer.connections == 1
            assert not peer.disconnected.is_set()
            assert not peer.socket.closed
        finally:
            if not deleting.done():
                await voice.aclose()
            await asyncio.gather(deleting, return_exceptions=True)


@pytest.mark.parametrize("ack_position", ["other_item", "self"])
async def test_native_contradictory_create_ack_interrupts_edit_and_preserves_actual_order(
    realtime_peer: RealtimePeer,
    ack_position: str,
) -> None:
    peer = realtime_peer
    old = {
        "id": "ack-old",
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "Keep existing context"}],
    }
    first = {**old, "id": "ack-first"}
    second = {**old, "id": "ack-never-create"}
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-contradictory-ack-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Keep observed provider positions accurate.", provider=provider
        ).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
        aclosing(voice.events()) as events,
    ):
        await observe_item(peer, events, old)
        cursor = len(peer.frames)
        updating = asyncio.create_task(voice.update_history([first, second, old]))
        try:
            request = await peer.frame(
                lambda frame: frame["type"] == "conversation.item.retrieve", after=cursor
            )
            cursor = peer.frames.index(request, cursor) + 1
            assert peer.socket is not None
            await peer.socket.send_json({"type": "conversation.item.retrieved", "item": old})
            creation = await peer.frame(
                lambda frame: frame["type"] == "conversation.item.create", after=cursor
            )
            cursor = peer.frames.index(creation, cursor) + 1
            assert creation["item"] == first
            assert creation["previous_item_id"] == "root"
            # The acknowledgement claims a different placement from the written request.
            await observe_item(
                peer,
                events,
                first,
                previous_item_id=old["id"] if ack_position == "other_item" else first["id"],
            )
            with pytest.raises(RuntimeError):
                await updating
            with pytest.raises(RuntimeError, match="ordered"):
                await voice.retrieve_conversation_history()
            assert not any(
                frame["type"] == "conversation.item.create" and frame["item"]["id"] == second["id"]
                for frame in peer.frames[cursor:]
            )
            retrieving = asyncio.create_task(voice.retrieve_conversation_item(first["id"]))
            try:
                request = await peer.frame(
                    lambda frame: frame["type"] == "conversation.item.retrieve", after=cursor
                )
                assert request["item_id"] == first["id"]
                await peer.socket.send_json({"type": "conversation.item.retrieved", "item": first})
                assert await retrieving == first
                assert peer.connections == 1
                assert not peer.disconnected.is_set()
            finally:
                if not retrieving.done():
                    await voice.aclose()
                await asyncio.gather(retrieving, return_exceptions=True)
        finally:
            if not updating.done():
                await voice.aclose()
            await asyncio.gather(updating, return_exceptions=True)


@pytest.mark.parametrize("first_observed", [True, False])
async def test_native_self_predecessor_blocks_ordered_history_but_keeps_direct_retrieval(
    realtime_peer: RealtimePeer, first_observed: bool
) -> None:
    peer = realtime_peer
    first = {
        "id": "before-self-predecessor",
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "Known first item"}],
    }
    invalid = {
        "id": "self-predecessor-item",
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "Provider item with invalid ordering"}],
    }
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-self-predecessor-key",
            supports_websockets=False,
        ) as provider,
        Agent(
            instructions="Keep ambiguous provider ordering local.", provider=provider
        ).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as voice,
        aclosing(voice.events()) as events,
    ):
        if not first_observed:
            await observe_item(peer, events, first)
        await observe_item(peer, events, invalid, previous_item_id=invalid["id"])
        cursor = len(peer.frames)
        with pytest.raises(RuntimeError, match="ordered"):
            await voice.retrieve_conversation_history()
        desired = [invalid] if first_observed else [first, invalid]
        with pytest.raises(RuntimeError, match="ordered"):
            await voice.update_history(desired)
        assert len(peer.frames) == cursor
        assert not peer.disconnected.is_set()
        retrieving = asyncio.create_task(voice.retrieve_conversation_item(invalid["id"]))
        try:
            request = await peer.frame(
                lambda frame: frame["type"] == "conversation.item.retrieve", after=cursor
            )
            assert request["item_id"] == invalid["id"]
            assert peer.socket is not None
            await peer.socket.send_json({"type": "conversation.item.retrieved", "item": invalid})
            assert await retrieving == invalid
            assert peer.connections == 1
            assert not peer.disconnected.is_set()
            assert not peer.socket.closed
        finally:
            if not retrieving.done():
                await voice.aclose()
            await asyncio.gather(retrieving, return_exceptions=True)
