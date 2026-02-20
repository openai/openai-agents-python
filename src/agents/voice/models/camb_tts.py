"""camb.ai MARS text-to-speech model for the voice pipeline.

Streams PCM s16le audio chunks via the ``AsyncCambAI`` SDK, suitable for use
in a :class:`VoicePipeline`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from ..model import TTSModel, TTSModelSettings

# Model-specific sample rates (Hz).
MODEL_SAMPLE_RATES: dict[str, int] = {
    "mars-flash": 22050,
    "mars-pro": 48000,
    "mars-instruct": 22050,
}


def _get_aligned_audio(buffer: bytes) -> tuple[bytes, bytes]:
    """Split *buffer* into 2-byte-aligned audio and a remainder."""
    aligned_size = (len(buffer) // 2) * 2
    return buffer[:aligned_size], buffer[aligned_size:]


class CambAITTSModel(TTSModel):
    """A text-to-speech model backed by camb.ai's MARS family.

    This model streams PCM audio (s16le, mono) and is designed to be used as the
    ``tts_model`` argument to :class:`VoicePipeline`.

    Args:
        model: Model name — ``"mars-flash"`` (fast, 22.05 kHz),
            ``"mars-pro"`` (high quality, 48 kHz), or ``"mars-instruct"``
            (follows *user_instructions*, 22.05 kHz).
        camb_client: An optional pre-built ``AsyncCambAI`` instance.  When
            omitted a new client is created lazily using *api_key*.
        api_key: camb.ai API key.  Falls back to ``CAMB_API_KEY`` env var.
        voice_id: Numeric voice identifier.
        language: BCP-47 language code (e.g. ``"en-us"``, ``"fr-fr"``).
        user_instructions: Instructions forwarded to the ``mars-instruct``
            model only.
    """

    def __init__(
        self,
        model: str = "mars-flash",
        *,
        camb_client: Any | None = None,
        api_key: str | None = None,
        voice_id: int = 147320,
        language: str = "en-us",
        user_instructions: str | None = None,
    ) -> None:
        self._model = model
        self._camb_client = camb_client
        self._api_key = api_key
        self._voice_id = voice_id
        self._language = language
        self._user_instructions = user_instructions
        self._sample_rate = MODEL_SAMPLE_RATES.get(model, 22050)

    def _get_client(self) -> Any:
        """Return a lazily-initialised ``AsyncCambAI`` client."""
        if self._camb_client is None:
            try:
                from camb.client import AsyncCambAI
            except ImportError as e:
                raise ImportError(
                    "The 'camb' package is required. Install it with: "
                    "pip install 'openai-agents[camb]'"
                ) from e
            self._camb_client = AsyncCambAI(api_key=self._api_key)
        return self._camb_client

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def sample_rate(self) -> int:
        """The native sample rate of the model."""
        return self._sample_rate

    async def run(self, text: str, settings: TTSModelSettings) -> AsyncIterator[bytes]:
        """Stream PCM audio for *text*.

        Args:
            text: The text to convert to speech.
            settings: TTS model settings (voice pipeline level).

        Yields:
            2-byte-aligned PCM s16le audio chunks.
        """
        try:
            from camb import StreamTtsOutputConfiguration
        except ImportError as e:
            raise ImportError(
                "The 'camb' package is required. Install it with: pip install 'openai-agents[camb]'"
            ) from e

        client = self._get_client()

        tts_kwargs: dict[str, Any] = {
            "text": text,
            "voice_id": self._voice_id,
            "language": self._language,
            "speech_model": self._model,
            "output_configuration": StreamTtsOutputConfiguration(format="pcm_s16le"),
        }
        if self._model == "mars-instruct" and self._user_instructions:
            tts_kwargs["user_instructions"] = self._user_instructions

        audio_buffer = b""
        async for chunk in client.text_to_speech.tts(**tts_kwargs):
            if chunk:
                audio_buffer += chunk
                aligned_audio, audio_buffer = _get_aligned_audio(audio_buffer)
                if aligned_audio:
                    yield aligned_audio

        # Yield any remaining complete samples.
        if len(audio_buffer) >= 2:
            aligned_audio, _ = _get_aligned_audio(audio_buffer)
            if aligned_audio:
                yield aligned_audio
