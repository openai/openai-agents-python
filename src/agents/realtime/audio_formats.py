from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from openai.types.realtime.realtime_audio_formats import (
    AudioPCM,
    AudioPCMA,
    AudioPCMU,
    RealtimeAudioFormats,
)

from ..logger import logger


def to_realtime_audio_format(
    input_audio_format: str | RealtimeAudioFormats | Mapping[str, Any] | None,
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
        elif isinstance(input_audio_format, Mapping):
            fmt_type = input_audio_format.get("type")
            rate = input_audio_format.get("rate")
            if fmt_type == "audio/pcm":
                pcm_rate: Literal[24000] | None
                if isinstance(rate, (int, float)) and int(rate) == 24000:
                    pcm_rate = 24000
                elif rate is None:
                    pcm_rate = 24000
                else:
                    logger.debug(
                        f"Unknown pcm rate in input_audio_format mapping: {input_audio_format}"
                    )
                    pcm_rate = 24000
                format = AudioPCM(type="audio/pcm", rate=pcm_rate)
            elif fmt_type == "audio/pcmu":
                format = AudioPCMU(type="audio/pcmu")
            elif fmt_type == "audio/pcma":
                format = AudioPCMA(type="audio/pcma")
            else:
                logger.debug(f"Unknown input_audio_format mapping: {input_audio_format}")
        else:
            format = input_audio_format
    return format
