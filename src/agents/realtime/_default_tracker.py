from __future__ import annotations

import time
from dataclasses import dataclass

from ._util import calculate_audio_length_ms
from .config import RealtimeAudioFormat


@dataclass
class ModelAudioState:
    initial_received_time: float
    audio_length_ms: float
    response_id: str | None = None


@dataclass
class ModelResponseAudioState:
    items: list[tuple[str, int]]


class ModelAudioTracker:
    def __init__(self) -> None:
        # (item_id, item_content_index) -> ModelAudioState
        self._states: dict[tuple[str, int], ModelAudioState] = {}
        self._last_audio_item: tuple[str, int] | None = None
        self._audio_items_by_response_id: dict[str, ModelResponseAudioState] = {}
        # Format is set once the session payload negotiates one. Audio deltas can
        # arrive before that for transcription-only sessions or when the payload
        # omits an audio format, so we default to None and let the length
        # calculator handle the unknown-format fallback.
        self._format: RealtimeAudioFormat | None = None

    def set_audio_format(self, format: RealtimeAudioFormat) -> None:
        """Called when the model wants to set the audio format."""
        self._format = format

    def on_audio_delta(
        self,
        item_id: str,
        item_content_index: int,
        audio_bytes: bytes,
        response_id: str | None = None,
    ) -> None:
        """Called when an audio delta is received from the model."""
        ms = calculate_audio_length_ms(self._format, audio_bytes)
        new_key = (item_id, item_content_index)

        self._last_audio_item = new_key
        if response_id is not None:
            response_state = self._audio_items_by_response_id.setdefault(
                response_id,
                ModelResponseAudioState(items=[]),
            )
            if new_key not in response_state.items:
                response_state.items.append(new_key)
        if new_key not in self._states:
            self._states[new_key] = ModelAudioState(time.monotonic(), ms, response_id)
        else:
            self._states[new_key].audio_length_ms += ms
            if response_id is not None:
                self._states[new_key].response_id = response_id

    def on_interrupted(self) -> None:
        """Called when the audio playback has been interrupted."""
        if self._last_audio_item is not None:
            state = self._states.get(self._last_audio_item)
            if state is not None and state.response_id is not None:
                self._audio_items_by_response_id.pop(state.response_id, None)
        self._last_audio_item = None

    def on_response_interrupted(self, response_id: str) -> None:
        """Called when buffered audio for a specific response has been interrupted."""
        interrupted_state = self._audio_items_by_response_id.pop(response_id, None)
        if interrupted_state is not None and self._last_audio_item in interrupted_state.items:
            self._last_audio_item = None

    def on_response_done(self, response_id: str) -> None:
        """Release response-scoped indexes after guardrails settle."""
        self._audio_items_by_response_id.pop(response_id, None)

    def clear_response_indexes(self) -> None:
        """Release every response-scoped index when the model connection closes."""
        self._audio_items_by_response_id.clear()

    def get_state(self, item_id: str, item_content_index: int) -> ModelAudioState | None:
        """Called when the model wants to get the current playback state."""
        return self._states.get((item_id, item_content_index))

    def get_last_audio_item(self) -> tuple[str, int] | None:
        """Called when the model wants to get the last audio item ID and content index."""
        return self._last_audio_item

    def get_audio_items_for_response(self, response_id: str) -> tuple[tuple[str, int], ...]:
        """Return every audio item received for a response in arrival order."""
        response_state = self._audio_items_by_response_id.get(response_id)
        return tuple(response_state.items) if response_state is not None else ()
