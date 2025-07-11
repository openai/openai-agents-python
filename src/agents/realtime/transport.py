import abc
from typing import Any, Literal, Union

from typing_extensions import NotRequired, TypeAlias, TypedDict

from .config import APIKeyOrKeyFunc, RealtimeClientMessage, RealtimeSessionConfig, RealtimeUserInput
from .transport_events import RealtimeTransportEvent, RealtimeTransportToolCallEvent

RealtimeModelName: TypeAlias = Union[
    Literal[
        "gpt-4o-realtime-preview",
        "gpt-4o-mini-realtime-preview",
        "gpt-4o-realtime-preview-2025-06-03",
        "gpt-4o-realtime-preview-2024-12-17",
        "gpt-4o-realtime-preview-2024-10-01",
        "gpt-4o-mini-realtime-preview-2024-12-17",
    ],
    str,
]
"""The name of a realtime model."""


class RealtimeTransportListener(abc.ABC):
    """A listener for realtime transport events."""

    @abc.abstractmethod
    async def on_event(self, event: RealtimeTransportEvent) -> None:
        """Called when an event is emitted by the realtime transport."""
        pass


class RealtimeTransportConnectionOptions(TypedDict):
    """Options for connecting to a realtime transport."""

    api_key: NotRequired[APIKeyOrKeyFunc]
    """The API key to use for the transport. If unset, the transport will attempt to use the
    `OPENAI_API_KEY` environment variable.
    """

    model: NotRequired[str]
    """The model to use."""

    url: NotRequired[str]
    """The URL to use for the transport. If unset, the transport will use the default OpenAI
    WebSocket URL.
    """

    initial_session_config: NotRequired[RealtimeSessionConfig]


class RealtimeSessionTransport(abc.ABC):
    """A transport layer for realtime sessions."""

    @abc.abstractmethod
    async def connect(self, options: RealtimeTransportConnectionOptions) -> None:
        """Establish a connection to the model and keep it alive."""
        pass

    @abc.abstractmethod
    def add_listener(self, listener: RealtimeTransportListener) -> None:
        """Add a listener to the transport."""
        pass

    @abc.abstractmethod
    async def remove_listener(self, listener: RealtimeTransportListener) -> None:
        """Remove a listener from the transport."""
        pass

    @abc.abstractmethod
    async def send_event(self, event: RealtimeClientMessage) -> None:
        """Send an event to the model."""
        pass

    @abc.abstractmethod
    async def send_message(
        self, message: RealtimeUserInput, other_event_data: dict[str, Any] | None = None
    ) -> None:
        """Send a message to the model."""
        pass

    @abc.abstractmethod
    async def send_audio(self, audio: bytes, *, commit: bool = False) -> None:
        """Send a raw audio chunk to the model.

        Args:
            audio: The audio data to send.
            commit: Whether to commit the audio buffer to the model.  If the model does not do turn
            detection, this can be used to indicate the turn is completed.
        """
        pass

    @abc.abstractmethod
    async def send_tool_output(
        self, tool_call: RealtimeTransportToolCallEvent, output: str, start_response: bool
    ) -> None:
        """Send tool output to the model."""
        pass

    @abc.abstractmethod
    async def interrupt(self) -> None:
        """Interrupt the model. For example, could be triggered by a guardrail."""
        pass

    @abc.abstractmethod
    async def close(self) -> None:
        """Close the session."""
        pass
