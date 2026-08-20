from collections.abc import AsyncIterator
from typing import cast

from agents.voice import AudioInput, StreamedAudioInput
from agents.voice.model import (
    STTModel,
    STTModelSettings,
    StreamedTranscriptionSession,
    TTSModel,
    TTSModelSettings,
)
from agents.voice.pipeline import VoicePipeline
from agents.voice.workflow import VoiceWorkflowBase


class _FalseySTTModel(STTModel):
    def __bool__(self) -> bool:
        return False

    @property
    def model_name(self) -> str:
        return "falsey-stt"

    async def transcribe(
        self,
        input: AudioInput,
        settings: STTModelSettings,
        trace_include_sensitive_data: bool,
        trace_include_sensitive_audio_data: bool,
    ) -> str:
        return ""

    async def create_session(
        self,
        input: StreamedAudioInput,
        settings: STTModelSettings,
        trace_include_sensitive_data: bool,
        trace_include_sensitive_audio_data: bool,
    ) -> StreamedTranscriptionSession:
        raise NotImplementedError


class _FalseyTTSModel(TTSModel):
    def __bool__(self) -> bool:
        return False

    @property
    def model_name(self) -> str:
        return "falsey-tts"

    async def run(self, text: str, settings: TTSModelSettings) -> AsyncIterator[bytes]:
        if False:
            yield b""


def _workflow() -> VoiceWorkflowBase:
    return cast(VoiceWorkflowBase, object())


def test_voice_pipeline_preserves_falsey_custom_stt_model() -> None:
    model = _FalseySTTModel()
    pipeline = VoicePipeline(workflow=_workflow(), stt_model=model)

    assert pipeline._get_stt_model() is model


def test_voice_pipeline_preserves_falsey_custom_tts_model() -> None:
    model = _FalseyTTSModel()
    pipeline = VoicePipeline(workflow=_workflow(), tts_model=model)

    assert pipeline._get_tts_model() is model
