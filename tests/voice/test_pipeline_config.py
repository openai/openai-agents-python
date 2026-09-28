# pyright: reportCallIssue=true, reportArgumentType=true
from dataclasses import replace
from typing import Any

import pytest

from agents.tracing import TracingConfig
from agents.voice import STTModelSettings, TTSModelSettings, VoicePipelineConfig
from agents.voice.models.openai_model_provider import OpenAIVoiceModelProvider


@pytest.mark.parametrize("legacy", [False, True])
def test_released_positional_config_fields(legacy: bool) -> None:
    provider = OpenAIVoiceModelProvider()
    tracing = TracingConfig(api_key="synthetic-placeholder")
    metadata = {"source": "synthetic"}
    stt = STTModelSettings(language="en")
    tts = TTSModelSettings(voice="alloy")
    if legacy:
        config = VoicePipelineConfig(
            provider,
            False,
            False,
            False,
            "workflow",
            "group",
            metadata,
            stt,
            tts,
            tracing=tracing,
        )
    else:
        config = VoicePipelineConfig(
            provider, False, tracing, False, False, "workflow", "group", metadata, stt, tts
        )

    assert config.model_provider is provider
    assert config.tracing_disabled is False
    assert config.tracing is tracing
    assert config.trace_include_sensitive_data is False
    assert config.trace_include_sensitive_audio_data is False
    assert config.workflow_name == "workflow"
    assert config.group_id == "group"
    assert config.trace_metadata is metadata
    assert config.stt_settings is stt
    assert config.tts_settings is tts
    assert replace(config, workflow_name="replacement") == VoicePipelineConfig(
        provider, False, tracing, False, False, "replacement", "group", metadata, stt, tts
    )


def test_legacy_positional_config_keeps_defaults_and_keyword_settings() -> None:
    provider = OpenAIVoiceModelProvider()
    config = VoicePipelineConfig(provider, False, False)
    assert config.tracing is None
    assert config.trace_include_sensitive_data is False
    assert config.trace_include_sensitive_audio_data is True
    assert config.workflow_name == "Voice Agent"
    assert config.group_id.startswith("group_")
    assert config.trace_metadata is None
    assert config.stt_settings == STTModelSettings()
    assert config.tts_settings == TTSModelSettings()

    config = VoicePipelineConfig(
        provider,
        True,
        True,
        trace_include_sensitive_audio_data=False,
        stt_settings={"language": "en"},
        tts_settings={"voice": "alloy"},
    )
    assert config.tracing_disabled is True
    assert config.tracing is None
    assert config.trace_include_sensitive_data is True
    assert config.trace_include_sensitive_audio_data is False
    assert config.stt_settings.language == "en"
    assert config.tts_settings.voice == "alloy"


def test_legacy_positional_config_rejects_duplicate_arguments() -> None:
    conflicting: dict[str, Any] = {"trace_include_sensitive_data": True}
    with pytest.raises(TypeError, match="multiple values.*trace_include_sensitive_data"):
        VoicePipelineConfig(OpenAIVoiceModelProvider(), False, False, **conflicting)
