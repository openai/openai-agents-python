from __future__ import annotations

from collections.abc import Mapping

from openai.types.realtime.realtime_audio_formats import AudioPCMA, AudioPCMU

from .config import RealtimeAudioFormat

PCM16_SAMPLE_RATE_HZ = 24_000
PCM16_SAMPLE_WIDTH_BYTES = 2
G711_SAMPLE_RATE_HZ = 8_000

# Every spelling of the two G.711 families accepted across the SDK: the legacy session-config
# names, the GA wire-format names, and their bare suffixes. Kept in sync with the session
# normalizer in `audio_formats.to_realtime_audio_format`.
_G711_FORMAT_NAMES = frozenset(
    {"g711_ulaw", "g711_alaw", "audio/pcmu", "audio/pcma", "pcmu", "pcma"}
)


def _is_g711_format(format: RealtimeAudioFormat | None) -> bool:
    """Whether the format is G.711 (8 kHz, one byte per sample) in any of its spellings.

    The format may arrive as a legacy string (``"g711_ulaw"``), a GA wire-format mapping
    (``{"type": "audio/pcmu"}``), or a typed ``AudioPCMU`` / ``AudioPCMA`` object, depending on
    how the session was configured and which path delivered it.
    """
    if isinstance(format, str):
        return format.lower() in _G711_FORMAT_NAMES or format.lower().startswith("g711")
    if isinstance(format, AudioPCMU | AudioPCMA):
        return True
    if isinstance(format, Mapping):
        format_type = format.get("type")
        return isinstance(format_type, str) and format_type.lower() in _G711_FORMAT_NAMES
    format_type = getattr(format, "type", None)
    return isinstance(format_type, str) and format_type.lower() in _G711_FORMAT_NAMES


def calculate_audio_length_ms(format: RealtimeAudioFormat | None, audio_bytes: bytes) -> float:
    if not audio_bytes:
        return 0.0

    if _is_g711_format(format):
        return (len(audio_bytes) / G711_SAMPLE_RATE_HZ) * 1000

    # PCM16 at 24 kHz, which also serves as the fallback for unknown formats.
    samples = len(audio_bytes) / PCM16_SAMPLE_WIDTH_BYTES
    return (samples / PCM16_SAMPLE_RATE_HZ) * 1000
