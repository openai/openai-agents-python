from __future__ import annotations

import os
import httpx
from openai import AsyncAzureOpenAI, DefaultAsyncHttpxClient

from . import _openai_shared
from .interface import Model, ModelProvider
from .openai_chatcompletions import OpenAIChatCompletionsModel
from .openai_responses import OpenAIResponsesModel

DEFAULT_API_VERSION = "2025-01-01-preview"  # Changed to a more widely supported Azure OpenAI API version
DEFAULT_DEPLOYMENT = "gpt-4o"  # Default deployment name

_http_client: httpx.AsyncClient | None = None


# Similar to OpenAI Provider, share the HTTP client to improve performance
def shared_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = DefaultAsyncHttpxClient()
    return _http_client


class AzureOpenAIProvider(ModelProvider):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        azure_endpoint: str | None = None,
        api_version: str | None = None,
        base_url: str | None = None,
        deployment: str | None = None,
        openai_client: AsyncAzureOpenAI | None = None,
        use_responses: bool | None = None,
    ) -> None:
        """Create a new Azure OpenAI provider.

        Args:
            api_key: API key for the Azure OpenAI client. If not provided, it will be retrieved from environment variables.
            azure_endpoint: Azure OpenAI endpoint, e.g., "https://{resource-name}.openai.azure.com". If not provided, it will be retrieved from environment variables.
            api_version: Azure OpenAI API version. Default is "2025-01-01-preview".
            base_url: Optional complete base URL. If provided, it will override azure_endpoint. If not provided, it will be retrieved from environment variables.
            deployment: Azure deployment name. Default is "gpt-4o".
            openai_client: Optional Azure OpenAI client instance. If provided, other client parameters will be ignored.
            use_responses: Whether to use OpenAI Responses API. Note: Azure OpenAI may not support the standard Responses API paths.
        """
        if openai_client is not None:
            assert api_key is None and azure_endpoint is None and base_url is None, (
                "Do not provide api_key, azure_endpoint, or base_url when providing openai_client"
            )
            self._client: AsyncAzureOpenAI | None = openai_client
        else:
            self._client = None
            # Automatically retrieve parameters from environment variables if not provided
            self._stored_api_key = api_key or os.getenv("AZURE_OPENAI_API_KEY")
            self._stored_azure_endpoint = azure_endpoint or os.getenv("AZURE_OPENAI_ENDPOINT")
            self._stored_base_url = base_url or os.getenv("AZURE_OPENAI_BASE_URL")
            self._stored_api_version = api_version or os.getenv("AZURE_OPENAI_API_VERSION") or DEFAULT_API_VERSION
            self._stored_deployment = deployment or os.getenv("AZURE_OPENAI_DEPLOYMENT") or DEFAULT_DEPLOYMENT

        # Default to not using Responses API, as Azure OpenAI API paths differ from standard OpenAI
        self._use_responses = False if use_responses is None else use_responses

    # Lazy load the client, ensuring that the client instance is only created when actually used
    def _get_client(self) -> AsyncAzureOpenAI:
        if self._client is None:
            if not self._stored_api_key:
                raise ValueError("Azure OpenAI API key not provided, please set the AZURE_OPENAI_API_KEY environment variable or provide it in the constructor")
            
            # Determine base URL
            base_url = self._stored_base_url or self._stored_azure_endpoint
            if not base_url:
                raise ValueError("Azure OpenAI endpoint not provided, please set the AZURE_OPENAI_ENDPOINT or AZURE_OPENAI_BASE_URL environment variable, or provide it in the constructor")

            self._client = AsyncAzureOpenAI(
                api_key=self._stored_api_key,
                api_version=self._stored_api_version,
                azure_endpoint=base_url,
                http_client=shared_http_client(),
            )

        return self._client

    def get_model(self, model_name: str | None) -> Model:
        """Get a model instance with the specified name
        
        Args:
            model_name: Model name, which is typically the deployment name in Azure OpenAI
            
        Returns:
            Model: Model instance
        """
        # In Azure OpenAI, model_name is actually the deployment name
        deployment_name = model_name if model_name else self._stored_deployment
        
        client = self._get_client()

        # Due to Azure OpenAI URL format requirements, use ChatCompletions API unless explicitly specified
        return (
            OpenAIResponsesModel(model=deployment_name, openai_client=client)
            if self._use_responses
            else OpenAIChatCompletionsModel(model=deployment_name, openai_client=client)
        )
    
    @staticmethod
    def from_env() -> AzureOpenAIProvider:
        """Create AzureOpenAIProvider instance from environment variables
        
        Environment variables:
            AZURE_OPENAI_API_KEY: Azure OpenAI API key
            AZURE_OPENAI_ENDPOINT: Azure OpenAI endpoint
            AZURE_OPENAI_BASE_URL: (Optional) Alternative complete base URL (overrides AZURE_OPENAI_ENDPOINT)
            AZURE_OPENAI_API_VERSION: (Optional) API version
            AZURE_OPENAI_DEPLOYMENT: (Optional) Deployment name
        
        Returns:
            AzureOpenAIProvider: Configured instance
        """
        return AzureOpenAIProvider(
            api_key=os.getenv("AZURE_OPENAI_API_KEY"),
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
            base_url=os.getenv("AZURE_OPENAI_BASE_URL"),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
            deployment=os.getenv("AZURE_OPENAI_DEPLOYMENT"),
        )
