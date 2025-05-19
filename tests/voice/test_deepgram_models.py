from __future__ import annotations

import pytest

from agents.voice import DeepgramVoiceModelProvider


@pytest.mark.asyncio
async def test_provider_returns_models() -> None:
    provider = DeepgramVoiceModelProvider(api_key="key")
    stt = provider.get_stt_model(None)
    tts = provider.get_tts_model(None)
    assert stt.model_name == "nova-3"
    assert tts.model_name == "aura-2"
