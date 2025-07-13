from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Callable, Literal

from .imports import np, npt
from .input import AudioInput, StreamedAudioInput
from .utils import get_sentence_based_splitter

DEFAULT_TTS_INSTRUCTIONS = (
    "You will receive partial sentences. Do not complete the sentence, just read out the text."
)
DEFAULT_TTS_BUFFER_SIZE = 120

TTSVoice = Literal["alloy", "ash", "coral", "echo", "fable", "onyx", "nova", "sage", "shimmer"]
"""Exportable type for the TTSModelSettings voice enum"""


@dataclass
class TTSModelSettings:
    """Settings for a TTS model."""

    voice: TTSVoice | None = None
    """
    The voice to use for the TTS model. If not provided, the default voice for the respective model
    will be used.
    """

    buffer_size: int = 120
    """The minimal size of the chunks of audio data that are being streamed out."""

    dtype: npt.DTypeLike = np.int16
    """The data type for the audio data to be returned in."""

    transform_data: (
        Callable[[npt.NDArray[np.int16 | np.float32]], npt.NDArray[np.int16 | np.float32]] | None
    ) = None
    """
    A function to transform the data from the TTS model. This is useful if you want the resulting
    audio stream to have the data in a specific shape already.
    """

    instructions: str = (
        "You will receive partial sentences. Do not complete the sentence just read out the text."
    )
    """
    The instructions to use for the TTS model. This is useful if you want to control the tone of the
    audio output.
    """

    text_splitter: Callable[[str], tuple[str, str]] = get_sentence_based_splitter()
    """
    A function to split the text into chunks. This is useful if you want to split the text into
    chunks before sending it to the TTS model rather than waiting for the whole text to be
    processed.
    """

    speed: float | None = None
    """The speed with which the TTS model will read the text. Between 0.25 and 4.0."""


class TTSModel(abc.ABC):
    """A text-to-speech model that can convert text into audio output."""

    @property
    @abc.abstractmethod
    def model_name(self) -> str:
        """The name of the TTS model."""
        pass

    @abc.abstractmethod
    def run(self, text: str, settings: TTSModelSettings) -> AsyncIterator[bytes]:
        """Given a text string, produces a stream of audio bytes, in PCM format.

        Args:
            text: The text to convert to audio.

        Returns:
            An async iterator of audio bytes, in PCM format.
        """
        pass


class StreamedTranscriptionSession(abc.ABC):
    """A streamed transcription of audio input."""

    @abc.abstractmethod
    def transcribe_turns(self) -> AsyncIterator[str]:
        """Yields a stream of text transcriptions. Each transcription is a turn in the conversation.

        This method is expected to return only after `close()` is called.
        """
        pass

    @abc.abstractmethod
    async def close(self) -> None:
        """Closes the session."""
        pass


@dataclass
class STTModelSettings:
    """Settings for a speech-to-text model."""

    prompt: str | None = None
    """Instructions for the model to follow."""

    language: str | None = None
    """The language of the audio input."""

    temperature: float | None = None
    """The temperature of the model."""

    turn_detection: dict[str, Any] | None = None
    """The turn detection settings for the model when using streamed audio input."""


class STTModel(abc.ABC):
    """A speech-to-text model that can convert audio input into text."""

    @property
    @abc.abstractmethod
    def model_name(self) -> str:
        """The name of the STT model."""
        pass

    @abc.abstractmethod
    async def transcribe(
        self,
        input: AudioInput,
        settings: STTModelSettings,
        trace_include_sensitive_data: bool,
        trace_include_sensitive_audio_data: bool,
    ) -> str:
        """Given an audio input, produces a text transcription.

        Args:
            input: The audio input to transcribe.
            settings: The settings to use for the transcription.
            trace_include_sensitive_data: Whether to include sensitive data in traces.
            trace_include_sensitive_audio_data: Whether to include sensitive audio data in traces.

        Returns:
            The text transcription of the audio input.
        """
        pass

    @abc.abstractmethod
    async def create_session(
        self,
        input: StreamedAudioInput,
        settings: STTModelSettings,
        trace_include_sensitive_data: bool,
        trace_include_sensitive_audio_data: bool,
    ) -> StreamedTranscriptionSession:
        """Creates a new transcription session, which you can push audio to, and receive a stream
        of text transcriptions.

        Args:
            input: The audio input to transcribe.
            settings: The settings to use for the transcription.
            trace_include_sensitive_data: Whether to include sensitive data in traces.
            trace_include_sensitive_audio_data: Whether to include sensitive audio data in traces.

        Returns:
            A new transcription session.
        """
        pass


class VoiceModelProvider(abc.ABC):
    """The base interface for a voice model provider.

    A model provider is responsible for creating speech-to-text and text-to-speech models, given a
    name.
    """

    @abc.abstractmethod
    def get_stt_model(self, model_name: str | None) -> STTModel:
        """Get a speech-to-text model by name.

        Args:
            model_name: The name of the model to get.

        Returns:
            The speech-to-text model.
        """
        pass

    @abc.abstractmethod
    def get_tts_model(self, model_name: str | None) -> TTSModel:
        """Get a text-to-speech model by name."""
