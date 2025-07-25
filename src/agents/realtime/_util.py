from .config import RealtimeAudioFormat


def calculate_audio_length_ms(format: RealtimeAudioFormat | None, bytes: bytes) -> float:
    if format and format.startswith("g711"):
        return (len(bytes) / 8000) * 1000
    return (len(bytes) / 24 / 2) * 1000
