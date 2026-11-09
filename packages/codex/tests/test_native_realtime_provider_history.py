"""Public provider configuration and history operations through the native engine."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from typing import Any

import pytest

from agents import Agent, NativeProvider, function_tool
from agents.realtime import RealtimeConfig, RealtimeConversationItemObserved
from tests._asyncio import timeout
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer


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


async def observe_item(peer: RealtimePeer, events: Any, item: dict[str, Any]) -> None:
    assert peer.socket is not None
    await peer.socket.send_json({"type": "conversation.item.added", "item": item})
    await peer.socket.send_json({"type": "conversation.item.done", "item": item})
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
        await observe_item(peer, events, {**old, "status": "completed"})
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
async def test_native_sip_projection_rejects_unsupported_explicit_vad_fields(
    realtime_peer: RealtimePeer, vad: Any
) -> None:
    peer = realtime_peer
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-sip-invalid-vad-key",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Validate SIP configuration.", provider=provider).session() as session,
    ):
        with pytest.raises((ValueError, RuntimeError)):
            await session.build_realtime_sip_config(
                config=RealtimeConfig(model="fixture-realtime", settings={"turn_detection": vad})
            )
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
        await observe_item(peer, events, {**audio, "status": "completed"})
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
