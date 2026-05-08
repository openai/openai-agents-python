from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .config import RealtimeAudioFormat

PCM16_SAMPLE_RATE_HZ = 24_000
PCM16_SAMPLE_WIDTH_BYTES = 2
G711_SAMPLE_RATE_HZ = 8_000


def _extract_format_type(format: RealtimeAudioFormat | None) -> str | None:
    """Extract the type string from any supported format representation."""
    if format is None:
        return None
    if isinstance(format, str):
        return format.lower()
    if isinstance(format, Mapping):
        type_value = format.get("type")
        return type_value.lower() if isinstance(type_value, str) else None
    # OpenAIRealtimeAudioFormats pydantic models expose a `type` attribute.
    type_attr: Any = getattr(format, "type", None)
    return type_attr.lower() if isinstance(type_attr, str) else None


def calculate_audio_length_ms(format: RealtimeAudioFormat | None, audio_bytes: bytes) -> float:
    if not audio_bytes:
        return 0.0

    normalized_format = _extract_format_type(format)

    if normalized_format and (
        normalized_format.startswith("g711")
        or normalized_format in ("audio/pcmu", "audio/pcma")
    ):
        return (len(audio_bytes) / G711_SAMPLE_RATE_HZ) * 1000

    samples = len(audio_bytes) / PCM16_SAMPLE_WIDTH_BYTES
    return (samples / PCM16_SAMPLE_RATE_HZ) * 1000
