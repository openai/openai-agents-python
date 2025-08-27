from __future__ import annotations

import abc
from typing import Callable

from typing_extensions import NotRequired, TypedDict

from ..util._types import MaybeAwaitable
from ._util import calculate_audio_length_ms
from .config import (
    RealtimeAudioFormat,
    RealtimeSessionModelSettings,
)
from .model_events import RealtimeModelEvent
from .model_inputs import RealtimeModelSendEvent


class RealtimePlaybackState(TypedDict):
    current_item_id: str | None
    """The item ID of the current item being played."""

    current_item_content_index: int | None
    """The index of the current item content being played."""

    elapsed_ms: float | None
    """The number of milliseconds of audio that have been played."""


class RealtimePlaybackTracker:
    """If you have custom playback logic or expect that audio is played with delays or at different
    speeds, create an instance of RealtimePlaybackTracker and pass it to the session. You are
    responsible for tracking the audio playback progress and calling `on_play_bytes` or
    `on_play_ms` when the user has played some audio."""

    def __init__(self) -> None:
        self._format: RealtimeAudioFormat | None = None
        # (item_id, item_content_index)
        self._current_item: tuple[str, int] | None = None
        self._elapsed_ms: float | None = None

    def on_play_bytes(self, item_id: str, item_content_index: int, bytes: bytes) -> None:
        """Called by you when you have played some audio.

        Args:
            item_id: The item ID of the audio being played.
            item_content_index: The index of the audio content in `item.content`
            bytes: The audio bytes that have been fully played.
        """
        ms = calculate_audio_length_ms(self._format, bytes)
        self.on_play_ms(item_id, item_content_index, ms)

    def on_play_ms(self, item_id: str, item_content_index: int, ms: float) -> None:
        """Called by you when you have played some audio.

        Args:
            item_id: The item ID of the audio being played.
            item_content_index: The index of the audio content in `item.content`
            ms: The number of milliseconds of audio that have been played.
        """
        if self._current_item != (item_id, item_content_index):
            self._current_item = (item_id, item_content_index)
            self._elapsed_ms = ms
        else:
            assert self._elapsed_ms is not None
            self._elapsed_ms += ms

    def on_interrupted(self) -> None:
        """Called by the model when the audio playback has been interrupted."""
        self._current_item = None
        self._elapsed_ms = None

    def set_audio_format(self, format: RealtimeAudioFormat) -> None:
        """Will be called by the model to set the audio format.

        Args:
            format: The audio format to use.
        """
        self._format = format

    def get_state(self) -> RealtimePlaybackState:
        """Will be called by the model to get the current playback state."""
        if self._current_item is None:
            return {
                "current_item_id": None,
                "current_item_content_index": None,
                "elapsed_ms": None,
            }
        assert self._elapsed_ms is not None

        item_id, item_content_index = self._current_item
        return {
            "current_item_id": item_id,
            "current_item_content_index": item_content_index,
            "elapsed_ms": self._elapsed_ms,
        }


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

    playback_tracker: NotRequired[RealtimePlaybackTracker]
    """The playback tracker to use when tracking audio playback progress. If not set, the model will
    use a default implementation that assumes audio is played immediately, at realtime speed.

    A playback tracker is useful for interruptions. The model generates audio much faster than
    realtime playback speed. So if there's an interruption, its useful for the model to know how
    much of the audio has been played by the user. In low-latency scenarios, it's fine to assume
    that audio is played back immediately at realtime speed. But in scenarios like phone calls or
    other remote interactions, you can set a playback tracker that lets the model know when audio
    is played to the user.
    """


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
