from __future__ import annotations

import httpx
from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from .interface import Model, ModelProvider
from .gemini_chatcompletions import GeminiChatCompletionsModel

DEFAULT_MODEL: str = "gemini-2.0-flash"

_http_client: httpx.AsyncClient | None = None

# If we create a new httpx client for each request, that would mean no sharing of connection pools,
# which would mean worse latency and resource usage. So, we share the client across requests.
def shared_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = DefaultAsyncHttpxClient()
    return _http_client


class GeminiProvider(ModelProvider):
    """
    Model provider for Google Gemini models.
    
    Uses Google's OpenAI-compatible API endpoint to integrate with Gemini models.
    """
    
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = "https://generativelanguage.googleapis.com/v1beta/openai/",
        openai_client: AsyncOpenAI | None = None,
    ) -> None:
        if openai_client is not None:
            assert api_key is None and base_url is None, (
                "Don't provide api_key or base_url if you provide openai_client"
            )
            self._client: AsyncOpenAI | None = openai_client
        else:
            self._client = None
            self._stored_api_key = api_key
            self._stored_base_url = base_url

    # We lazy load the client in case you never actually use GeminiProvider()
    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self._stored_api_key,
                base_url=self._stored_base_url,
                http_client=shared_http_client(),
            )

        return self._client

    def get_model(self, model_name: str | None) -> Model:
        if model_name is None:
            model_name = DEFAULT_MODEL

        client = self._get_client()

        return GeminiChatCompletionsModel(model=model_name, openai_client=client)