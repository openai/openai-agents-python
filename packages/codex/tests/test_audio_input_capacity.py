import json
from typing import Any

import pytest

from _agents_codex.initial_messages import _capture_initial_messages
from _agents_codex.input import _native_input

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
