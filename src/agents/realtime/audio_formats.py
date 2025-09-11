from __future__ import annotations

from openai.types.realtime.realtime_audio_formats import (
    AudioPCM,
    AudioPCMA,
    AudioPCMU,
    RealtimeAudioFormats,
)

from ..logger import logger


def to_realtime_audio_format(
    input_audio_format: str | RealtimeAudioFormats | None,
) -> RealtimeAudioFormats | None:
    format: RealtimeAudioFormats | None = None
    if input_audio_format is not None:
        if isinstance(input_audio_format, str):
            if input_audio_format in ["pcm16", "audio/pcm", "pcm"]:
                format = AudioPCM(type="audio/pcm", rate=24000)
            elif input_audio_format in ["g711_ulaw", "audio/pcmu", "pcmu"]:
                format = AudioPCMU(type="audio/pcmu")
            elif input_audio_format in ["g711_alaw", "audio/pcma", "pcma"]:
                format = AudioPCMA(type="audio/pcma")
            else:
                logger.debug(f"Unknown input_audio_format: {input_audio_format}")
        else:
            format = input_audio_format
    return format
