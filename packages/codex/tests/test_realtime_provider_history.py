"""Python forwarding for provider history; these doubles do not verify Core behavior."""

import asyncio
import json
from collections import UserDict
from types import MappingProxyType, SimpleNamespace
from typing import Any, cast

import pytest

from _agents_codex.realtime import RealtimeConfig, RealtimeSession
from agents import Session
from tests._asyncio import timeout


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


@pytest.mark.parametrize("text", ["a" * (2 * 1024 * 1024 + 1), "\x00" * 350_000])
@pytest.mark.parametrize("mapping", [dict, MappingProxyType, UserDict])
async def test_history_input_budget_rejects_before_json_copy(
    text: str, mapping: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    voice, requests = owner()

    def unexpected(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Oversized history must be rejected before JSON serialization")

    monkeypatch.setattr("_agents_codex.realtime.json.dumps", unexpected)
    with pytest.raises(ValueError, match="8 MiB"):
        await voice.update_history(
            [
                mapping(
                    {
                        "id": "oversized-history",
                        "content": [mapping({"type": "input_text", "text": text})],
                    }
                )
            ]
        )
    assert requests == []


async def test_history_input_budget_counts_nested_json_tuples_before_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice, requests = owner()

    def unexpected(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Oversized JSON tuples must be rejected before serialization")

    monkeypatch.setattr("_agents_codex.realtime.json.dumps", unexpected)
    with pytest.raises(ValueError, match="8 MiB"):
        await voice.update_history(
            ({"id": "oversized-history", "content": ({"text": "a" * 2_100_000},)},)
        )
    assert requests == []


@pytest.mark.parametrize("mapping", [dict, MappingProxyType, UserDict])
async def test_cancelled_history_retains_aggregate_input_budget_until_native_settles(
    mapping: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice, _ = owner()
    entered, release = asyncio.Event(), asyncio.Event()
    accepted: list[str] = []

    async def call(operation: str, request: str) -> str:
        entered.set()
        await release.wait()
        accepted.append(operation)
        return "{}"

    monkeypatch.setattr(voice._session._native, "call", call)
    items = (mapping({"id": "large-history", "content": (mapping({"text": "a" * 1_100_000}),)}),)
    async with timeout(5):
        updating = asyncio.create_task(voice.update_history(items))
        try:
            await entered.wait()
            updating.cancel()
            await asyncio.sleep(0)
            assert not updating.done()
            with pytest.raises(ValueError, match="input queue"):
                await voice.update_history(items)
            assert accepted == []
        finally:
            release.set()
            await asyncio.gather(updating, return_exceptions=True)
        assert updating.cancelled()
        assert accepted == ["realtime_conversation_update_history_sdk"]
        await voice.update_history(items)
        assert accepted == ["realtime_conversation_update_history_sdk"] * 2


@pytest.mark.parametrize("mapping", [MappingProxyType, UserDict])
async def test_history_captures_declared_nested_mapping_inputs(mapping: Any) -> None:
    voice, requests = owner()
    content = {"type": "input_text", "text": "Original mapping text"}
    nested = mapping(content)
    row = mapping(
        {"id": "mapping-message", "type": "message", "role": "user", "content": (nested,)}
    )
    updating = asyncio.create_task(voice.update_history((row,)))
    await asyncio.sleep(0)
    content["text"] = "Mutated after admission"
    if isinstance(nested, UserDict):
        nested["text"] = "Mutated after admission"
    await updating
    assert requests == [
        (
            "realtime_conversation_update_history_sdk",
            {
                "generation": voice._generation,
                "items": [
                    {
                        "id": "mapping-message",
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Original mapping text"}],
                    }
                ],
            },
        )
    ]
