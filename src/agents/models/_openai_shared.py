from __future__ import annotations

from openai import AsyncOpenAI

_default_openai_key: str | None = None
_default_openai_client: AsyncOpenAI | None = None
_use_responses_by_default: bool = True


def set_default_openai_key(key: str) -> None:
    """
    Set the default OpenAI API key.

    Args:
        key (str): The OpenAI API key to be used for authentication.
    """
    global _default_openai_key
    _default_openai_key = key


def get_default_openai_key() -> str | None:
    """
    Get the default OpenAI API key.

    Returns:
        str | None: The currently set API key, or None if not set.
    """
    return _default_openai_key


def set_default_openai_client(client: AsyncOpenAI) -> None:
    """
    Set the default AsyncOpenAI client instance.

    Args:
        client (AsyncOpenAI): An instance of the AsyncOpenAI client.
    """
    global _default_openai_client
    _default_openai_client = client


def get_default_openai_client() -> AsyncOpenAI | None:
    """
    Get the default AsyncOpenAI client instance.

    Returns:
        AsyncOpenAI | None: The currently set AsyncOpenAI client, or None if not set.
    """
    return _default_openai_client


def set_use_responses_by_default(use_responses: bool) -> None:
    """
    Configure whether responses should be used by default.

    Args:
        use_responses (bool): If True, responses will be used by default.
    """
    global _use_responses_by_default
    _use_responses_by_default = use_responses


def get_use_responses_by_default() -> bool:
    """
    Check whether responses are configured to be used by default.

    Returns:
        bool: True if responses are set to be used by default, False otherwise.
    """
    return _use_responses_by_default
