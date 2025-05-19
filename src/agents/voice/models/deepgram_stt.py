from __future__ import annotations

from typing import Any

import httpx  # type: ignore

from ..input import AudioInput, StreamedAudioInput
from ..model import StreamedTranscriptionSession, STTModel, STTModelSettings


class DeepgramSTTModel(STTModel):
    """Speech-to-text model using Deepgram Nova 3."""

    def __init__(
        self, model: str, api_key: str, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self.model = model
        self.api_key = api_key
        self._client = client or httpx.AsyncClient()

    @property
    def model_name(self) -> str:
        return self.model

    async def transcribe(
        self,
        input: AudioInput,
        settings: STTModelSettings,
        trace_include_sensitive_data: bool,
        trace_include_sensitive_audio_data: bool,
    ) -> str:
        url = f"https://api.deepgram.com/v1/listen?model={self.model}"
        headers = {"Authorization": f"Token {self.api_key}"}
        filename, data, content_type = input.to_audio_file()
        response = await self._client.post(url, headers=headers, content=data.getvalue())
        payload: dict[str, Any] = response.json()
        return (
            payload.get("results", {})
            .get("channels", [{}])[0]
            .get("alternatives", [{}])[0]
            .get("transcript", "")
        )

    async def create_session(
        self,
        input: StreamedAudioInput,
        settings: STTModelSettings,
        trace_include_sensitive_data: bool,
        trace_include_sensitive_audio_data: bool,
    ) -> StreamedTranscriptionSession:
        raise NotImplementedError("Streaming transcription is not implemented.")
