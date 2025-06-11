from __future__ import annotations

import asyncio
import base64
import io
import wave
from dataclasses import dataclass
from typing import List

from ..exceptions import UserError
from .imports import np, npt

DEFAULT_SAMPLE_RATE = 24000


def _buffer_to_audio_file(
    buffer: npt.NDArray[np.int16 | np.float32],
    frame_rate: int = DEFAULT_SAMPLE_RATE,
    sample_width: int = 2,
    channels: int = 1,
) -> tuple[str, io.BytesIO, str]:
    if buffer.dtype == np.float32:
        # convert to int16
        buffer = np.clip(buffer, -1.0, 1.0)
        buffer = (buffer * 32767).astype(np.int16)
    elif buffer.dtype != np.int16:
        raise UserError("Buffer must be a numpy array of int16 or float32")

    audio_file = io.BytesIO()
    with wave.open(audio_file, "w") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(frame_rate)
        wav_file.writeframes(buffer.tobytes())
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
        return _buffer_to_audio_file(
            self.buffer, self.frame_rate, self.sample_width, self.channels
        )

    def to_base64(self) -> str:
        """Returns the audio data as a base64 encoded string."""
        if self.buffer.dtype == np.float32:
            # convert to int16
            self.buffer = np.clip(self.buffer, -1.0, 1.0)
            self.buffer = (self.buffer * 32767).astype(np.int16)
        elif self.buffer.dtype != np.int16:
            raise UserError("Buffer must be a numpy array of int16 or float32")

        return base64.b64encode(self.buffer.tobytes()).decode("utf-8")


class StreamedAudioInput(AudioInput):
    """An audio input that can be added to over time.

    This class is useful for continuous audio input, such as from a microphone.
    """

    queue: asyncio.Queue
    is_closed: bool

    def __init__(self) -> None:
        """Initialize a new StreamedAudioInput."""
        self.queue = asyncio.Queue()
        self.is_closed = False

    async def add_audio(
        self, audio: npt.NDArray[npt.np.int16 | npt.np.float32] | None
    ) -> None:
        """Add audio to the input.

        Args:
            audio: The audio data to add. This can be a numpy array of int16 or float32 values.
               NOTE: Passing None is deprecated and will be removed in a future version.
               Use close() to signal the end of the stream instead.
        """
        if audio is None:
            # Backwards compatibility: None was previously used as a sentinel
            # to signal the end of the stream. Log a deprecation warning and call close()
            # for backwards compatibility.
            import warnings

            warnings.warn(
                "Passing None to add_audio() is deprecated. Use close() to signal the end of the stream.",
                DeprecationWarning,
                stacklevel=2,
            )
            await self.close()
            return

        if self.is_closed:
            raise ValueError("Cannot add audio to a closed StreamedAudioInput")

        await self.queue.put(audio)

    async def close(self) -> None:
        """Close the audio input stream.

        This signals that no more audio will be added and allows consumers to clean up resources.
        After calling close(), add_audio() will raise an error.
        """
        if not self.is_closed:
            self.is_closed = True
            # Put a sentinel None value for backwards compatibility
            # TODO: Remove this in a future version when all consumers are updated
            await self.queue.put(None)

    async def read_audio(
        self,
    ) -> List[npt.NDArray[npt.np.int16 | npt.np.float32]]:
        """Read all audio from the input.

        Returns:
            A list of numpy arrays containing the audio data.
        """
        # Drain the queue to get all audio
        result = []
        try:
            while True:
                item = self.queue.get_nowait()
                if item is None:  # Skip the sentinel value
                    continue
                result.append(item)
                self.queue.task_done()
        except asyncio.QueueEmpty:
            pass

        # If the queue is empty and no audio was found, wait for one item
        if not result:
            item = await self.queue.get()
            if item is not None:  # Skip the sentinel value
                result.append(item)
            self.queue.task_done()

        return result
