from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import pydantic
from openai.types.realtime.realtime_audio_config import RealtimeAudioConfig
from openai.types.realtime.realtime_audio_formats import (
    AudioPCM,
    AudioPCMA,
    AudioPCMU,
)
from openai.types.realtime.realtime_session_create_request import (
    RealtimeSessionCreateRequest,
)
from openai.types.realtime.realtime_transcription_session_create_request import (
    RealtimeTranscriptionSessionCreateRequest,
)

from agents.realtime.openai_realtime import OpenAIRealtimeWebSocketModel as Model


class _DummyModel(pydantic.BaseModel):
    type: str


def _session_with_output(fmt: Any | None) -> RealtimeSessionCreateRequest:
    if fmt is None:
        return RealtimeSessionCreateRequest(type="realtime", model="gpt-realtime")
    return RealtimeSessionCreateRequest(
        type="realtime",
        model="gpt-realtime",
        # Use dict for output to avoid importing non-exported symbols in tests
        audio=RealtimeAudioConfig(output=cast(Any, {"format": fmt})),
    )


def test_normalize_session_payload_variants() -> None:
    # Passthrough: already a realtime session model
    rt = _session_with_output(AudioPCM(type="audio/pcm"))
    assert Model._normalize_session_payload(rt) is rt

    # Transcription session instance should be ignored
    ts = RealtimeTranscriptionSessionCreateRequest(type="transcription")
    assert Model._normalize_session_payload(ts) is None

    # Transcription-like mapping should be ignored
    transcription_mapping: Mapping[str, object] = {"type": "transcription"}
    assert Model._normalize_session_payload(transcription_mapping) is None

    # Valid realtime mapping should be converted to model
    realtime_mapping: Mapping[str, object] = {"type": "realtime", "model": "gpt-realtime"}
    as_model = Model._normalize_session_payload(realtime_mapping)
    assert isinstance(as_model, RealtimeSessionCreateRequest)
    assert as_model.type == "realtime"

    # Invalid mapping returns None
    invalid_mapping: Mapping[str, object] = {"type": "bogus"}
    assert Model._normalize_session_payload(invalid_mapping) is None


def test_extract_audio_format_from_session_objects() -> None:
    # Known OpenAI audio format models -> normalized names
    s_pcm = _session_with_output(AudioPCM(type="audio/pcm"))
    assert Model._extract_audio_format(s_pcm) == "pcm16"

    s_ulaw = _session_with_output(AudioPCMU(type="audio/pcmu"))
    assert Model._extract_audio_format(s_ulaw) == "g711_ulaw"

    s_alaw = _session_with_output(AudioPCMA(type="audio/pcma"))
    assert Model._extract_audio_format(s_alaw) == "g711_alaw"

    # Missing/None output format -> None
    s_none = _session_with_output(None)
    assert Model._extract_audio_format(s_none) is None


def test_normalize_audio_format_fallbacks() -> None:
    # String passthrough
    assert Model._normalize_audio_format("pcm24") == "pcm24"

    # Mapping with type field
    assert Model._normalize_audio_format({"type": "g711_ulaw"}) == "g711_ulaw"

    # Pydantic model with type field
    assert Model._normalize_audio_format(_DummyModel(type="custom")) == "custom"

    # Object with attribute 'type'
    class HasType:
        def __init__(self) -> None:
            self.type = "weird"

    assert Model._normalize_audio_format(HasType()) == "weird"
