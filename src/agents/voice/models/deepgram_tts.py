from __future__ import annotations

from collections.abc import AsyncIterator

import httpx  # type: ignore

from ..model import TTSModel, TTSModelSettings


class DeepgramTTSModel(TTSModel):
    """Text-to-speech model using Deepgram Aura 2."""

    def __init__(
        self, model: str, api_key: str, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self.model = model
        self.api_key = api_key
        self._client = client or httpx.AsyncClient()

    @property
    def model_name(self) -> str:
        return self.model

    async def run(self, text: str, settings: TTSModelSettings) -> AsyncIterator[bytes]:
        url = "https://api.deepgram.com/v1/speak"
        headers = {"Authorization": f"Token {self.api_key}", "Content-Type": "application/json"}
        payload = {"text": text, "model": self.model, "voice": settings.voice or "aura-2"}
        response = await self._client.post(url, headers=headers, json=payload)
        yield response.content
