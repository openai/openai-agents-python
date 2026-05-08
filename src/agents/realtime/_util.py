from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .config import RealtimeAudioFormat

PCM16_SAMPLE_RATE_HZ = 24_000
PCM16_SAMPLE_WIDTH_BYTES = 2
G711_SAMPLE_RATE_HZ = 8_000


def _normalize_format_to_str(format: RealtimeAudioFormat | None) -> str | None:
    """Extract a lower-cased format identifier from any RealtimeAudioFormat shape.

    `RealtimeAudioFormat` may be a string, a Mapping with a ``type`` key, or one of
    the typed ``AudioPCM`` / ``AudioPCMU`` / ``AudioPCMA`` pydantic models. The
    length calculator previously only handled strings, which silently fell back to
    PCM math for typed/Mapping g711 formats and yielded a ~6x wrong duration.
    """
    if format is None:
        return None
    if isinstance(format, str):
        return format.lower()
    type_value: Any
    if isinstance(format, Mapping):
        type_value = format.get("type")
    else:
        type_value = getattr(format, "type", None)
    return type_value.lower() if isinstance(type_value, str) else None


def calculate_audio_length_ms(format: RealtimeAudioFormat | None, audio_bytes: bytes) -> float:
    if not audio_bytes:
        return 0.0

    normalized_format = _normalize_format_to_str(format)

    if normalized_format and (
        normalized_format.startswith("g711") or normalized_format in ("audio/pcmu", "audio/pcma")
    ):
        return (len(audio_bytes) / G711_SAMPLE_RATE_HZ) * 1000

    samples = len(audio_bytes) / PCM16_SAMPLE_WIDTH_BYTES
    return (samples / PCM16_SAMPLE_RATE_HZ) * 1000
