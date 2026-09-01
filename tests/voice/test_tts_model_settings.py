from __future__ import annotations

import numpy as np
import pytest

from agents.exceptions import UserError
from agents.voice import AudioInput, TTSModelSettings, VoicePipeline

from .helpers import extract_events
from .pipeline_test_models import QueuedSTTModel, QueuedVoiceWorkflow, ZeroPcmTTSModel


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dtype", "expected_dtype"),
    [("int16", np.int16), ("float32", np.float32), ("f4", np.float32)],
    ids=["int16-string", "float32-string", "float32-alias"],
)
async def test_voicepipeline_accepts_string_tts_dtype_from_dictionary_config(
    dtype: str,
    expected_dtype: type[np.int16] | type[np.float32],
) -> None:
    fake_stt = QueuedSTTModel(["first"])
    fake_tts = ZeroPcmTTSModel()
    pipeline = VoicePipeline(
        workflow=QueuedVoiceWorkflow([["out_1"]]),
        stt_model=fake_stt,
        tts_model=fake_tts,
        config={"tts_settings": {"buffer_size": 1, "dtype": dtype}},
    )

    result = await pipeline.run(AudioInput(buffer=np.zeros(2, dtype=np.int16)))
    events, audio_chunks = await extract_events(result)

    assert events == ["turn_started", "audio", "turn_ended", "session_ended"]
    decoded_audio = np.frombuffer(audio_chunks[0], dtype=expected_dtype)
    assert decoded_audio.dtype == np.dtype(expected_dtype)


@pytest.mark.parametrize(
    "dtype",
    ["not-a-dtype", {"names": ["x"], "formats": []}],
    ids=["unparseable-string", "malformed-structured-dtype"],
)
def test_tts_model_settings_preserves_user_error_for_invalid_dtype(dtype: object) -> None:
    with pytest.raises(UserError, match="Invalid output dtype"):
        TTSModelSettings(dtype=dtype)  # type: ignore[arg-type]
