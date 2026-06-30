from __future__ import annotations

from collections.abc import Mapping
from typing import Any

try:
    from openai.types.realtime.realtime_audio_formats import (
        AudioPCMA,
        AudioPCMU,
    )
except ImportError:  # pragma: no cover - openai package missing the type
    AudioPCMU = None  # type: ignore[assignment,misc]
    AudioPCMA = None  # type: ignore[assignment,misc]

from .config import RealtimeAudioFormat

PCM16_SAMPLE_RATE_HZ = 24_000
PCM16_SAMPLE_WIDTH_BYTES = 2
G711_SAMPLE_RATE_HZ = 8_000


def _is_g711_format(format: RealtimeAudioFormat | None) -> bool:
    """Return True if `format` represents a G.711 audio stream in any shape."""
    if format is None:
        return False
    # Match the typed models first: their generated `type` field is Optional and
    # defaults to None, so a `AudioPCMU()` / `AudioPCMA()` instance has nothing
    # for the string-based check below to inspect.
    if AudioPCMU is not None and isinstance(format, AudioPCMU):
        return True
    if AudioPCMA is not None and isinstance(format, AudioPCMA):
        return True
    if isinstance(format, str):
        text = format.lower()
        return text.startswith("g711") or text in ("audio/pcmu", "audio/pcma")
    type_value: Any
    if isinstance(format, Mapping):
        type_value = format.get("type")
    else:
        type_value = getattr(format, "type", None)
    if not isinstance(type_value, str):
        return False
    text = type_value.lower()
    return text.startswith("g711") or text in ("audio/pcmu", "audio/pcma")


def calculate_audio_length_ms(format: RealtimeAudioFormat | None, audio_bytes: bytes) -> float:
    if not audio_bytes:
        return 0.0

    if _is_g711_format(format):
        return (len(audio_bytes) / G711_SAMPLE_RATE_HZ) * 1000

    samples = len(audio_bytes) / PCM16_SAMPLE_WIDTH_BYTES
    return (samples / PCM16_SAMPLE_RATE_HZ) * 1000
