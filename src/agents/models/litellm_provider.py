from __future__ import annotations

import os
import httpx
from openai import AsyncOpenAI
from typing import Any, Dict

from . import _openai_shared
from .interface import Model, ModelProvider
from .openai_chatcompletions import OpenAIChatCompletionsModel
from .openai_responses import OpenAIResponsesModel

DEFAULT_MODEL: str = "gpt-4"

_http_client: httpx.AsyncClient | None = None


def shared_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient()
    return _http_client


class LiteLLMProvider(ModelProvider):
    """A provider that connects to a LiteLLM API server.
    
    Environment Variables:
        LITELLM_API_BASE: The base URL of the LiteLLM API server (e.g. "http://localhost:8000")
        LITELLM_API_KEY: The API key for authentication with the LiteLLM server
        LITELLM_MODEL: The default model to use (optional, defaults to gpt-4)
        
        Model-specific keys (examples):
        OPENAI_API_KEY: OpenAI API key
        ANTHROPIC_API_KEY: Anthropic API key
        AZURE_API_KEY: Azure OpenAI API key
        AZURE_API_BASE: Azure OpenAI API base URL
        AZURE_API_VERSION: Azure OpenAI API version
        AWS_ACCESS_KEY_ID: AWS access key for Bedrock
        AWS_SECRET_ACCESS_KEY: AWS secret key for Bedrock
        AWS_REGION_NAME: AWS region for Bedrock
        
        See LiteLLM documentation for all supported environment variables:
        https://docs.litellm.ai/docs/proxy/environment_variables
    """
    
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model_name: str | None = None,
        use_responses: bool | None = None,
        extra_headers: Dict[str, str] | None = None,
        drop_params: bool = False,  # Whether to drop unsupported params for specific models
    ) -> None:
        # Get configuration from environment variables with fallbacks
        self._api_key = api_key or os.getenv("LITELLM_API_KEY")
        if not self._api_key:
            raise ValueError("LITELLM_API_KEY environment variable or api_key parameter must be set")
            
        self._base_url = base_url or os.getenv("LITELLM_API_BASE")
        if not self._base_url:
            raise ValueError("LITELLM_API_BASE environment variable or base_url parameter must be set")
            
        self._model_name = model_name or os.getenv("LITELLM_MODEL", DEFAULT_MODEL)
        self._drop_params = drop_params

        # Collect all environment variables that start with known prefixes
        # These will be passed through headers to the LiteLLM proxy
        self._env_headers = {
            "Content-Type": "application/json",  # Always set content type
        }
        
        # Add LiteLLM-specific headers
        if self._drop_params:
            self._env_headers["litellm-drop-params"] = "true"
            
        env_prefixes = [
            "OPENAI_", "ANTHROPIC_", "AZURE_", "AWS_", "COHERE_",
            "REPLICATE_", "HUGGINGFACE_", "TOGETHERAI_", "VERTEX_",
            "PALM_", "CLAUDE_", "GEMINI_", "MISTRAL_", "GROQ_"
        ]
        
        for key, value in os.environ.items():
            if any(key.startswith(prefix) for prefix in env_prefixes):
                # Convert environment variables to headers
                header_key = f"x-{key.lower().replace('_', '-')}"
                self._env_headers[header_key] = value

        # Merge any extra headers provided
        if extra_headers:
            self._env_headers.update(extra_headers)

        # Create an OpenAI client configured to use the LiteLLM API server
        # LiteLLM server provides an OpenAI-compatible API
        self._openai_client = AsyncOpenAI(
            api_key=self._api_key,
            base_url=self._base_url,
            http_client=shared_http_client(),
            default_headers=self._env_headers  # Pass all provider keys as headers
        )

        if use_responses is not None:
            self._use_responses = use_responses
        else:
            self._use_responses = _openai_shared.get_use_responses_by_default()

    def get_model(self, model_name: str | None) -> Model:
        """Get a model by name.
        
        Args:
            model_name: The name of the model to get. If None, uses the default model
                      configured through LITELLM_MODEL or the constructor.
                      
                      For OpenAI-compatible endpoints, prefix with 'openai/'
                      For completion endpoints, prefix with 'text-completion-openai/'
            
        Returns:
            The model implementation, either using OpenAI Responses or Chat Completions format.
            The response format is identical to OpenAI's API as LiteLLM provides full compatibility.
        """
        if model_name is None:
            model_name = self._model_name
            
        # If model doesn't have a provider prefix, add the appropriate one
        if not any(model_name.startswith(prefix) for prefix in [
            "openai/", "anthropic/", "azure/", "aws/", "cohere/",
            "replicate/", "huggingface/", "together/", "vertex_ai/",
            "palm/", "claude/", "gemini/", "mistral/", "groq/"
        ]):
            # Map model names to their providers
            provider_prefixes = {
                "gpt-": "openai/",
                "claude-": "anthropic/",
                "mistral-": "mistral/",
                "gemini-": "gemini/",
                "j2-": "aws/",
                "command-": "cohere/",
                "llama-": "replicate/",
                "palm-": "palm/",
                "groq-": "groq/",
            }
            
            # Find the matching provider prefix
            prefix = next(
                (prefix for name_prefix, prefix in provider_prefixes.items() 
                 if model_name.startswith(name_prefix)),
                "openai/"  # Default to OpenAI if no match
            )
            model_name = f"{prefix}{model_name}"

        # LiteLLM server provides an OpenAI-compatible API, so we can reuse the OpenAI models
        return (
            OpenAIResponsesModel(model=model_name, openai_client=self._openai_client)
            if self._use_responses
            else OpenAIChatCompletionsModel(model=model_name, openai_client=self._openai_client)
        )

    async def __aenter__(self) -> LiteLLMProvider:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self._openai_client.close() 