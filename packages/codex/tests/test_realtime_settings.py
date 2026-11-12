"""Canonical settings remain sparse, detached and finite before native admission."""

from typing import Any, cast

import pytest

from _agents_codex.realtime_settings import (
    NativeRealtimeSessionUpdate,
    NativeRealtimeSettings,
    serialize_realtime_session_update,
    serialize_realtime_settings,
)


def test_sparse_update_and_explicit_disabled_settings():
    assert serialize_realtime_settings({}) == {}
    assert serialize_realtime_session_update({"output_speed": 1.25}) == {"output_speed": 1.25}
    disabled: NativeRealtimeSettings = {
        "turn_detection": None,
        "noise_reduction": None,
        "transcription": None,
        "tracing": None,
        "prompt": None,
    }
    assert serialize_realtime_settings(disabled) == disabled


def test_all_typed_settings_keep_canonical_names_and_false_zero_values():
    settings: NativeRealtimeSessionUpdate = {
        "input_format": "g711_ulaw",
        "output_format": "g711_alaw",
        "noise_reduction": {"type": "far_field"},
        "transcription": {
            "model": "gpt-transcribe",
            "language": "en",
            "prompt": "names",
            "keywords": ["Codex"],
            "languages": ["en", "es"],
            "delay": "low",
        },
        "turn_detection": {
            "type": "server_vad",
            "create_response": False,
            "interrupt_response": False,
            "threshold": 0.0,
            "prefix_padding_ms": 0,
            "silence_duration_ms": 500,
            "idle_timeout_ms": 10000,
        },
        "output_voice": {"id": "voice_123"},
        "output_speed": 0.75,
        "prompt": {
            "id": "pmpt_123",
            "version": "2",
            "variables": {
                "city": "Paris",
                "text": {"type": "input_text", "text": "hello"},
                "image": {
                    "type": "input_image",
                    "image_url": "https://example.com/a.png",
                    "detail": "auto",
                },
                "file": {"type": "input_file", "file_id": "file_123"},
            },
        },
        "tracing": {
            "workflow_name": "voice",
            "group_id": "group_123",
            "metadata": {"camelCase": True},
        },
        "max_output_tokens": "inf",
        "parallel_tool_calls": False,
        "reasoning": {"effort": "high"},
        "model": "gpt-realtime",
        "instructions": "brief",
        "output_modality": "audio",
    }
    assert serialize_realtime_session_update(settings) == settings
    assert serialize_realtime_settings(
        {"input_format": "pcm16", "output_voice": "marin", "tracing": "auto"}
    ) == {
        "input_format": "pcm16",
        "output_voice": "marin",
        "tracing": "auto",
    }
    assert serialize_realtime_settings(
        {"turn_detection": {"type": "semantic_vad", "eagerness": "high"}}
    ) == {
        "turn_detection": {"type": "semantic_vad", "eagerness": "high"},
    }


def test_serialization_detaches_mutable_input():
    metadata: dict[str, Any] = {"values": [1]}
    settings: NativeRealtimeSettings = {"tracing": {"metadata": metadata}}
    result = serialize_realtime_settings(settings)
    metadata["values"].append(2)
    assert result == {"tracing": {"metadata": {"values": [1]}}}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("field", ["output_speed", "threshold", "metadata"])
def test_nonfinite_values_are_not_changed_to_null(value, field):
    payload: dict[str, Any]
    if field == "threshold":
        payload = {"turn_detection": {"type": "server_vad", "threshold": value}}
    elif field == "metadata":
        payload = {"tracing": {"metadata": {"value": value}}}
    else:
        payload = {"output_speed": value}
    with pytest.raises(ValueError):
        serialize_realtime_settings(cast(NativeRealtimeSettings, payload))


@pytest.mark.parametrize(
    "choice",
    [
        "auto",
        "none",
        "required",
        {"type": "function", "name": "weather"},
        {"type": "mcp", "server_label": "docs"},
        {"type": "mcp", "server_label": "docs", "name": "weather"},
    ],
)
def test_tool_choice_is_sparse_and_detached(choice: Any) -> None:
    choice = dict(choice) if isinstance(choice, dict) else choice
    value = {"tool_choice": choice}
    assert serialize_realtime_settings(cast(NativeRealtimeSettings, value)) == value
    assert serialize_realtime_session_update(cast(NativeRealtimeSessionUpdate, value)) == value
    assert "tool_choice" not in serialize_realtime_settings({})
    if isinstance(choice, dict):
        result = serialize_realtime_settings(cast(NativeRealtimeSettings, value))
        original = dict(choice)
        choice["name"] = "changed"
        assert result["tool_choice"] == original


@pytest.mark.parametrize(
    "choice",
    [
        None,
        "weather",
        [],
        {"type": "mcp", "name": "weather"},
        {"type": "mcp", "server_label": "docs", "name": None},
        {"type": "mcp", "server_label": "docs", "unexpected": "field"},
        {"type": "function"},
    ],
)
def test_invalid_tool_choice_is_rejected_before_native_io(choice: Any) -> None:
    with pytest.raises(ValueError, match="tool_choice"):
        serialize_realtime_settings(cast(NativeRealtimeSettings, {"tool_choice": choice}))


@pytest.mark.parametrize("mode", ["server_vad", "semantic_vad"])
def test_vad_provider_data_is_detached_and_preserves_empty_and_null(mode):
    data = {"vendor_tuning": {"enabled": False, "delay_ms": 0}}
    source = {"turn_detection": {"type": mode, "provider_data": data}}
    result = serialize_realtime_settings(source)
    data["vendor_tuning"]["delay_ms"] = 9
    assert result["turn_detection"]["provider_data"]["vendor_tuning"]["delay_ms"] == 0
    for reset in ({}, None):
        assert serialize_realtime_session_update(
            {"turn_detection": {"type": mode, "provider_data": reset}}
        ) == {"turn_detection": {"type": mode, "provider_data": reset}}


@pytest.mark.parametrize(
    "key", ["type", "threshold", "interrupt_response", "interruptResponse", "provider_data"]
)
def test_vad_provider_data_cannot_replace_typed_fields(key):
    with pytest.raises(ValueError, match="cannot override typed"):
        serialize_realtime_settings(
            {"turn_detection": {"type": "server_vad", "provider_data": {key: True}}}
        )


@pytest.mark.parametrize("data", [[], "invalid", {1: "not a string key"}])
def test_vad_provider_data_requires_a_json_object(data):
    with pytest.raises(ValueError, match="JSON object"):
        serialize_realtime_settings(
            {"turn_detection": {"type": "server_vad", "provider_data": data}}
        )
