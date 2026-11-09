"""Prepared wire coverage for typed native Realtime user input."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import pytest

from agents import Agent, NativeProvider
from agents.realtime import RealtimeConfig
from tests._asyncio import timeout
from tests.test_native_realtime import realtime_peer as realtime_peer
from tests.test_native_realtime_manual_audio import SendHookAdapter


@pytest.mark.parametrize("custom", [False, True], ids=["native", "custom"])
@pytest.mark.parametrize("trigger", [False, True], ids=["deferred-response", "automatic-response"])
async def test_text_image_input_is_one_ordered_user_message(realtime_peer, custom, trigger):
    peer = realtime_peer
    adapter = (
        SendHookAdapter(
            peer.responses.url.replace("http://", "ws://") + "/realtime?model=fixture-realtime"
        )
        if custom
        else None
    )
    message = {
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": "Inspect this image."},
            {
                "type": "input_image",
                "image_url": "data:image/png;base64,aW1hZ2U=",
                "detail": "low",
            },
            {"type": "input_text", "text": "Keep that order."},
        ],
    }
    original = copy.deepcopy(message)
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-image-input",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Describe images.", provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime", adapter=adapter)) as owner,
    ):
        await peer.frame(lambda frame: frame["type"] == "session.update")
        start = len(peer.frames)
        await owner.send_message(message, trigger_response=trigger)
        sent = await peer.frame(
            lambda frame: frame["type"] == "conversation.item.create", after=start
        )
        assert sent["item"]["type"] == "message" and sent["item"]["role"] == "user"
        assert sent["item"]["content"] == original["content"]
        assert message == original
        if not trigger:
            await owner.send_message("Deferred input fence", trigger_response=False)
            fence = await peer.frame(
                lambda frame: (
                    frame.get("item", {}).get("content")
                    == [{"type": "input_text", "text": "Deferred input fence"}]
                ),
                after=start,
            )
            assert not any(
                frame["type"] == "response.create"
                for frame in peer.frames[start : peer.frames.index(fence) + 1]
            )
            await owner.request_response()
        await peer.frame(lambda frame: frame["type"] == "response.create", after=start)
        assert sum(
            frame["type"] == "conversation.item.create" for frame in peer.frames[start:]
        ) == (1 if trigger else 2)
        assert sum(frame["type"] == "response.create" for frame in peer.frames[start:]) == 1
        assert not any(
            frame["type"] == "input_audio_buffer.commit" for frame in peer.frames[start:]
        )


async def test_explicit_response_does_not_append_or_commit_input(realtime_peer):
    peer = realtime_peer
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-explicit-response",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Respond to accepted input.", provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as owner,
    ):
        await peer.frame(lambda frame: frame["type"] == "session.update")
        start = len(peer.frames)
        await owner.request_response()
        await peer.frame(lambda frame: frame["type"] == "response.create", after=start)
        assert [frame["type"] for frame in peer.frames[start:]] == ["response.create"]


async def test_invalid_message_and_response_flag_have_no_wire_effects(realtime_peer):
    peer = realtime_peer
    invalid: list[Any] = [
        "",
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "input_text", "text": "No"}],
        },
        {"type": "message", "role": "user", "content": []},
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_audio", "audio_url": "data:audio/fake"}],
        },
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_image", "file_id": "file-not-admitted"}],
        },
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_image", "image_url": "image", "detail": "original"}],
        },
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "No", "providerData": {"role": "system"}}],
        },
    ]
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-invalid-input",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as owner,
    ):
        await peer.frame(lambda frame: frame["type"] == "session.update")
        start = len(peer.frames)
        for message in invalid:
            with pytest.raises((TypeError, ValueError)):
                await owner.send_message(message)
        with pytest.raises(TypeError, match="trigger_response must be a bool"):
            await owner.send_message("No", trigger_response=1)
        await owner.send_text("Compatibility fence")
        item = await peer.frame(
            lambda frame: frame["type"] == "conversation.item.create", after=start
        )
        assert item["item"]["content"] == [{"type": "input_text", "text": "Compatibility fence"}]
        await peer.frame(
            lambda frame: frame["type"] == "response.create",
            after=peer.frames.index(item) + 1,
        )
        assert (
            sum(frame["type"] == "conversation.item.create" for frame in peer.frames[start:]) == 1
        )


async def test_message_is_captured_before_waiting_for_audio_commit(realtime_peer, monkeypatch):
    peer = realtime_peer
    adapter = SendHookAdapter(
        peer.responses.url.replace("http://", "ws://") + "/realtime?model=fixture-realtime"
    )
    entered, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
    message = {
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": "captured"},
            {
                "type": "input_image",
                "image_url": "data:image/png;base64,b3JpZ2luYWw=",
                "detail": "low",
            },
        ],
    }
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-captured-input",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime", adapter=adapter)) as owner,
    ):
        await peer.frame(lambda frame: frame["type"] == "session.update")
        original = adapter.forward
        assert original is not None

        async def gate(frame):
            if json.loads(frame)["type"] == "input_audio_buffer.commit":
                entered.set()
                await release.wait()
            await original(frame)

        monkeypatch.setattr(adapter, "send_hook", gate)
        start = len(peer.frames)
        audio = asyncio.create_task(owner.send_audio(b"\0\0" * 2400, commit=True))
        sending = None
        try:
            await entered.wait()

            async def send_captured():
                queued.set()
                await owner.send_message(message, trigger_response=False)

            sending = asyncio.create_task(send_captured())
            await queued.wait()
            # send_message has run synchronously to its first await: the held input lock.
            message["content"][0]["text"] = "mutated after queuing"
            message["content"][1]["image_url"] = "data:image/png;base64,bXV0YXRlZA=="
            message["content"][1]["detail"] = "high"
            release.set()
            await asyncio.gather(audio, sending)
            await owner.request_response()
            await peer.frame(lambda frame: frame["type"] == "response.create", after=start)
            frames = peer.frames[start:]
            assert [frame["type"] for frame in frames] == [
                "input_audio_buffer.append",
                "input_audio_buffer.commit",
                "conversation.item.create",
                "response.create",
            ]
            assert frames[2]["item"]["content"] == [
                {"type": "input_text", "text": "captured"},
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,b3JpZ2luYWw=",
                    "detail": "low",
                },
            ]
        finally:
            release.set()
            await asyncio.gather(
                *(task for task in (audio, sending) if task is not None),
                return_exceptions=True,
            )


async def test_provider_deletion_keeps_canonical_target_audit(realtime_peer):
    from contextlib import aclosing

    from agents.realtime import (
        RealtimeConversationItemDeleted,
        RealtimeConversationItemObserved,
        RealtimeHistory,
    )

    peer = realtime_peer
    provider_item_id = "provider-user-message"
    item = {
        "id": provider_item_id,
        "type": "message",
        "role": "user",
        "status": "completed",
        "content": [{"type": "input_text", "text": "Keep the audit"}],
    }
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url,
            api_key="synthetic-provider-delete",
            supports_websockets=False,
        ) as provider,
        Agent(instructions="Keep canonical history.", provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime")) as owner,
        aclosing(owner.events()) as events,
    ):
        await peer.frame(lambda frame: frame["type"] == "session.update")
        assert peer.socket is not None
        await peer.socket.send_json({"type": "conversation.item.added", "item": item})
        await peer.socket.send_json({"type": "conversation.item.done", "item": item})
        async for event in events:
            if (
                isinstance(event, RealtimeConversationItemObserved)
                and event.item_id == provider_item_id
                and event.status == "done"
            ):
                break
        prior_ids = {entry.id for entry in owner.history}
        start = len(peer.frames)
        with pytest.raises(RuntimeError):
            await owner.delete_conversation_item("unobserved-provider-item")
        deleting = asyncio.create_task(owner.delete_conversation_item(provider_item_id))
        try:
            sent = await peer.frame(
                lambda frame: frame["type"] == "conversation.item.delete", after=start
            )
            assert sent["item_id"] == provider_item_id
            assert not deleting.done()
            await peer.socket.send_json(
                {"type": "conversation.item.deleted", "item_id": provider_item_id}
            )
            await deleting
            deleted, audited = False, False
            async for event in events:
                if (
                    isinstance(event, RealtimeConversationItemDeleted)
                    and event.item_id == provider_item_id
                ):
                    deleted = True
                if (
                    isinstance(event, RealtimeHistory)
                    and event.completed
                    and event.item.kind == "provider_item_deleted"
                ):
                    assert event.item.provider_item_id == provider_item_id
                    audited = True
                if deleted and audited:
                    break
            assert prior_ids <= {entry.id for entry in owner.history}
            audit = [entry for entry in owner.history if entry.kind == "provider_item_deleted"]
            assert audit[-1].provider_item_id == provider_item_id
            assert (
                sum(frame["type"] == "conversation.item.delete" for frame in peer.frames[start:])
                == 1
            )
        finally:
            if not deleting.done():
                deleting.cancel()
            await asyncio.gather(deleting, return_exceptions=True)


@pytest.mark.parametrize("queued", [False, True], ids=["active-commit", "commit-before-lock"])
async def test_adapter_nested_input_rejects_behind_reserved_commit(
    realtime_peer, monkeypatch, queued
):
    peer = realtime_peer
    adapter = SendHookAdapter(
        peer.responses.url.replace("http://", "ws://") + "/realtime?model=fixture-realtime"
    )
    in_callback, try_nested, rejected = asyncio.Event(), asyncio.Event(), asyncio.Event()
    admitted, release_admission, commit_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    errors = []
    async with (
        timeout(20),
        NativeProvider(
            base_url=peer.responses.url, api_key="synthetic-input-guard", supports_websockets=False
        ) as provider,
        Agent(instructions="Listen.", provider=provider).session() as session,
        session.realtime(config=RealtimeConfig(model="fixture-realtime", adapter=adapter)) as owner,
    ):
        await peer.frame(lambda frame: frame["type"] == "session.update")
        original_send = adapter.forward
        assert original_send is not None

        async def callback(frame):
            kind = json.loads(frame)["type"]
            if kind == ("input_audio_buffer.append" if queued else "input_audio_buffer.commit"):
                in_callback.set()
                await try_nested.wait()
                try:
                    await owner.send_text("Must not enqueue from this callback")
                except RuntimeError as error:
                    errors.append(error)
                    rejected.set()
            await original_send(frame)

        monkeypatch.setattr(adapter, "send_hook", callback)
        original_call = owner._call
        first_audio = True

        async def hold_admission(operation, **params):
            nonlocal first_audio
            result = await original_call(operation, **params)
            if queued and operation == "audio" and first_audio:
                first_audio = False
                admitted.set()
                await release_admission.wait()
            return result

        monkeypatch.setattr(owner, "_call", hold_admission)
        start = len(peer.frames)
        audio = asyncio.create_task(owner.send_audio(b"\0\0", commit=not queued))
        committing = None
        try:
            await in_callback.wait()
            if queued:
                await admitted.wait()

                async def commit_after_audio():
                    commit_started.set()
                    await owner.send_audio(b"", commit=True)

                committing = asyncio.create_task(commit_after_audio())
                await commit_started.wait()
            try_nested.set()
            await rejected.wait()
            assert len(errors) == 1
            assert "Realtime adapter callback cannot" in str(errors[0])
            release_admission.set()
            await asyncio.gather(*(task for task in (audio, committing) if task is not None))
            await owner.send_text("Ordinary input remains usable")
            await peer.frame(lambda frame: frame["type"] == "response.create", after=start)
            assert [frame["type"] for frame in peer.frames[start:]] == [
                "input_audio_buffer.append",
                "input_audio_buffer.commit",
                "conversation.item.create",
                "response.create",
            ]
        finally:
            try_nested.set()
            release_admission.set()
            await asyncio.gather(
                *(task for task in (audio, committing) if task is not None), return_exceptions=True
            )
