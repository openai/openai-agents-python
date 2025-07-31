from __future__ import annotations

from .config import RealtimeAudioFormat


def calculate_audio_length_ms(format: RealtimeAudioFormat | None, audio_bytes: bytes) -> float:
    if format and format.startswith("g711"):
        return (len(audio_bytes) / 8000) * 1000
    return (len(audio_bytes) / 24 / 2) * 1000
