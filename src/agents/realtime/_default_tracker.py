from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ._util import calculate_audio_length_ms
from .config import RealtimeAudioFormat


@dataclass
class ModelAudioState:
    initial_received_time: datetime
    audio_length_ms: float


class ModelAudioTracker:
    def __init__(self) -> None:
        # (item_id, item_content_index) -> ModelAudioState
        self._states: dict[tuple[str, int], ModelAudioState] = {}
        self._last_audio_item: tuple[str, int] | None = None

    def set_audio_format(self, format: RealtimeAudioFormat) -> None:
        """Called when the model wants to set the audio format."""
        self._format = format

    def on_audio_delta(self, item_id: str, item_content_index: int, audio_bytes: bytes) -> None:
        """Called when an audio delta is received from the model."""
        ms = calculate_audio_length_ms(self._format, audio_bytes)
        new_key = (item_id, item_content_index)

        self._last_audio_item = new_key
        if new_key not in self._states:
            self._states[new_key] = ModelAudioState(datetime.now(), ms)
        else:
            self._states[new_key].audio_length_ms += ms

    def on_interrupted(self) -> None:
        """Called when the audio playback has been interrupted."""
        self._last_audio_item = None

    def get_state(self, item_id: str, item_content_index: int) -> ModelAudioState | None:
        """Called when the model wants to get the current playback state."""
        return self._states.get((item_id, item_content_index))

    def get_last_audio_item(self) -> tuple[str, int] | None:
        """Called when the model wants to get the last audio item ID and content index."""
        return self._last_audio_item
