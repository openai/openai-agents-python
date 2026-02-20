"""camb.ai voice model provider.

Provides a :class:`VoiceModelProvider` that creates :class:`CambAITTSModel`
instances.  camb.ai does not offer a speech-to-text service, so
:meth:`get_stt_model` always raises :class:`NotImplementedError`.
"""

from __future__ import annotations

from typing import Any

from ..model import STTModel, TTSModel, VoiceModelProvider
from .camb_tts import CambAITTSModel

DEFAULT_TTS_MODEL = "mars-flash"


class CambAIVoiceModelProvider(VoiceModelProvider):
    """A :class:`VoiceModelProvider` backed by camb.ai.

    Args:
        api_key: camb.ai API key.  Falls back to ``CAMB_API_KEY`` env var.
        camb_client: An optional pre-built ``AsyncCambAI`` instance.
        voice_id: Default voice identifier used by all returned TTS models.
        language: Default BCP-47 language code (e.g. ``"en-us"``).
        user_instructions: Instructions forwarded when using ``mars-instruct``.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        camb_client: Any | None = None,
        voice_id: int = 147320,
        language: str = "en-us",
        user_instructions: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._camb_client = camb_client
        self._voice_id = voice_id
        self._language = language
        self._user_instructions = user_instructions

    def get_stt_model(self, model_name: str | None) -> STTModel:
        """camb.ai does not provide a speech-to-text model.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError("camb.ai does not provide a speech-to-text model.")

    def get_tts_model(self, model_name: str | None) -> TTSModel:
        """Return a :class:`CambAITTSModel` for the given model name.

        Args:
            model_name: Model name (e.g. ``"mars-flash"``, ``"mars-pro"``).
                Falls back to ``"mars-flash"`` when ``None``.
        """
        return CambAITTSModel(
            model=model_name or DEFAULT_TTS_MODEL,
            camb_client=self._camb_client,
            api_key=self._api_key,
            voice_id=self._voice_id,
            language=self._language,
            user_instructions=self._user_instructions,
        )
