from __future__ import annotations

import httpx
from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from ...models import _openai_shared
from ..model import STTModel, TTSModel, VoiceModelProvider
from .openai_stt import OpenAISTTModel
from .openai_tts import OpenAITTSModel

_http_client: httpx.AsyncClient | None = None


# If we create a new httpx client for each request, that would mean no sharing of connection pools,
# which would mean worse latency and resource usage. So, we share the client across requests.
def shared_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = DefaultAsyncHttpxClient()
    return _http_client


DEFAULT_STT_MODEL = "gpt-4o-transcribe"
DEFAULT_TTS_MODEL = "gpt-4o-mini-tts"


class OpenAIVoiceModelProvider(VoiceModelProvider):
    """A voice model provider that uses OpenAI models."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        openai_client: AsyncOpenAI | None = None,
        organization: str | None = None,
        project: str | None = None,
    ) -> None:
        """Create a new OpenAI voice model provider.

        Args:
            api_key: The API key to use for the OpenAI client. If not provided, we will use the
                default API key.
            base_url: The base URL to use for the OpenAI client. If not provided, we will use the
                default base URL.
            openai_client: An optional OpenAI client to use. If not provided, we will create a new
                OpenAI client using the api_key and base_url.
            organization: The organization to use for the OpenAI client.
            project: The project to use for the OpenAI client.
        """
        if openai_client is not None:
            assert api_key is None and base_url is None, (
                "Don't provide api_key or base_url if you provide openai_client"
            )
            self._client: AsyncOpenAI | None = openai_client
        else:
            self._client = None
            self._stored_api_key = api_key
            self._stored_base_url = base_url
            self._stored_organization = organization
            self._stored_project = project

    # We lazy load the client in case you never actually use OpenAIProvider(). Otherwise
    # AsyncOpenAI() raises an error if you don't have an API key set.
    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = _openai_shared.get_default_openai_client() or AsyncOpenAI(
                api_key=self._stored_api_key or _openai_shared.get_default_openai_key(),
                base_url=self._stored_base_url,
                organization=self._stored_organization,
                project=self._stored_project,
                http_client=shared_http_client(),
            )

        return self._client

    def get_stt_model(self, model_name: str | None) -> STTModel:
        """Get a speech-to-text model by name.

        Args:
            model_name: The name of the model to get.

        Returns:
            The speech-to-text model.
        """
        return OpenAISTTModel(model_name or DEFAULT_STT_MODEL, self._get_client())

    def get_tts_model(self, model_name: str | None) -> TTSModel:
        """Get a text-to-speech model by name.

        Args:
            model_name: The name of the model to get.

        Returns:
            The text-to-speech model.
        """
        return OpenAITTSModel(model_name or DEFAULT_TTS_MODEL, self._get_client())
