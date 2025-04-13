from __future__ import annotations

import httpx
from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from . import _openai_shared
from .interface import Model, ModelProvider
from .openai_chatcompletions import OpenAIChatCompletionsModel
from .openai_responses import OpenAIResponsesModel

DEFAULT_MODEL: str = "gpt-4o"


_http_client: httpx.AsyncClient | None = None


# If we create a new httpx client for each request, that would mean no sharing of connection pools,
# which would mean worse latency and resource usage. So, we share the client across requests.
def shared_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = DefaultAsyncHttpxClient()
    return _http_client


class OpenAIProvider(ModelProvider):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        openai_client: AsyncOpenAI | None = None,
        organization: str | None = None,
        project: str | None = None,
        use_responses: bool | None = None,
    ) -> None:
        """Create a new OpenAI provider.

        Args:
            api_key: The API key to use for the OpenAI client. If not provided, we will use the
                default API key.
            base_url: The base URL to use for the OpenAI client. If not provided, we will use the
                default base URL.
            openai_client: An optional OpenAI client to use. If not provided, we will create a new
                OpenAI client using the api_key and base_url.
            organization: The organization to use for the OpenAI client.
            project: The project to use for the OpenAI client.
            use_responses: Whether to use the OpenAI responses API.
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

        if use_responses is not None:
            self._use_responses = use_responses
        else:
            self._use_responses = _openai_shared.get_use_responses_by_default()

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

    def get_model(self, model_name: str | None) -> Model:
        if model_name is None:
            model_name = DEFAULT_MODEL

        client = self._get_client()

        return (
            OpenAIResponsesModel(model=model_name, openai_client=client)
            if self._use_responses
            else OpenAIChatCompletionsModel(model=model_name, openai_client=client)
        )
