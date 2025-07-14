from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Union

from typing_extensions import NotRequired, TypeAlias, TypedDict

from .config import RealtimeSessionModelSettings
from .model_events import RealtimeModelToolCallEvent


class RealtimeModelRawClientMessage(TypedDict):
    """A raw message to be sent to the model."""

    type: str  # explicitly required
    other_data: NotRequired[dict[str, Any]]
    """Merged into the message body."""


class RealtimeModelInputTextContent(TypedDict):
    """A piece of text to be sent to the model."""

    type: Literal["input_text"]
    text: str


class RealtimeModelUserInputMessage(TypedDict):
    """A message to be sent to the model."""

    type: Literal["message"]
    role: Literal["user"]
    content: list[RealtimeModelInputTextContent]


RealtimeModelUserInput: TypeAlias = Union[str, RealtimeModelUserInputMessage]
"""A user input to be sent to the model."""


# Model messages


@dataclass
class RealtimeModelSendRawMessage:
    """Send a raw message to the model."""

    message: RealtimeModelRawClientMessage
    """The message to send."""


@dataclass
class RealtimeModelSendUserInput:
    """Send a user input to the model."""

    user_input: RealtimeModelUserInput
    """The user input to send."""


@dataclass
class RealtimeModelSendAudio:
    """Send audio to the model."""

    audio: bytes
    commit: bool = False


@dataclass
class RealtimeModelSendToolOutput:
    """Send tool output to the model."""

    tool_call: RealtimeModelToolCallEvent
    """The tool call to send."""

    output: str
    """The output to send."""

    start_response: bool
    """Whether to start a response."""


@dataclass
class RealtimeModelSendInterrupt:
    """Send an interrupt to the model."""


@dataclass
class RealtimeModelSendSessionUpdate:
    """Send a session update to the model."""

    session_settings: RealtimeSessionModelSettings
    """The updated session settings to send."""


RealtimeModelSendEvent: TypeAlias = Union[
    RealtimeModelSendRawMessage,
    RealtimeModelSendUserInput,
    RealtimeModelSendAudio,
    RealtimeModelSendToolOutput,
    RealtimeModelSendInterrupt,
    RealtimeModelSendSessionUpdate,
]
