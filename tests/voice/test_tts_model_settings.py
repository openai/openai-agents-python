from __future__ import annotations

import numpy as np

from agents.voice import TTSModelSettings


def test_tts_model_settings_normalizes_string_dtype() -> None:
    settings = TTSModelSettings(dtype="float32")

    assert settings.dtype == np.dtype("float32")


def test_tts_model_settings_normalizes_int16_dtype() -> None:
    settings = TTSModelSettings(dtype="int16")

    assert settings.dtype == np.dtype("int16")


def test_tts_model_settings_accepts_numpy_dtype() -> None:
    settings = TTSModelSettings(dtype=np.float32)

    assert settings.dtype == np.dtype("float32")
