from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Union

from typing_extensions import TypeAlias

from ..guardrail import OutputGuardrailResult
from ..run_context import RunContextWrapper
from ..tool import Tool
from .agent import RealtimeAgent
from .items import RealtimeItem
from .model_events import RealtimeModelAudioEvent, RealtimeModelEvent


@dataclass
class RealtimeEventInfo:
    context: RunContextWrapper
    """The context for the event."""


@dataclass
class RealtimeAgentStartEvent:
    """A new agent has started."""

    agent: RealtimeAgent
    """The new agent."""

    info: RealtimeEventInfo
    """Common info for all events, such as the context."""

    type: Literal["agent_start"] = "agent_start"


@dataclass
class RealtimeAgentEndEvent:
    """An agent has ended."""

    agent: RealtimeAgent
    """The agent that ended."""

    info: RealtimeEventInfo
    """Common info for all events, such as the context."""

    type: Literal["agent_end"] = "agent_end"


@dataclass
class RealtimeHandoffEvent:
    """An agent has handed off to another agent."""

    from_agent: RealtimeAgent
    """The agent that handed off."""

    to_agent: RealtimeAgent
    """The agent that was handed off to."""

    info: RealtimeEventInfo
    """Common info for all events, such as the context."""

    type: Literal["handoff"] = "handoff"


@dataclass
class RealtimeToolStart:
    """An agent is starting a tool call."""

    agent: RealtimeAgent
    """The agent that updated."""

    tool: Tool

    info: RealtimeEventInfo
    """Common info for all events, such as the context."""

    type: Literal["tool_start"] = "tool_start"


@dataclass
class RealtimeToolEnd:
    """An agent has ended a tool call."""

    agent: RealtimeAgent
    """The agent that ended the tool call."""

    tool: Tool
    """The tool that was called."""

    output: Any
    """The output of the tool call."""

    info: RealtimeEventInfo
    """Common info for all events, such as the context."""

    type: Literal["tool_end"] = "tool_end"


@dataclass
class RealtimeRawModelEvent:
    """Forwards raw events from the model layer."""

    data: RealtimeModelEvent
    """The raw data from the model layer."""

    info: RealtimeEventInfo
    """Common info for all events, such as the context."""

    type: Literal["raw_model_event"] = "raw_model_event"


@dataclass
class RealtimeAudioEnd:
    """Triggered when the agent stops generating audio."""

    info: RealtimeEventInfo
    """Common info for all events, such as the context."""

    item_id: str
    """The ID of the item containing audio."""

    content_index: int
    """The index of the audio content in `item.content`"""

    type: Literal["audio_end"] = "audio_end"


@dataclass
class RealtimeAudio:
    """Triggered when the agent generates new audio to be played."""

    audio: RealtimeModelAudioEvent
    """The audio event from the model layer."""

    item_id: str
    """The ID of the item containing audio."""

    content_index: int
    """The index of the audio content in `item.content`"""

    info: RealtimeEventInfo
    """Common info for all events, such as the context."""

    type: Literal["audio"] = "audio"


@dataclass
class RealtimeAudioInterrupted:
    """Triggered when the agent is interrupted. Can be listened to by the user to stop audio
    playback or give visual indicators to the user.
    """

    info: RealtimeEventInfo
    """Common info for all events, such as the context."""

    item_id: str
    """The ID of the item containing audio."""

    content_index: int
    """The index of the audio content in `item.content`"""

    type: Literal["audio_interrupted"] = "audio_interrupted"


@dataclass
class RealtimeError:
    """An error has occurred."""

    error: Any
    """The error that occurred."""

    info: RealtimeEventInfo
    """Common info for all events, such as the context."""

    type: Literal["error"] = "error"


@dataclass
class RealtimeHistoryUpdated:
    """The history has been updated. Contains the full history of the session."""

    history: list[RealtimeItem]
    """The full history of the session."""

    info: RealtimeEventInfo
    """Common info for all events, such as the context."""

    type: Literal["history_updated"] = "history_updated"


@dataclass
class RealtimeHistoryAdded:
    """A new item has been added to the history."""

    item: RealtimeItem
    """The new item that was added to the history."""

    info: RealtimeEventInfo
    """Common info for all events, such as the context."""

    type: Literal["history_added"] = "history_added"


@dataclass
class RealtimeGuardrailTripped:
    """A guardrail has been tripped and the agent has been interrupted."""

    guardrail_results: list[OutputGuardrailResult]
    """The results from all triggered guardrails."""

    message: str
    """The message that was being generated when the guardrail was triggered."""

    info: RealtimeEventInfo
    """Common info for all events, such as the context."""

    type: Literal["guardrail_tripped"] = "guardrail_tripped"


RealtimeSessionEvent: TypeAlias = Union[
    RealtimeAgentStartEvent,
    RealtimeAgentEndEvent,
    RealtimeHandoffEvent,
    RealtimeToolStart,
    RealtimeToolEnd,
    RealtimeRawModelEvent,
    RealtimeAudioEnd,
    RealtimeAudio,
    RealtimeAudioInterrupted,
    RealtimeError,
    RealtimeHistoryUpdated,
    RealtimeHistoryAdded,
    RealtimeGuardrailTripped,
]
"""An event emitted by the realtime session."""
