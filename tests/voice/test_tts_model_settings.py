from __future__ import annotations

from collections.abc import AsyncIterator

import numpy as np
import pytest

from agents.exceptions import UserError
from agents.voice import AudioInput, TTSModelSettings, VoicePipeline

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
    events: list[str] = []
    seen_dtypes: list[np.dtype[object]] = []
    async for event in result.stream():
        if event.type == "voice_stream_event_audio":
            events.append("audio")
            if event.data is not None:
                seen_dtypes.append(event.data.dtype)
        elif event.type == "voice_stream_event_lifecycle":
            events.append(event.event)
        elif event.type == "voice_stream_event_error":
            events.append("error")

    assert events == ["turn_started", "audio", "turn_ended", "session_ended"]
    assert seen_dtypes
    assert all(dtype == np.dtype(expected_dtype) for dtype in seen_dtypes)


@pytest.mark.parametrize("dtype", ["not-a-dtype"], ids=["unparseable-string"])
def test_tts_model_settings_preserves_user_error_for_invalid_string_dtype(dtype: str) -> None:
    with pytest.raises(UserError, match="Invalid output dtype"):
        TTSModelSettings(dtype=dtype)


@pytest.mark.asyncio
async def test_voicepipeline_preserves_non_string_dtype_for_custom_tts_provider() -> None:
    class CallableDtypeTTSModel(ZeroPcmTTSModel):
        async def run(self, text: str, settings: TTSModelSettings) -> AsyncIterator[bytes]:
            del text
            # Custom providers historically receive the original DTypeLike object. In particular,
            # NumPy scalar classes are callable and provider code may legitimately use that API.
            assert settings.dtype is np.int16
            assert settings.dtype(1) == np.int16(1)  # type: ignore[operator]
            yield np.zeros(2, dtype=np.int16).tobytes()

    pipeline = VoicePipeline(
        workflow=QueuedVoiceWorkflow([["out_1"]]),
        stt_model=QueuedSTTModel(["first"]),
        tts_model=CallableDtypeTTSModel(),
        config={"tts_settings": {"buffer_size": 1, "dtype": np.int16}},
    )

    result = await pipeline.run(AudioInput(buffer=np.zeros(2, dtype=np.int16)))
    async for _ in result.stream():
        pass
