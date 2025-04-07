from __future__ import annotations

import logging

import httpx
from openai import DefaultAsyncHttpxClient, OpenAIError

from . import _openai_shared
from ._openai_shared import TOpenAIClient, create_client
from .interface import Model, ModelProvider
from .openai_chatcompletions import OpenAIChatCompletionsModel
from .openai_responses import OpenAIResponsesModel

DEFAULT_MODEL: str = "gpt-4o"
_logger = logging.getLogger(__name__)

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
        openai_client: TOpenAIClient | None = None,
        organization: str | None = None,
        project: str | None = None,
        use_responses: bool | None = None,
        default_model: str = DEFAULT_MODEL,
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
            default_model: The default model to use if none is specified.
        """
        if openai_client is not None:
            assert api_key is None and base_url is None, (
                "Don't provide api_key or base_url if you provide openai_client"
            )
            self._client: TOpenAIClient | None = openai_client
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

        self._default_model = default_model

    # We lazy load the client in case you never actually use OpenAIProvider(). Otherwise
    # AsyncOpenAI() raises an error if you don't have an API key set.
    def _get_client(self) -> TOpenAIClient:
        if self._client is None:
            default_client = _openai_shared.get_default_openai_client()
            if default_client:
                self._client = default_client
            else:
                try:
                    self._client = create_client(
                        api_key=self._stored_api_key,
                        base_url=self._stored_base_url,
                        organization=self._stored_organization,
                        project=self._stored_project,
                        http_client=shared_http_client(),
                    )
                except OpenAIError as e:
                    _logger.error(f"Failed to create OpenAI client: {e}")
                    raise

        return self._client

    def get_model(self, model_name: str | None) -> Model:
        """Get a model instance by name.

        Args:
            model_name: The name of the model to get. If None, uses the default model.

        Returns:
            An OpenAI model implementation (either Responses or ChatCompletions
            based on configuration)
        """
        if model_name is None:
            model_name = self._default_model

        client = self._get_client()

        return (
            OpenAIResponsesModel(model=model_name, openai_client=client)
            if self._use_responses
            else OpenAIChatCompletionsModel(model=model_name, openai_client=client)
        )
