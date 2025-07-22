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
    """Model attempted a tool/function call."""

    name: str
    call_id: str
    arguments: str

    id: str | None = None
    previous_item_id: str | None = None

    type: Literal["function_call"] = "function_call"


@dataclass
class RealtimeModelAudioEvent:
    """Raw audio bytes emitted by the model."""

    data: bytes
    response_id: str

    type: Literal["audio"] = "audio"


@dataclass
class RealtimeModelAudioInterruptedEvent:
    """Audio interrupted."""

    type: Literal["audio_interrupted"] = "audio_interrupted"


@dataclass
class RealtimeModelAudioDoneEvent:
    """Audio done."""

    type: Literal["audio_done"] = "audio_done"


@dataclass
class RealtimeModelInputAudioTranscriptionCompletedEvent:
    """Input audio transcription completed."""

    item_id: str
    transcript: str

    type: Literal["input_audio_transcription_completed"] = "input_audio_transcription_completed"


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


# TODO (rm) Add usage events


RealtimeModelEvent: TypeAlias = Union[
    RealtimeModelErrorEvent,
    RealtimeModelToolCallEvent,
    RealtimeModelAudioEvent,
    RealtimeModelAudioInterruptedEvent,
    RealtimeModelAudioDoneEvent,
    RealtimeModelInputAudioTranscriptionCompletedEvent,
    RealtimeModelTranscriptDeltaEvent,
    RealtimeModelItemUpdatedEvent,
    RealtimeModelItemDeletedEvent,
    RealtimeModelConnectionStatusEvent,
    RealtimeModelTurnStartedEvent,
    RealtimeModelTurnEndedEvent,
    RealtimeModelOtherEvent,
    RealtimeModelExceptionEvent,
]
