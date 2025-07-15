from __future__ import annotations

import abc
from typing import Callable

from typing_extensions import NotRequired, TypedDict

from ..util._types import MaybeAwaitable
from .config import (
    RealtimeSessionModelSettings,
)
from .model_events import RealtimeModelEvent
from .model_inputs import RealtimeModelSendEvent


class RealtimeModelListener(abc.ABC):
    """A listener for realtime transport events."""

    @abc.abstractmethod
    async def on_event(self, event: RealtimeModelEvent) -> None:
        """Called when an event is emitted by the realtime transport."""
        pass


class RealtimeModelConfig(TypedDict):
    """Options for connecting to a realtime model."""

    api_key: NotRequired[str | Callable[[], MaybeAwaitable[str]]]
    """The API key (or function that returns a key) to use when connecting. If unset, the model will
    try to use a sane default. For example, the OpenAI Realtime model will try to use the
    `OPENAI_API_KEY`  environment variable.
    """

    url: NotRequired[str]
    """The URL to use when connecting. If unset, the model will use a sane default. For example,
    the OpenAI Realtime model will use the default OpenAI WebSocket URL.
    """

    initial_model_settings: NotRequired[RealtimeSessionModelSettings]
    """The initial model settings to use when connecting."""


class RealtimeModel(abc.ABC):
    """Interface for connecting to a realtime model and sending/receiving events."""

    @abc.abstractmethod
    async def connect(self, options: RealtimeModelConfig) -> None:
        """Establish a connection to the model and keep it alive."""
        pass

    @abc.abstractmethod
    def add_listener(self, listener: RealtimeModelListener) -> None:
        """Add a listener to the model."""
        pass

    @abc.abstractmethod
    def remove_listener(self, listener: RealtimeModelListener) -> None:
        """Remove a listener from the model."""
        pass

    @abc.abstractmethod
    async def send_event(self, event: RealtimeModelSendEvent) -> None:
        """Send an event to the model."""
        pass

    @abc.abstractmethod
    async def close(self) -> None:
        """Close the session."""
        pass
