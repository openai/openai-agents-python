from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Union, Any

from typing_extensions import TypeAlias

from .imports import np, npt


@dataclass
class VoiceStreamEventAudio:
    """Streaming event from the VoicePipeline"""

    data: npt.NDArray[np.int16 | np.float32] | None
    """The audio data."""

    type: Literal["voice_stream_event_audio"] = "voice_stream_event_audio"
    """The type of event."""


@dataclass
class VoiceStreamEventLifecycle:
    """Streaming event from the VoicePipeline"""

    event: Literal["turn_started", "turn_ended", "session_ended"]
    """The event that occurred."""

    type: Literal["voice_stream_event_lifecycle"] = "voice_stream_event_lifecycle"
    """The type of event."""


@dataclass
class VoiceStreamEventError:
    """Streaming event from the VoicePipeline"""

    error: Exception
    """The error that occurred."""

    type: Literal["voice_stream_event_error"] = "voice_stream_event_error"
    """The type of event."""


@dataclass
class VoiceStreamEventToolCall:
    """Streaming event indicating a tool call from a real-time pipeline."""

    tool_call_id: str
    """The unique ID for this tool call instance."""

    tool_name: str
    """The name of the tool to be called."""

    arguments: dict[str, Any]
    """The arguments for the tool, as a dictionary."""

    type: Literal["voice_stream_event_tool_call"] = "voice_stream_event_tool_call"


VoiceStreamEvent: TypeAlias = Union[
    VoiceStreamEventAudio,
    VoiceStreamEventLifecycle,
    VoiceStreamEventError,
    VoiceStreamEventToolCall,
]
"""An event from the `VoicePipeline`, streamed via `StreamedAudioResult.stream()`."""
