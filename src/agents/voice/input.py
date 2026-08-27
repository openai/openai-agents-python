from __future__ import annotations

import asyncio
import base64
import io
import wave
from dataclasses import dataclass
from typing import cast

from ..exceptions import UserError
from .imports import np, npt

DEFAULT_SAMPLE_RATE = 24000


def _buffer_to_audio_file(
    buffer: npt.NDArray[np.int16 | np.float32 | np.float64],
    frame_rate: int = DEFAULT_SAMPLE_RATE,
    sample_width: int = 2,
    channels: int = 1,
) -> tuple[str, io.BytesIO, str]:
    if sample_width not in {1, 2, 3, 4}:
        raise UserError("Sample width must be between 1 and 4 bytes")

    if frame_rate <= 0:
        raise UserError("Frame rate must be greater than zero")

    if buffer.dtype not in (np.int16, np.float32):
        raise UserError("Buffer must be a numpy array of int16 or float32")

    if channels <= 0:
        raise UserError("Channels must be greater than zero")

    if buffer.size % channels != 0:
        raise UserError("Buffer must contain complete channel frames")

    if buffer.dtype == np.float32:
        clipped_buffer = np.clip(buffer, -1.0, 1.0)
        if sample_width == 1:
            audio_bytes = (
                np.rint((clipped_buffer.astype(np.float64) + 1.0) * 127.5)
                .astype(np.uint8)
                .tobytes()
            )
        elif sample_width == 2:
            # Keep the established float32-to-PCM16 quantization unchanged.
            audio_bytes = (clipped_buffer * 32767).astype("<i2").tobytes()
        else:
            max_sample_value = (1 << (sample_width * 8 - 1)) - 1
            pcm_buffer = (clipped_buffer.astype(np.float64) * max_sample_value).astype(np.int32)
            if sample_width == 3:
                pcm_32 = np.ascontiguousarray(pcm_buffer, dtype="<i4")
                audio_bytes = pcm_32.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
            else:
                audio_bytes = pcm_buffer.astype("<i4").tobytes()
    elif sample_width == 1:
        audio_bytes = ((buffer.astype(np.int32) >> 8) + 128).astype(np.uint8).tobytes()
    else:
        pcm_buffer = buffer.astype(np.int32) << (8 * (sample_width - 2))
        if sample_width == 2:
            audio_bytes = pcm_buffer.astype("<i2").tobytes()
        elif sample_width == 3:
            pcm_32 = np.ascontiguousarray(pcm_buffer, dtype="<i4")
            audio_bytes = pcm_32.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
        else:
            audio_bytes = pcm_buffer.astype("<i4").tobytes()

    audio_file = io.BytesIO()
    with wave.open(audio_file, "w") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(frame_rate)
        wav_file.writeframes(audio_bytes)
        audio_file.seek(0)

    # (filename, bytes, content_type)
    return ("audio.wav", audio_file, "audio/wav")


@dataclass
class AudioInput:
    """Static audio to be used as input for the VoicePipeline."""

    buffer: npt.NDArray[np.int16 | np.float32]
    """
    A buffer containing the audio data for the agent. Must be a numpy array of int16 or float32.
    """

    frame_rate: int = DEFAULT_SAMPLE_RATE
    """The sample rate of the audio data. Defaults to 24000."""

    sample_width: int = 2
    """The sample width of the audio data. Defaults to 2."""

    channels: int = 1
    """The number of channels in the audio data. Defaults to 1."""

    def to_audio_file(self) -> tuple[str, io.BytesIO, str]:
        """Returns a tuple of (filename, bytes, content_type)"""
        return _buffer_to_audio_file(self.buffer, self.frame_rate, self.sample_width, self.channels)

    def to_base64(self) -> str:
        """Returns the audio data as a base64 encoded string."""
        if self.buffer.dtype == np.float32:
            # convert to int16 without mutating the caller's buffer
            int16_buffer = (np.clip(self.buffer, -1.0, 1.0) * 32767).astype(np.int16)
        elif self.buffer.dtype == np.int16:
            int16_buffer = cast("npt.NDArray[np.int16]", self.buffer)
        else:
            raise UserError("Buffer must be a numpy array of int16 or float32")

        return base64.b64encode(int16_buffer.tobytes()).decode("utf-8")


class StreamedAudioInput:
    """Audio input represented as a stream of audio data. You can pass this to the `VoicePipeline`
    and then push audio data into the queue using the `add_audio` method.
    """

    def __init__(self):
        self.queue: asyncio.Queue[npt.NDArray[np.int16 | np.float32] | None] = asyncio.Queue()

    async def add_audio(self, audio: npt.NDArray[np.int16 | np.float32] | None) -> None:
        """Adds more audio data to the stream.

        Args:
            audio: The audio data to add. Must be a numpy array of int16 or float32 or None.
                If None passed, it indicates the end of the stream.
        """
        await self.queue.put(audio)
