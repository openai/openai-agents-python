from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from ..imports import npt
from ...tool import Tool


@dataclass
class RealtimeEventSessionBegins:
    session_id: str
    type: Literal["session_begins"] = "session_begins"


@dataclass
class RealtimeEventAudioChunk:
    # Assuming audio is received as bytes (e.g., base64 decoded PCM)
    # and will be converted to np.ndarray[np.int16] by the concrete model implementation
    # or by the StreamedRealtimeResult
    data: bytes
    type: Literal["audio_chunk"] = "audio_chunk"


@dataclass
class RealtimeEventTextDelta:
    delta: str
    type: Literal["text_delta"] = "text_delta"


@dataclass
class RealtimeEventToolCall:
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]  # Parsed JSON arguments
    type: Literal["tool_call"] = "tool_call"


@dataclass
class RealtimeEventSessionEnds:
    reason: str | None = None  # Optional reason for session ending
    type: Literal["session_ends"] = "session_ends"


@dataclass
class RealtimeEventError:
    message: str
    code: int | None = None  # Optional error code
    type: Literal["error"] = "error"


@dataclass
class RealtimeEventResponseDone:
    item_id: str | None = None  # The item ID of the response that is done
    type: Literal["response_done"] = "response_done"


@dataclass
class RealtimeEventRateLimitsUpdated:
    data: Any  # The raw data from the rate_limits.updated event
    type: Literal["rate_limits_updated"] = "rate_limits_updated"


@dataclass
class RealtimeEventInputAudioTranscriptionDelta:
    item_id: str
    content_index: int
    delta: str
    type: Literal["input_audio_transcription_delta"] = "input_audio_transcription_delta"


@dataclass
class RealtimeEventInputAudioTranscriptionCompleted:
    item_id: str
    content_index: int
    transcript: str
    type: Literal["input_audio_transcription_completed"] = (
        "input_audio_transcription_completed"
    )


RealtimeEvent = (
    RealtimeEventSessionBegins
    | RealtimeEventAudioChunk
    | RealtimeEventTextDelta
    | RealtimeEventToolCall
    | RealtimeEventSessionEnds
    | RealtimeEventError
    | RealtimeEventResponseDone
    | RealtimeEventRateLimitsUpdated
    | RealtimeEventInputAudioTranscriptionDelta
    | RealtimeEventInputAudioTranscriptionCompleted
)


class RealtimeSession(abc.ABC):
    """Represents an active real-time LLM session."""

    @abc.abstractmethod
    async def send_audio_chunk(self, pcm_audio: npt.NDArray[npt.np.int16]) -> None:
        """Sends a chunk of PCM audio to the real-time LLM.

        Args:
            pcm_audio: A numpy array of int16 audio data.
        """
        pass

    @abc.abstractmethod
    async def send_tool_result(self, tool_call_id: str, content: str) -> None:
        """Sends the result of a tool execution back to the LLM.

        Args:
            tool_call_id: The ID of the tool call this result corresponds to.
            content: The string content of the tool's output (often JSON).
        """
        pass

    @abc.abstractmethod
    def receive_events(self) -> AsyncIterator[RealtimeEvent]:
        """Receives and yields events from the real-time LLM session.

        Returns:
            An async iterator of RealtimeEvent instances.
        """
        # Ensure it's an async iterator
        if False:  # pragma: no cover
            yield

    @abc.abstractmethod
    async def close(self) -> None:
        """Closes the real-time session and any underlying connections."""
        pass


class RealtimeLLMModel(abc.ABC):
    """Abstract base class for real-time Language Model providers."""

    @property
    @abc.abstractmethod
    def model_name(self) -> str:
        """The name of the real-time LLM model (e.g., 'gpt-4o-realtime-preview')."""
        pass

    @abc.abstractmethod
    async def create_session(
        self,
        *,
        tools: Sequence[Tool] = (),
        system_message: str | None = None,
        assistant_voice: str | None = None,
        # Potentially other config like language, output_format, etc.
        # For now, keeping it minimal as per OpenAI docs for gpt4o-realtime
    ) -> RealtimeSession:
        """Creates a new real-time LLM session.

        Args:
            tools: A sequence of Tool instances available during the session.
            system_message: An optional system message to guide the assistant.
            assistant_voice: The voice to be used for the assistant's speech output.
                             (e.g., "alloy", "echo", etc. - specific to the model)

        Returns:
            An instance of RealtimeSession.
        """
        pass
