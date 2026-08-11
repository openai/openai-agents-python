from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Literal

import numpy as np
import numpy.typing as npt

from agents.voice import (
    StreamedAudioInput,
    STTModelSettings,
    TTSModelSettings,
)
from agents.voice.testing import (
    ScriptedSTTModel,
    ScriptedTranscriptionSession,
    ScriptedTTSModel,
    ScriptedVoiceWorkflow,
)


class ZeroPcmTTSModel(ScriptedTTSModel):
    """Generate deterministic zero-valued PCM for pipeline lifecycle tests."""

    def __init__(self, strategy: Literal["default", "split_words"] = "default") -> None:
        super().__init__(model_name="zero-pcm-tts")
        self.strategy = strategy

    async def run(self, text: str, settings: TTSModelSettings) -> AsyncIterator[bytes]:
        if self.strategy == "default":
            yield np.zeros(2, dtype=np.int16).tobytes()
        elif self.strategy == "split_words":
            for _ in text.split():
                yield np.zeros(2, dtype=np.int16).tobytes()

    async def verify_audio(self, text: str, audio: bytes, dtype: npt.DTypeLike = np.int16) -> None:
        assert audio == np.zeros(2, dtype=dtype).tobytes()

    async def verify_audio_chunks(
        self, text: str, audio_chunks: list[bytes], dtype: npt.DTypeLike = np.int16
    ) -> None:
        assert audio_chunks == [np.zeros(2, dtype=dtype).tobytes() for _word in text.split()]


class QueuedTranscriptionSession(ScriptedTranscriptionSession):
    """Yield mutable queued transcripts for lifecycle-specific pipeline tests."""

    def __init__(self) -> None:
        super().__init__()
        self.outputs: list[str] = []

    async def transcribe_turns(self) -> AsyncIterator[str]:
        for transcript in self.outputs:
            yield transcript

    async def close(self) -> None:
        return None


class QueuedSTTModel(ScriptedSTTModel):
    """Share one transcript queue across static and lifecycle-specific streamed tests."""

    def __init__(self, outputs: list[str] | None = None) -> None:
        super().__init__(outputs or [], model_name="queued-stt")
        self.outputs = self._transcriptions

    async def create_session(
        self,
        input: StreamedAudioInput,
        settings: STTModelSettings,
        trace_include_sensitive_data: bool,
        trace_include_sensitive_audio_data: bool,
    ) -> QueuedTranscriptionSession:
        del input, settings, trace_include_sensitive_data, trace_include_sensitive_audio_data
        session = QueuedTranscriptionSession()
        session.outputs = self.outputs
        return session


class QueuedVoiceWorkflow(ScriptedVoiceWorkflow):
    """A named scripted workflow base for pipeline lifecycle subclasses."""


class StreamedAudioInputFactory:
    @classmethod
    async def get(cls, count: int) -> StreamedAudioInput:
        input = StreamedAudioInput()
        for _ in range(count):
            await input.add_audio(np.zeros(2, dtype=np.int16))
        return input
