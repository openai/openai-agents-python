"""Deterministic speech and workflow components for Voice pipeline tests."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .imports import np
from .input import AudioInput, StreamedAudioInput
from .model import (
    StreamedTranscriptionSession,
    STTModel,
    STTModelSettings,
    TTSModel,
    TTSModelSettings,
)
from .workflow import VoiceWorkflowBase


class VoiceScriptError(Exception):
    """Base exception for an invalid or incompletely consumed Voice script."""


class UnexpectedVoiceCall(VoiceScriptError):
    """Raised when a Voice component is called after its script is exhausted."""

    def __init__(self, message: str, *, operation: str) -> None:
        super().__init__(message)
        self.operation = operation


class UnconsumedVoiceSteps(VoiceScriptError):
    """Raised when a test finishes before consuming every configured Voice step."""

    def __init__(self, message: str, *, remaining_steps: int) -> None:
        super().__init__(message)
        self.remaining_steps = remaining_steps


@dataclass(frozen=True)
class STTCall:
    """A recorded static transcription call."""

    input: AudioInput
    settings: STTModelSettings
    trace_include_sensitive_data: bool
    trace_include_sensitive_audio_data: bool


@dataclass(frozen=True)
class STTSessionCall:
    """A recorded streamed transcription-session creation call."""

    input: StreamedAudioInput
    settings: STTModelSettings
    trace_include_sensitive_data: bool
    trace_include_sensitive_audio_data: bool


@dataclass(frozen=True)
class TTSCall:
    """A recorded text-to-speech call."""

    text: str
    settings: TTSModelSettings


@dataclass(frozen=True)
class TTSResult:
    """The PCM byte chunks returned by one text-to-speech call."""

    chunks: Sequence[bytes] = field(default_factory=tuple)


TranscriptionResult = str | Exception
TTSResultItem = TTSResult | Sequence[bytes] | Exception
WorkflowResult = str | Sequence[str] | Exception

_START_NOT_CONFIGURED: Any = object()


def _snapshot_audio_input(input: AudioInput) -> AudioInput:
    return AudioInput(
        buffer=input.buffer.copy(),
        frame_rate=input.frame_rate,
        sample_width=input.sample_width,
        channels=input.channels,
    )


def _snapshot_stt_call(call: STTCall) -> STTCall:
    return STTCall(
        input=_snapshot_audio_input(call.input),
        settings=copy.deepcopy(call.settings),
        trace_include_sensitive_data=call.trace_include_sensitive_data,
        trace_include_sensitive_audio_data=call.trace_include_sensitive_audio_data,
    )


def _snapshot_stt_session_call(call: STTSessionCall) -> STTSessionCall:
    return STTSessionCall(
        input=call.input,
        settings=copy.deepcopy(call.settings),
        trace_include_sensitive_data=call.trace_include_sensitive_data,
        trace_include_sensitive_audio_data=call.trace_include_sensitive_audio_data,
    )


def _snapshot_tts_call(call: TTSCall) -> TTSCall:
    return TTSCall(text=call.text, settings=copy.deepcopy(call.settings))


class ScriptedTranscriptionSession(StreamedTranscriptionSession):
    """A closable stream of configured transcription turns."""

    def __init__(
        self,
        turns: str | Iterable[TranscriptionResult] = (),
        *,
        close_error: Exception | None = None,
    ) -> None:
        self._turns: list[TranscriptionResult] = [turns] if isinstance(turns, str) else list(turns)
        self._close_error = close_error
        self.closed = False
        self.close_calls = 0

    async def transcribe_turns(self) -> AsyncIterator[str]:
        while self._turns and not self.closed:
            turn = self._turns.pop(0)
            if isinstance(turn, Exception):
                raise turn
            yield turn

    async def close(self) -> None:
        self.close_calls += 1
        if self.closed:
            return
        self.closed = True
        if self._close_error is not None:
            raise self._close_error

    def assert_complete(self) -> None:
        """Raise when configured transcript turns remain unconsumed."""
        if self._turns:
            raise UnconsumedVoiceSteps(
                f"{len(self._turns)} scripted transcription turn(s) were not consumed.",
                remaining_steps=len(self._turns),
            )


class ScriptedSTTModel(STTModel):
    """A deterministic speech-to-text model for static and streamed audio tests."""

    def __init__(
        self,
        transcriptions: str | Iterable[TranscriptionResult] = (),
        *,
        sessions: str
        | Iterable[ScriptedTranscriptionSession | Iterable[TranscriptionResult] | Exception] = (),
        model_name: str = "scripted-stt",
    ) -> None:
        self._transcriptions: list[TranscriptionResult] = (
            [transcriptions] if isinstance(transcriptions, str) else list(transcriptions)
        )
        configured_sessions = [sessions] if isinstance(sessions, str) else sessions
        self._sessions: list[
            ScriptedTranscriptionSession | tuple[TranscriptionResult, ...] | Exception
        ] = [
            configured
            if isinstance(configured, ScriptedTranscriptionSession | Exception)
            else (configured,)
            if isinstance(configured, str)
            else tuple(configured)
            for configured in configured_sessions
        ]
        self._model_name = model_name
        self._calls: list[STTCall] = []
        self._session_calls: list[STTSessionCall] = []
        self._created_sessions: list[ScriptedTranscriptionSession] = []

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def calls(self) -> tuple[STTCall, ...]:
        """Return detached snapshots of recorded static transcription calls."""
        return tuple(_snapshot_stt_call(call) for call in self._calls)

    @property
    def session_calls(self) -> tuple[STTSessionCall, ...]:
        """Return detached snapshots of recorded streamed-session calls."""
        return tuple(_snapshot_stt_session_call(call) for call in self._session_calls)

    @property
    def created_sessions(self) -> tuple[ScriptedTranscriptionSession, ...]:
        """Return created sessions while preserving their live object identity."""
        return tuple(self._created_sessions)

    async def transcribe(
        self,
        input: AudioInput,
        settings: STTModelSettings,
        trace_include_sensitive_data: bool,
        trace_include_sensitive_audio_data: bool,
    ) -> str:
        call = STTCall(
            input=input,
            settings=settings,
            trace_include_sensitive_data=trace_include_sensitive_data,
            trace_include_sensitive_audio_data=trace_include_sensitive_audio_data,
        )
        call = _snapshot_stt_call(call)
        self._calls.append(call)
        if not self._transcriptions:
            raise UnexpectedVoiceCall(
                "Unexpected static transcription call: no scripted transcriptions remain.",
                operation="static_transcription",
            )
        result = self._transcriptions.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def create_session(
        self,
        input: StreamedAudioInput,
        settings: STTModelSettings,
        trace_include_sensitive_data: bool,
        trace_include_sensitive_audio_data: bool,
    ) -> StreamedTranscriptionSession:
        call = STTSessionCall(
            input=input,
            settings=settings,
            trace_include_sensitive_data=trace_include_sensitive_data,
            trace_include_sensitive_audio_data=trace_include_sensitive_audio_data,
        )
        call = _snapshot_stt_session_call(call)
        self._session_calls.append(call)
        if not self._sessions:
            raise UnexpectedVoiceCall(
                "Unexpected streamed transcription session: no scripted sessions remain.",
                operation="streamed_session",
            )
        configured = self._sessions.pop(0)
        if isinstance(configured, Exception):
            raise configured
        session = (
            configured
            if isinstance(configured, ScriptedTranscriptionSession)
            else ScriptedTranscriptionSession(configured)
        )
        self._created_sessions.append(session)
        return session

    def assert_complete(self) -> None:
        """Raise when static transcriptions or sessions remain unconsumed."""
        remaining = len(self._transcriptions) + len(self._sessions)
        if remaining:
            raise UnconsumedVoiceSteps(
                f"{remaining} scripted STT step(s) were not consumed.",
                remaining_steps=remaining,
            )
        for session in self._created_sessions:
            session.assert_complete()


class ScriptedTTSModel(TTSModel):
    """A deterministic text-to-speech model that yields configured PCM byte chunks."""

    def __init__(
        self,
        results: Iterable[TTSResultItem] = (),
        *,
        model_name: str = "scripted-tts",
    ) -> None:
        self._results = [self._coerce_result(result) for result in results]
        self._model_name = model_name
        self._calls: list[TTSCall] = []

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def calls(self) -> tuple[TTSCall, ...]:
        """Return detached snapshots of recorded text-to-speech calls."""
        return tuple(_snapshot_tts_call(call) for call in self._calls)

    async def run(self, text: str, settings: TTSModelSettings) -> AsyncIterator[bytes]:
        call = _snapshot_tts_call(TTSCall(text=text, settings=settings))
        self._calls.append(call)
        if not self._results:
            raise UnexpectedVoiceCall(
                "Unexpected TTS call: no scripted results remain.",
                operation="tts",
            )
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        for chunk in result.chunks:
            yield chunk

    def assert_complete(self) -> None:
        """Raise when configured TTS results remain unconsumed."""
        if self._results:
            raise UnconsumedVoiceSteps(
                f"{len(self._results)} scripted TTS result(s) were not consumed.",
                remaining_steps=len(self._results),
            )

    @staticmethod
    def _coerce_result(result: TTSResultItem) -> TTSResult | Exception:
        if isinstance(result, Exception):
            return result
        if isinstance(result, TTSResult):
            return TTSResult(chunks=tuple(result.chunks))
        return TTSResult(chunks=tuple(result))


class ScriptedVoiceWorkflow(VoiceWorkflowBase):
    """A deterministic Voice workflow that yields configured text fragments per turn."""

    def __init__(
        self,
        turns: str | Iterable[WorkflowResult] = (),
        *,
        start: str | Sequence[str] | Exception = _START_NOT_CONFIGURED,
    ) -> None:
        configured_turns = [turns] if isinstance(turns, str) else turns
        self._turns = [
            turn if isinstance(turn, Exception) else _normalize_fragments(turn)
            for turn in configured_turns
        ]
        self._start = (
            ()
            if start is _START_NOT_CONFIGURED
            else start
            if isinstance(start, Exception)
            else _normalize_fragments(start)
        )
        self._start_configured = start is not _START_NOT_CONFIGURED
        self._start_pending = self._start_configured
        self._transcriptions: list[str] = []

    @property
    def transcriptions(self) -> tuple[str, ...]:
        """Return the recorded workflow transcriptions."""
        return tuple(self._transcriptions)

    async def on_start(self) -> AsyncIterator[str]:
        if self._start_configured and not self._start_pending:
            raise UnexpectedVoiceCall(
                "Unexpected workflow startup call: no scripted startup step remains.",
                operation="workflow_start",
            )
        self._start_pending = False
        if isinstance(self._start, Exception):
            raise self._start
        for fragment in self._start:
            yield fragment

    async def run(self, transcription: str) -> AsyncIterator[str]:
        self._transcriptions.append(transcription)
        if not self._turns:
            raise UnexpectedVoiceCall(
                "Unexpected workflow turn: no scripted turns remain.",
                operation="workflow_turn",
            )
        result = self._turns.pop(0)
        if isinstance(result, Exception):
            raise result
        for fragment in result:
            yield fragment

    def assert_complete(self) -> None:
        """Raise when the startup step or configured workflow turns remain unconsumed."""
        remaining = int(self._start_pending) + len(self._turns)
        if not remaining:
            return
        if self._start_pending and not self._turns:
            raise UnconsumedVoiceSteps(
                "1 scripted workflow startup step was not consumed.",
                remaining_steps=1,
            )
        if not self._start_pending:
            raise UnconsumedVoiceSteps(
                f"{len(self._turns)} scripted workflow turn(s) were not consumed.",
                remaining_steps=len(self._turns),
            )
        raise UnconsumedVoiceSteps(
            f"{remaining} scripted workflow step(s) were not consumed.",
            remaining_steps=remaining,
        )


def _normalize_fragments(fragments: str | Sequence[str]) -> tuple[str, ...]:
    return (fragments,) if isinstance(fragments, str) else tuple(fragments)


def pcm16_samples(samples: Iterable[int]) -> bytes:
    """Encode integer samples as native little-endian PCM16 bytes."""
    return np.asarray(list(samples), dtype="<i2").tobytes()


__all__ = [
    "STTCall",
    "STTSessionCall",
    "ScriptedSTTModel",
    "ScriptedTTSModel",
    "ScriptedTranscriptionSession",
    "ScriptedVoiceWorkflow",
    "TTSCall",
    "TTSResult",
    "UnconsumedVoiceSteps",
    "UnexpectedVoiceCall",
    "VoiceScriptError",
    "pcm16_samples",
]
