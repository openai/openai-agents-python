from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Union

from typing_extensions import TypeAlias

from .items import RealtimeItem

RealtimeConnectionStatus: TypeAlias = Literal["connecting", "connected", "disconnected"]


@dataclass
class RealtimeModelErrorEvent:
    """Represents a transport‑layer error."""

    error: Any

    type: Literal["error"] = "error"


@dataclass
class RealtimeModelToolCallEvent:
    """Event emitted when a model attempts to call a tool/function in realtime.
    
    This event is generated during model streaming when the model decides
    to use a tool. It contains all necessary information to execute the
    tool call and track its lifecycle in the realtime session.

    Attributes:
        name: Name of the tool/function being called
        call_id: Unique identifier for this specific tool call
        arguments: JSON-formatted string containing the tool arguments
        id: Optional unique identifier for this event
        previous_item_id: Optional ID of the item that led to this tool call
    """

    name: str
    call_id: str
    arguments: str

    id: str | None = None
    previous_item_id: str | None = None

    type: Literal["function_call"] = "function_call"


@dataclass
class RealtimeModelAudioEvent:
    """Event containing streaming audio data from a model's response.
    
    This event is emitted when a model produces audio output during
    a realtime session, typically as part of a text-to-speech or
    voice response feature.

    Attributes:
        data: Raw audio bytes from the model
        response_id: Identifier linking this audio to a specific model response
        item_id: ID of the realtime item containing this audio content
        content_index: Position of this audio chunk in the item's content array
        type: Discriminator field identifying this as an audio event
    """

    data: bytes
    response_id: str

    item_id: str
    """The ID of the item containing audio."""

    content_index: int
    """The index of the audio content in `item.content`"""

    type: Literal["audio"] = "audio"


@dataclass
class RealtimeModelAudioInterruptedEvent:
    """Audio interrupted."""

    item_id: str
    """The ID of the item containing audio."""

    content_index: int
    """The index of the audio content in `item.content`"""

    type: Literal["audio_interrupted"] = "audio_interrupted"


@dataclass
class RealtimeModelAudioDoneEvent:
    """Audio done."""

    item_id: str
    """The ID of the item containing audio."""

    content_index: int
    """The index of the audio content in `item.content`"""

    type: Literal["audio_done"] = "audio_done"


@dataclass
class RealtimeModelInputAudioTranscriptionCompletedEvent:
    """Input audio transcription completed."""

    item_id: str
    transcript: str

    type: Literal["input_audio_transcription_completed"] = "input_audio_transcription_completed"


@dataclass
class RealtimeModelInputAudioTimeoutTriggeredEvent:
    """Input audio timeout triggered."""

    item_id: str
    audio_start_ms: int
    audio_end_ms: int

    type: Literal["input_audio_timeout_triggered"] = "input_audio_timeout_triggered"


@dataclass
class RealtimeModelTranscriptDeltaEvent:
    """Partial transcript update."""

    item_id: str
    delta: str
    response_id: str

    type: Literal["transcript_delta"] = "transcript_delta"


@dataclass
class RealtimeModelItemUpdatedEvent:
    """Item added to the history or updated."""

    item: RealtimeItem

    type: Literal["item_updated"] = "item_updated"


@dataclass
class RealtimeModelItemDeletedEvent:
    """Item deleted from the history."""

    item_id: str

    type: Literal["item_deleted"] = "item_deleted"


@dataclass
class RealtimeModelConnectionStatusEvent:
    """Connection status changed."""

    status: RealtimeConnectionStatus

    type: Literal["connection_status"] = "connection_status"


@dataclass
class RealtimeModelTurnStartedEvent:
    """Triggered when the model starts generating a response for a turn."""

    type: Literal["turn_started"] = "turn_started"


@dataclass
class RealtimeModelTurnEndedEvent:
    """Triggered when the model finishes generating a response for a turn."""

    type: Literal["turn_ended"] = "turn_ended"


@dataclass
class RealtimeModelOtherEvent:
    """Used as a catchall for vendor-specific events."""

    data: Any

    type: Literal["other"] = "other"


@dataclass
class RealtimeModelExceptionEvent:
    """Exception occurred during model operation."""

    exception: Exception
    context: str | None = None

    type: Literal["exception"] = "exception"


@dataclass
class RealtimeModelRawServerEvent:
    """Raw events forwarded from the server."""

    data: Any

    type: Literal["raw_server_event"] = "raw_server_event"


# TODO (rm) Add usage events


RealtimeModelEvent: TypeAlias = Union[
    RealtimeModelErrorEvent,
    RealtimeModelToolCallEvent,
    RealtimeModelAudioEvent,
    RealtimeModelAudioInterruptedEvent,
    RealtimeModelAudioDoneEvent,
    RealtimeModelInputAudioTimeoutTriggeredEvent,
    RealtimeModelInputAudioTranscriptionCompletedEvent,
    RealtimeModelTranscriptDeltaEvent,
    RealtimeModelItemUpdatedEvent,
    RealtimeModelItemDeletedEvent,
    RealtimeModelConnectionStatusEvent,
    RealtimeModelTurnStartedEvent,
    RealtimeModelTurnEndedEvent,
    RealtimeModelOtherEvent,
    RealtimeModelExceptionEvent,
    RealtimeModelRawServerEvent,
]
