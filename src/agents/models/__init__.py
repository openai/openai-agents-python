"""Model implementations and utilities for working with language models."""

from ._openai_shared import (
    TOpenAIClient,
    create_client,
    get_default_openai_client,
    get_default_openai_key,
    get_use_responses_by_default,
    set_default_openai_client,
    set_default_openai_key,
    set_use_responses_by_default,
)
from .interface import Model, ModelProvider, ModelRetrySettings, ModelTracing
from .openai_chatcompletions import OpenAIChatCompletionsModel
from .openai_provider import OpenAIProvider
from .openai_responses import OpenAIResponsesModel
from .utils import (
    cache_model_response,
    clear_cache,
    compute_cache_key,
    get_token_count_estimate,
    set_cache_ttl,
    validate_response,
)

__all__ = [
    # Interface
    "Model",
    "ModelProvider",
    "ModelRetrySettings",
    "ModelTracing",

    # OpenAI utilities
    "get_default_openai_client",
    "get_default_openai_key",
    "get_use_responses_by_default",
    "set_default_openai_client",
    "set_default_openai_key",
    "set_use_responses_by_default",
    "TOpenAIClient",
    "create_client",

    # Model implementations
    "OpenAIChatCompletionsModel",
    "OpenAIProvider",
    "OpenAIResponsesModel",

    # Caching and utilities
    "cache_model_response",
    "clear_cache",
    "compute_cache_key",
    "get_token_count_estimate",
    "set_cache_ttl",
    "validate_response",
]
