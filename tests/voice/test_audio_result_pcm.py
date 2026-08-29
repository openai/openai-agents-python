import numpy as np

from agents.voice import StreamedAudioResult, TTSModelSettings, VoicePipelineConfig

from .pipeline_test_models import ZeroPcmTTSModel


def test_streamed_audio_result_normalizes_pcm16_full_scale_to_float32() -> None:
    result = StreamedAudioResult(
        ZeroPcmTTSModel(),
        TTSModelSettings(dtype=np.float32),
        VoicePipelineConfig(),
    )
    samples = np.array([-32768, 32767], dtype=np.int16)

    transformed = result._transform_audio_buffer([samples.tobytes()], np.float32)

    assert transformed.dtype == np.float32
    assert transformed.shape == (2, 1)
    assert transformed[0, 0] == -1.0
    assert transformed[1, 0] == np.float32(32767 / 32768)
    assert np.all(transformed >= -1.0)
    assert np.all(transformed <= 1.0)
