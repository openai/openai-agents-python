import logging
from typing import Any

import pytest
from openai.types.realtime.realtime_audio_formats import AudioPCM, AudioPCMA, AudioPCMU

from agents import _debug
from agents.realtime.audio_formats import to_realtime_audio_format


def test_to_realtime_audio_format_from_strings():
    assert to_realtime_audio_format("pcm").type == "audio/pcm"  # type: ignore[union-attr]
    assert to_realtime_audio_format("pcm16").type == "audio/pcm"  # type: ignore[union-attr]
    assert to_realtime_audio_format("audio/pcm").type == "audio/pcm"  # type: ignore[union-attr]
    assert to_realtime_audio_format("pcmu").type == "audio/pcmu"  # type: ignore[union-attr]
    assert to_realtime_audio_format("audio/pcmu").type == "audio/pcmu"  # type: ignore[union-attr]
    assert to_realtime_audio_format("g711_ulaw").type == "audio/pcmu"  # type: ignore[union-attr]
    assert to_realtime_audio_format("pcma").type == "audio/pcma"  # type: ignore[union-attr]
    assert to_realtime_audio_format("audio/pcma").type == "audio/pcma"  # type: ignore[union-attr]
    assert to_realtime_audio_format("g711_alaw").type == "audio/pcma"  # type: ignore[union-attr]


def test_to_realtime_audio_format_passthrough_and_unknown_logs():
    fmt = AudioPCM(type="audio/pcm", rate=24000)
    # Passing a RealtimeAudioFormats should return the same instance
    assert to_realtime_audio_format(fmt) is fmt

    # Unknown string returns None (and logs at debug level internally)
    assert to_realtime_audio_format("something_else") is None


def test_to_realtime_audio_format_none():
    assert to_realtime_audio_format(None) is None


def test_to_realtime_audio_format_from_mapping():
    pcm_exact_rate = to_realtime_audio_format({"type": "audio/pcm", "rate": 24000})
    assert isinstance(pcm_exact_rate, AudioPCM)
    assert pcm_exact_rate.rate == 24000

    pcm = to_realtime_audio_format({"type": "audio/pcm", "rate": 16000})
    assert isinstance(pcm, AudioPCM)
    assert pcm.type == "audio/pcm"
    assert pcm.rate == 24000

    pcm_default_rate = to_realtime_audio_format({"type": "audio/pcm"})
    assert isinstance(pcm_default_rate, AudioPCM)
    assert pcm_default_rate.rate == 24000

    ulaw = to_realtime_audio_format({"type": "audio/pcmu"})
    assert isinstance(ulaw, AudioPCMU)
    assert ulaw.type == "audio/pcmu"

    alaw = to_realtime_audio_format({"type": "audio/pcma"})
    assert isinstance(alaw, AudioPCMA)
    assert alaw.type == "audio/pcma"

    assert to_realtime_audio_format({"type": "audio/unknown", "rate": 8000}) is None


@pytest.mark.parametrize("tool_data_redacted", [False, True])
@pytest.mark.parametrize(
    ("input_audio_format", "expected_message", "expected_type"),
    [
        ("format-secret", "Unknown input audio format", None),
        (
            {"type": "audio/pcm", "rate": "rate-secret"},
            "Unknown PCM rate in input audio format mapping",
            AudioPCM,
        ),
        (
            {"type": "format-secret", "nested": "mapping-secret"},
            "Unknown input audio format mapping",
            None,
        ),
    ],
)
def test_to_realtime_audio_format_redacts_unknown_values(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tool_data_redacted: bool,
    input_audio_format: Any,
    expected_message: str,
    expected_type: type[AudioPCM] | None,
) -> None:
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", tool_data_redacted)
    caplog.set_level(logging.DEBUG, logger="openai.agents")

    result = to_realtime_audio_format(input_audio_format)

    if expected_type is None:
        assert result is None
    else:
        assert isinstance(result, expected_type)
    record = caplog.records[-1]
    assert record.msg == expected_message
    assert record.args == ()
    assert record.exc_info is None
    assert record.exc_text is None
    assert all(value is not input_audio_format for value in record.__dict__.values())
    assert logging.Formatter().format(record) == expected_message


@pytest.mark.parametrize("tool_data_redacted", [False, True])
def test_to_realtime_audio_format_preserves_diagnostic_mapping(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tool_data_redacted: bool,
) -> None:
    input_audio_format = {"type": "format-secret", "nested": "mapping-secret"}
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", False)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", tool_data_redacted)
    caplog.set_level(logging.DEBUG, logger="openai.agents")

    assert to_realtime_audio_format(input_audio_format) is None

    record = caplog.records[-1]
    assert record.msg == "Unknown input_audio_format mapping: %s"
    assert record.args is input_audio_format
    assert record.exc_info is None
    assert record.exc_text is None
    assert "format-secret" in logging.Formatter().format(record)
    assert "mapping-secret" in logging.Formatter().format(record)


def test_to_realtime_audio_format_redaction_does_not_render_hostile_mapping(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class HostileMapping(dict[str, object]):
        def __str__(self) -> str:
            raise AssertionError("redacted logging must not call __str__")

        def __repr__(self) -> str:
            raise AssertionError("redacted logging must not call __repr__")

    input_audio_format = HostileMapping(type="unknown")
    monkeypatch.setattr(_debug, "DONT_LOG_MODEL_DATA", True)
    monkeypatch.setattr(_debug, "DONT_LOG_TOOL_DATA", False)
    caplog.set_level(logging.DEBUG, logger="openai.agents")

    assert to_realtime_audio_format(input_audio_format) is None

    record = caplog.records[-1]
    assert record.msg == "Unknown input audio format mapping"
    assert record.args == ()
    assert record.exc_info is None
    assert record.exc_text is None
    assert all(value is not input_audio_format for value in record.__dict__.values())
    assert logging.Formatter().format(record) == "Unknown input audio format mapping"
