import numpy as np

from agents.voice.result import StreamedAudioResult


def test_transform_audio_buffer_decodes_pcm16_as_little_endian() -> None:
    result = StreamedAudioResult.__new__(StreamedAudioResult)

    raw = b"\x01\x02\x03\x04"
    transformed = result._transform_audio_buffer([raw], np.int16)

    assert transformed.dtype == np.dtype(np.int16)
    assert transformed.tolist() == [0x0201, 0x0403]


def test_transform_audio_buffer_float32_uses_little_endian_samples() -> None:
    result = StreamedAudioResult.__new__(StreamedAudioResult)

    raw = b"\x01\x02\x03\x04"
    transformed = result._transform_audio_buffer([raw], np.float32)

    assert transformed.dtype == np.dtype(np.float32)
    assert transformed.shape == (2, 1)
    np.testing.assert_allclose(
        transformed[:, 0],
        np.asarray([0x0201, 0x0403], dtype=np.float32) / 32767.0,
    )
