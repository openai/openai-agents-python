"""Python forwarding for provider history; these doubles do not verify Core behavior."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from _agents_codex.realtime import RealtimeConfig, RealtimeSession
from agents import Session


def owner() -> tuple[RealtimeSession, list[tuple[str, dict[str, Any]]]]:
    requests: list[tuple[str, dict[str, Any]]] = []

    async def call(operation: str, request: str) -> str:
        params = json.loads(request)
        requests.append((operation, params))
        if operation.endswith("retrieve_history_sdk"):
            return json.dumps([{"id": "provider-item", "content": [{"audio": "AQI="}]}])
        if operation.endswith("retrieve_item_sdk"):
            return json.dumps({"id": params["item_id"], "content": [{"audio": "AQI="}]})
        return "{}"

    session = cast(Session[Any, Any], SimpleNamespace(_native=SimpleNamespace(call=call)))
    voice = RealtimeSession(session, config=RealtimeConfig())
    voice._live = True
    voice._ready.set_result(None)
    return voice, requests


async def test_history_forwards_ordered_snapshot_to_exact_generation() -> None:
    voice, requests = owner()
    items = [
        {"id": "first", "type": "message", "content": [{"text": "first"}]},
        {"id": "second", "type": "message", "content": [{"text": "second"}]},
    ]
    updating = asyncio.create_task(voice.update_history(items))
    await asyncio.sleep(0)
    items[0]["content"][0]["text"] = "mutated"
    await updating
    assert requests == [
        (
            "realtime_conversation_update_history_sdk",
            {
                "generation": voice._generation,
                "items": [
                    {"id": "first", "type": "message", "content": [{"text": "first"}]},
                    {"id": "second", "type": "message", "content": [{"text": "second"}]},
                ],
            },
        )
    ]
    assert voice.history == ()


async def test_provider_retrieval_preserves_media_and_keeps_audit_separate() -> None:
    voice, requests = owner()
    item = await voice.retrieve_conversation_item("provider-item")
    history = await voice.retrieve_conversation_history()
    assert history == [item]
    assert item["content"] == [{"audio": "AQI="}]
    assert requests == [
        (
            "realtime_conversation_retrieve_item_sdk",
            {"generation": voice._generation, "item_id": "provider-item"},
        ),
        ("realtime_conversation_retrieve_history_sdk", {"generation": voice._generation}),
    ]
    assert voice.history == ()


@pytest.mark.parametrize("item_id", ["", None, 1])
async def test_item_retrieval_rejects_invalid_identity_before_dispatch(item_id: Any) -> None:
    voice, requests = owner()
    with pytest.raises(ValueError, match="provider_item_id"):
        await voice.retrieve_conversation_item(item_id)
    assert requests == []


@pytest.mark.parametrize("operation", ["update", "item", "history"])
async def test_history_operations_require_live_owner(operation: str) -> None:
    voice, requests = owner()
    voice._live = False
    with pytest.raises(RuntimeError):
        if operation == "update":
            await voice.update_history([])
        elif operation == "item":
            await voice.retrieve_conversation_item("provider-item")
        else:
            await voice.retrieve_conversation_history()
    assert requests == []


async def test_history_update_cancellation_joins_native_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice, _ = owner()
    entered = asyncio.Event()
    release = asyncio.Event()
    accepted = asyncio.Event()

    async def call(operation: str, request: str) -> str:
        assert operation == "realtime_conversation_update_history_sdk"
        entered.set()
        await release.wait()
        accepted.set()
        return "{}"

    monkeypatch.setattr(voice._session._native, "call", call)
    updating = asyncio.create_task(voice.update_history([]))
    await entered.wait()
    try:
        updating.cancel()
        await asyncio.sleep(0)
        assert not updating.done()
        assert not accepted.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await updating
    assert accepted.is_set()


async def test_history_rejection_drops_media_from_sdk_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice, _ = owner()
    media = "synthetic-private-audio-payload"

    async def reject(operation: str, request: str) -> str:
        assert media in request
        raise RuntimeError("Provider rejected history")

    monkeypatch.setattr(voice._session._native, "call", reject)
    with pytest.raises(RuntimeError, match="Provider rejected") as caught:
        await voice.update_history(
            [{"id": "private", "content": [{"type": "input_audio", "audio": media}]}]
        )
    traceback = caught.value.__traceback__
    frames = []
    while traceback is not None:
        if (
            traceback.tb_frame.f_code.co_filename
            == RealtimeSession.update_history.__code__.co_filename
        ):
            frames.append(traceback.tb_frame)
        traceback = traceback.tb_next
    assert frames
    assert all(media not in repr(frame.f_locals) for frame in frames)
