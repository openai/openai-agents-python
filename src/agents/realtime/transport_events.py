from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Union

from typing_extensions import TypeAlias

from .items import RealtimeItem

RealtimeConnectionStatus: TypeAlias = Literal["connecting", "connected", "disconnected"]


@dataclass
class RealtimeTransportErrorEvent:
    """Represents a transport‑layer error."""

    error: Any

    type: Literal["error"] = "error"


@dataclass
class RealtimeTransportToolCallEvent:
    """Model attempted a tool/function call."""

    name: str
    call_id: str
    arguments: str

    id: str | None = None
    previous_item_id: str | None = None

    type: Literal["function_call"] = "function_call"


@dataclass
class RealtimeTransportAudioEvent:
    """Raw audio bytes emitted by the model."""

    data: bytes
    response_id: str

    type: Literal["audio"] = "audio"


@dataclass
class RealtimeTransportAudioInterruptedEvent:
    """Audio interrupted."""

    type: Literal["audio_interrupted"] = "audio_interrupted"


@dataclass
class RealtimeTransportAudioDoneEvent:
    """Audio done."""

    type: Literal["audio_done"] = "audio_done"


@dataclass
class RealtimeTransportInputAudioTranscriptionCompletedEvent:
    """Input audio transcription completed."""

    item_id: str
    transcript: str

    type: Literal["conversation.item.input_audio_transcription.completed"] = (
        "conversation.item.input_audio_transcription.completed"
    )


@dataclass
class RealtimeTransportTranscriptDelta:
    """Partial transcript update."""

    item_id: str
    delta: str
    response_id: str

    type: Literal["transcript_delta"] = "transcript_delta"


@dataclass
class RealtimeTransportItemUpdatedEvent:
    """Item added to the history or updated."""

    item: RealtimeItem

    type: Literal["item_updated"] = "item_updated"


@dataclass
class RealtimeTransportItemDeletedEvent:
    """Item deleted from the history."""

    item_id: str

    type: Literal["item_deleted"] = "item_deleted"


@dataclass
class RealtimeTransportConnectionStatusEvent:
    """Connection status changed."""

    status: RealtimeConnectionStatus

    type: Literal["connection_status"] = "connection_status"


@dataclass
class RealtimeTransportTurnStartedEvent:
    """Triggered when the model starts generating a response for a turn."""

    type: Literal["turn_started"] = "turn_started"


@dataclass
class RealtimeTransportTurnEndedEvent:
    """Triggered when the model finishes generating a response for a turn."""

    type: Literal["turn_ended"] = "turn_ended"


@dataclass
class RealtimeTransportOtherEvent:
    """Used as a catchall for vendor-specific events."""

    data: Any

    type: Literal["other"] = "other"


# TODO (rm) Add usage events


RealtimeTransportEvent: TypeAlias = Union[
    RealtimeTransportErrorEvent,
    RealtimeTransportToolCallEvent,
    RealtimeTransportAudioEvent,
    RealtimeTransportAudioInterruptedEvent,
    RealtimeTransportAudioDoneEvent,
    RealtimeTransportInputAudioTranscriptionCompletedEvent,
    RealtimeTransportTranscriptDelta,
    RealtimeTransportItemUpdatedEvent,
    RealtimeTransportItemDeletedEvent,
    RealtimeTransportConnectionStatusEvent,
    RealtimeTransportTurnStartedEvent,
    RealtimeTransportTurnEndedEvent,
    RealtimeTransportOtherEvent,
]
