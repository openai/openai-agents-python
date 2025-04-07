from __future__ import annotations

import logging
from typing import Any, TypeAlias

from openai import AsyncOpenAI

# Type aliases for common OpenAI types
TOpenAIClient: TypeAlias = AsyncOpenAI
TOpenAIClientOptions: TypeAlias = dict[str, Any]

_default_openai_key: str | None = None
_default_openai_client: TOpenAIClient | None = None
_use_responses_by_default: bool = True
_logger = logging.getLogger(__name__)


def set_default_openai_key(key: str) -> None:
    """Set the default OpenAI API key to use when creating clients.

    Args:
        key: The OpenAI API key
    """
    global _default_openai_key
    _default_openai_key = key


def get_default_openai_key() -> str | None:
    """Get the default OpenAI API key.

    Returns:
        The default OpenAI API key, or None if not set
    """
    return _default_openai_key


def set_default_openai_client(client: TOpenAIClient) -> None:
    """Set the default OpenAI client to use.

    Args:
        client: The OpenAI client instance
    """
    global _default_openai_client
    _default_openai_client = client


def get_default_openai_client() -> TOpenAIClient | None:
    """Get the default OpenAI client.

    Returns:
        The default OpenAI client, or None if not set
    """
    return _default_openai_client


def set_use_responses_by_default(use_responses: bool) -> None:
    """Set whether to use the Responses API by default.

    Args:
        use_responses: Whether to use the Responses API
    """
    global _use_responses_by_default
    _use_responses_by_default = use_responses


def get_use_responses_by_default() -> bool:
    """Get whether to use the Responses API by default.

    Returns:
        Whether to use the Responses API by default
    """
    return _use_responses_by_default


def create_client(
    api_key: str | None = None,
    base_url: str | None = None,
    organization: str | None = None,
    project: str | None = None,
    http_client: Any = None,
) -> TOpenAIClient:
    """Create a new OpenAI client with the given parameters.

    This is a utility function to standardize client creation across the codebase.

    Args:
        api_key: The API key to use. If not provided, uses the default.
        base_url: The base URL to use. If not provided, uses the default.
        organization: The organization to use.
        project: The project to use.
        http_client: The HTTP client to use.

    Returns:
        A new OpenAI client
    """
    return AsyncOpenAI(
        api_key=api_key or get_default_openai_key(),
        base_url=base_url,
        organization=organization,
        project=project,
        http_client=http_client,
    )
