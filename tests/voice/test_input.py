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


@pytest.mark.parametrize("dtype", [np.int16, np.float32])
@pytest.mark.parametrize("sample_width", [1, 2, 3, 4])
def test_buffer_to_audio_file_honors_sample_width(dtype, sample_width):
    buffer = np.array([-1000, 0, 1000, 2000], dtype=dtype)

    _, audio_file, _ = _buffer_to_audio_file(buffer, sample_width=sample_width)

    with wave.open(audio_file, "rb") as wav_file:
        audio_bytes = wav_file.readframes(wav_file.getnframes())
        assert wav_file.getsampwidth() == sample_width
        assert wav_file.getnframes() == len(buffer)
        assert len(audio_bytes) == len(buffer) * sample_width


def test_buffer_to_audio_file_preserves_int16_amplitude_across_sample_widths():
    buffer = np.array([-32768, -1, 0, 1, 32767], dtype=np.int16)

    _, audio_file_8, _ = _buffer_to_audio_file(buffer, sample_width=1)
    with wave.open(audio_file_8, "rb") as wav_file:
        assert list(wav_file.readframes(wav_file.getnframes())) == [0, 127, 128, 128, 255]

    _, audio_file_32, _ = _buffer_to_audio_file(buffer, sample_width=4)
    with wave.open(audio_file_32, "rb") as wav_file:
        decoded = np.frombuffer(wav_file.readframes(wav_file.getnframes()), dtype="<i4")
        assert np.array_equal(decoded, buffer.astype(np.int32) << 16)


def test_buffer_to_audio_file_keeps_default_float32_quantization():
    buffer = np.array([-0.60606706, -0.5, 0.0, 0.5, 0.89654225], dtype=np.float32)
    expected = (np.clip(buffer, -1.0, 1.0) * 32767).astype(np.int16)

    _, audio_file, _ = _buffer_to_audio_file(buffer)

    with wave.open(audio_file, "rb") as wav_file:
        decoded = np.frombuffer(wav_file.readframes(wav_file.getnframes()), dtype="<i2")
        assert np.array_equal(decoded, expected)


def test_buffer_to_audio_file_rejects_unsupported_sample_width():
    buffer = np.zeros(4, dtype=np.int16)

    with pytest.raises(UserError, match="Sample width must be between 1 and 4 bytes"):
        _buffer_to_audio_file(buffer, sample_width=5)


def test_audio_input_validates_multichannel_frame_alignment():
    complete_audio = AudioInput(buffer=np.array([1, 2, 3, 4], dtype=np.int16), channels=2)
    _, audio_file, _ = complete_audio.to_audio_file()
    with wave.open(audio_file, "rb") as wav_file:
        assert wav_file.getnchannels() == 2
        assert wav_file.getnframes() == 2

    audio_input = AudioInput(buffer=np.array([1, 2, 3], dtype=np.int16), channels=2)

    with pytest.raises(UserError, match="complete channel frames"):
        audio_input.to_audio_file()


@pytest.mark.parametrize("channels", [0, -1])
def test_audio_input_rejects_non_positive_channels(channels):
    audio_input = AudioInput(buffer=np.zeros(4, dtype=np.int16), channels=channels)

    with pytest.raises(UserError, match="Channels must be greater than zero"):
        audio_input.to_audio_file()


@pytest.mark.parametrize("frame_rate", [0, -8000])
def test_audio_input_rejects_non_positive_frame_rate(frame_rate):
    audio_input = AudioInput(buffer=np.zeros(4, dtype=np.int16), frame_rate=frame_rate)

    with pytest.raises(UserError, match="Frame rate must be greater than zero"):
        audio_input.to_audio_file()


def test_buffer_to_audio_file_invalid_dtype():
    # Create a buffer with invalid dtype (float64)
    buffer = np.array([1.0, 2.0, 3.0], dtype=np.float64)

    with pytest.raises(UserError, match="Buffer must be a numpy array of int16 or float32"):
        _buffer_to_audio_file(buffer=buffer)

    with pytest.raises(UserError, match="Buffer must be a numpy array of int16 or float32"):
        _buffer_to_audio_file(buffer=buffer, channels=2)


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

    def test_audio_input_to_base64_does_not_mutate_float32_buffer(self):
        # Regression: to_base64() previously rebound self.buffer to int16,
        # silently corrupting any caller-held reference to the original float32 array.
        buffer = np.sin(2 * np.pi * 440 * np.linspace(0, 1, 100)).astype(np.float32)
        original = buffer.copy()

        audio_input = AudioInput(buffer=buffer)
        audio_input.to_base64()

        assert audio_input.buffer.dtype == np.float32
        assert np.array_equal(audio_input.buffer, original)
        # Calling it twice should still work and return the same encoding.
        assert audio_input.to_base64() == audio_input.to_base64()


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
        retrieved_audio1 = streamed_input.queue.get_nowait()
        # Satisfy type checker
        assert retrieved_audio1 is not None
        assert np.array_equal(retrieved_audio1, audio1)

        # Test blocking get
        retrieved_audio2 = await streamed_input.queue.get()
        # Satisfy type checker
        assert retrieved_audio2 is not None
        assert np.array_equal(retrieved_audio2, audio2)
        assert streamed_input.queue.empty()
