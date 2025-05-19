from __future__ import annotations

import httpx  # type: ignore

from ..model import STTModel, TTSModel, VoiceModelProvider
from .deepgram_stt import DeepgramSTTModel
from .deepgram_tts import DeepgramTTSModel

DEFAULT_STT_MODEL = "nova-3"
DEFAULT_TTS_MODEL = "aura-2"


class DeepgramVoiceModelProvider(VoiceModelProvider):
    """Voice model provider for Deepgram APIs."""

    def __init__(self, api_key: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._api_key = api_key
        self._client = client

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    def get_stt_model(self, model_name: str | None) -> STTModel:
        return DeepgramSTTModel(
            model_name or DEFAULT_STT_MODEL, self._api_key, client=self._get_client()
        )

    def get_tts_model(self, model_name: str | None) -> TTSModel:
        return DeepgramTTSModel(
            model_name or DEFAULT_TTS_MODEL, self._api_key, client=self._get_client()
        )
