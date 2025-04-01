import io
import wave

import numpy as np
import pytest

try:
    from agents import UserError
    from agents.voice import AudioInput, StreamedAudioInput
    from agents.voice.input import DEFAULT_SAMPLE_RATE, _buffer_to_audio_file
except ImportError:
    pass


def test_buffer_to_audio_file_int16():
    # Create a simple sine wave in int16 format
    t = np.linspace(0, 1, DEFAULT_SAMPLE_RATE)
    buffer = (np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)

    filename, audio_file, content_type = _buffer_to_audio_file(buffer)

    assert filename == "audio.wav"
    assert content_type == "audio/wav"
    assert isinstance(audio_file, io.BytesIO)

    # Verify the WAV file contents
    with wave.open(audio_file, "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == DEFAULT_SAMPLE_RATE
        assert wav_file.getnframes() == len(buffer)


def test_buffer_to_audio_file_float32():
    # Create a simple sine wave in float32 format
    t = np.linspace(0, 1, DEFAULT_SAMPLE_RATE)
    buffer = np.sin(2 * np.pi * 440 * t).astype(np.float32)

    filename, audio_file, content_type = _buffer_to_audio_file(buffer)

    assert filename == "audio.wav"
    assert content_type == "audio/wav"
    assert isinstance(audio_file, io.BytesIO)

    # Verify the WAV file contents
    with wave.open(audio_file, "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == DEFAULT_SAMPLE_RATE
        assert wav_file.getnframes() == len(buffer)


def test_buffer_to_audio_file_invalid_dtype():
    # Create a buffer with invalid dtype (float64)
    buffer = np.array([1.0, 2.0, 3.0], dtype=np.float64)

    with pytest.raises(UserError, match="Buffer must be a numpy array of int16 or float32"):
        # Purposely ignore the type error
        _buffer_to_audio_file(buffer)  # type: ignore


class TestAudioInput:
    def test_audio_input_default_params(self):
        # Create a simple sine wave
        t = np.linspace(0, 1, DEFAULT_SAMPLE_RATE)
        buffer = np.sin(2 * np.pi * 440 * t).astype(np.float32)

        audio_input = AudioInput(buffer=buffer)

        assert audio_input.frame_rate == DEFAULT_SAMPLE_RATE
        assert audio_input.sample_width == 2
        assert audio_input.channels == 1
        assert np.array_equal(audio_input.buffer, buffer)

    def test_audio_input_custom_params(self):
        # Create a simple sine wave
        t = np.linspace(0, 1, 48000)
        buffer = np.sin(2 * np.pi * 440 * t).astype(np.float32)

        audio_input = AudioInput(buffer=buffer, frame_rate=48000, sample_width=4, channels=2)

        assert audio_input.frame_rate == 48000
        assert audio_input.sample_width == 4
        assert audio_input.channels == 2
        assert np.array_equal(audio_input.buffer, buffer)

    def test_audio_input_to_audio_file(self):
        # Create a simple sine wave
        t = np.linspace(0, 1, DEFAULT_SAMPLE_RATE)
        buffer = np.sin(2 * np.pi * 440 * t).astype(np.float32)

        audio_input = AudioInput(buffer=buffer)
        filename, audio_file, content_type = audio_input.to_audio_file()

        assert filename == "audio.wav"
        assert content_type == "audio/wav"
        assert isinstance(audio_file, io.BytesIO)

        # Verify the WAV file contents
        with wave.open(audio_file, "rb") as wav_file:
            assert wav_file.getnchannels() == 1
            assert wav_file.getsampwidth() == 2
            assert wav_file.getframerate() == DEFAULT_SAMPLE_RATE
            assert wav_file.getnframes() == len(buffer)


class TestStreamedAudioInput:
    @pytest.mark.asyncio
    async def test_streamed_audio_input(self):
        streamed_input = StreamedAudioInput()

        # Create some test audio data
        t = np.linspace(0, 1, DEFAULT_SAMPLE_RATE)
        audio1 = np.sin(2 * np.pi * 440 * t).astype(np.float32)
        audio2 = np.sin(2 * np.pi * 880 * t).astype(np.float32)

        # Add audio to the queue
        await streamed_input.add_audio(audio1)
        await streamed_input.add_audio(audio2)

        # Verify the queue contents
        assert streamed_input.queue.qsize() == 2
        # Test non-blocking get
        assert np.array_equal(streamed_input.queue.get_nowait(), audio1)
        # Test blocking get
        assert np.array_equal(await streamed_input.queue.get(), audio2)
        assert streamed_input.queue.empty()
