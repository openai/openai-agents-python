import json
from contextlib import asynccontextmanager
from typing import Any

import pytest

from _agents_codex.initial_messages import _capture_initial_messages
from _agents_codex.input import _native_input
from agents import Agent

CAPACITY = 64 * 1024 * 1024


def message(audio: str) -> Any:
    return {"role": "user", "content": [{"type": "input_audio", "audio_url": audio}]}


def test_raw_audio_rejects_before_json_encoding(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("oversized source reached JSON serialization")

    monkeypatch.setattr(json, "dumps", forbidden)
    with pytest.raises(ValueError, match="Audio input exceeds"):
        _native_input(message("https://audio.test/" + "a" * CAPACITY))


def test_audio_quoting_and_aggregate_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    original = json.dumps
    largest = 0

    def counting(value: Any, *args: Any, **kwargs: Any) -> str:
        nonlocal largest
        if isinstance(value, str):
            largest = max(largest, len(value))
        return original(value, *args, **kwargs)

    monkeypatch.setattr(json, "dumps", counting)
    with pytest.raises(ValueError, match="Audio input exceeds"):
        _native_input(message("https://audio.test/" + "😀" * (CAPACITY // 12)))
    with pytest.raises(ValueError, match="Audio input exceeds"):
        _native_input({"role": "user", "content": message("a" * (CAPACITY // 2))["content"] * 2})
    assert largest <= 32_768


def test_audio_reference_boundary() -> None:
    overhead = len(json.dumps([{"type": "audio", "audio_url": ""}]))
    source = "a" * (CAPACITY - overhead)
    assert _native_input(message(source)) == [{"type": "audio", "audio_url": source}]
    with pytest.raises(ValueError, match="Audio input exceeds"):
        _native_input(message(source + "a"))


def test_initial_messages_share_audio_budget() -> None:
    source = "a" * (CAPACITY // 2)
    with pytest.raises(ValueError, match="Audio input exceeds"):
        _capture_initial_messages([message(source), message(source)])


async def test_first_run_audio_budget_precedes_session_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered: list[str] = []

    @asynccontextmanager
    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        entered.append("session")
        raise AssertionError("oversized aggregate acquired Session resources")
        yield

    monkeypatch.setattr(Agent, "session", forbidden)
    source = "a" * (CAPACITY // 2)
    with pytest.raises(ValueError, match="Audio input exceeds"):
        await Agent(model="custom-model", instructions="Audio admission test").run(
            message(source), initial_messages=[message(source)]
        )
    assert entered == []


async def test_seed_envelope_precedes_session_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    entered: list[str] = []

    @asynccontextmanager
    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        entered.append("session")
        raise AssertionError("oversized seed envelope acquired Session resources")
        yield

    monkeypatch.setattr(Agent, "_session_context", forbidden)
    overhead = len(json.dumps([{"type": "audio", "audio_url": ""}]))
    with pytest.raises(ValueError, match="Audio input exceeds"):
        async with Agent(model="custom-model", instructions="Audio admission test").session(
            initial_messages=[message("a" * (CAPACITY - overhead))]
        ):
            pytest.fail("oversized seed admitted")
    assert entered == []


def test_complete_audio_envelope_exact_boundary_and_bounded_quoting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from _agents_codex import _media

    payload = {
        "input": [{"type": "audio", "audio_url": "😀\n" * 32_769}],
        "tracing_context": {"traceparent": "0" * 55},
        "start": {"final_output_json_schema": {"enum": [None, False, 1.25]}},
    }
    expected = json.dumps(payload)
    original = json.dumps
    full_encodes = 0
    largest_string = 0

    def observe(value: Any, *args: Any, **kwargs: Any) -> str:
        nonlocal full_encodes, largest_string
        if value is payload:
            full_encodes += 1
        if isinstance(value, str):
            largest_string = max(largest_string, len(value))
        return original(value, *args, **kwargs)

    monkeypatch.setattr(json, "dumps", observe)
    monkeypatch.setattr(_media, "_MAX_AUDIO_INPUT_BYTES", len(expected))
    assert _media._audio_json(payload) == expected
    assert full_encodes == 1
    assert largest_string <= 32_768
    monkeypatch.setattr(_media, "_MAX_AUDIO_INPUT_BYTES", len(expected) - 1)
    with pytest.raises(ValueError, match="Audio input exceeds"):
        _media._audio_json(payload)
    assert full_encodes == 1
