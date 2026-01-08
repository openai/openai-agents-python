from __future__ import annotations

from .config import RealtimeAudioFormat

PCM16_SAMPLE_RATE_HZ = 24_000
PCM16_SAMPLE_WIDTH_BYTES = 2
G711_SAMPLE_RATE_HZ = 8_000


def calculate_audio_length_ms(format: RealtimeAudioFormat | None, audio_bytes: bytes) -> float:
    if not audio_bytes:
        return 0.0

    normalized_format = format.lower() if isinstance(format, str) else None

    if normalized_format and normalized_format.startswith("g711"):
        return (len(audio_bytes) / G711_SAMPLE_RATE_HZ) * 1000

    samples = len(audio_bytes) / PCM16_SAMPLE_WIDTH_BYTES
    return (samples / PCM16_SAMPLE_RATE_HZ) * 1000
