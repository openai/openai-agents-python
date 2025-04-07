"""Utility functions for model implementations.

This module contains helper functions for working with models and optimizing their usage.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any, TypeVar, cast

from openai.types.chat import ChatCompletion
from openai.types.responses import Response

from ..items import TResponseInputItem
from ..model_settings import ModelSettings

logger = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")
CacheableReturn = TypeVar("CacheableReturn", bound=ChatCompletion | Response)

# Simple in-memory cache for model responses
_response_cache: dict[str, tuple[float, Any]] = {}
_cache_ttl_seconds = 300  # Default 5 minute TTL


def set_cache_ttl(ttl_seconds: int) -> None:
    """Set the TTL for cached responses.

    Args:
        ttl_seconds: Time-to-live in seconds for cached responses
    """
    global _cache_ttl_seconds
    _cache_ttl_seconds = ttl_seconds


def clear_cache() -> None:
    """Clear the model response cache."""
    global _response_cache
    _response_cache = {}


def compute_cache_key(
    model: str,
    system_instructions: str | None,
    input_items: str | list[TResponseInputItem],
    model_settings: ModelSettings,
    tool_names: list[str],
) -> str:
    """Compute a cache key for the given request parameters.

    Args:
        model: Model name
        system_instructions: System instructions
        input_items: Input to the model
        model_settings: Model settings affecting output
        tool_names: Names of available tools

    Returns:
        A string hash key for caching
    """
    # Deterministically serialize the parameters
    params = {
        "model": model,
        "system": system_instructions,
        "input": input_items if isinstance(input_items, str) else json.dumps(input_items),
        "settings": {
            k: v for k, v in asdict(model_settings).items()
            if v is not None and k not in {"metadata", "reasoning"}
        },
        "tools": sorted(tool_names),
    }

    # Create a hash of the parameters
    key_str = json.dumps(params, sort_keys=True)
    return hashlib.sha256(key_str.encode()).hexdigest()


def cache_model_response(
    ttl_seconds: int | None = None,
    cache_keys: list[str] | None = None,
) -> Callable[[Callable[..., Awaitable[R]]], Callable[..., Awaitable[R]]]:
    """Decorator to cache model responses.

    Args:
        ttl_seconds: Time-to-live for cached items
        cache_keys: Additional keys to include in the cache key

    Returns:
        A decorator that caches model responses
    """
    def decorator(func: Callable[..., Awaitable[R]]) -> Callable[..., Awaitable[R]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> R:
            # Get cache settings
            actual_ttl = ttl_seconds or _cache_ttl_seconds

            # Check if caching is disabled
            if actual_ttl <= 0:
                return await func(*args, **kwargs)

            # Compute cache key - we take the function name, args, kwargs hash
            key_parts = [func.__name__]
            if cache_keys:
                key_parts.extend(cache_keys)

            # Add function args/kwargs to key
            for arg in args:
                if isinstance(arg, (str, int, float, bool)):
                    key_parts.append(str(arg))

            for k, v in kwargs.items():
                if isinstance(v, (str, int, float, bool)):
                    key_parts.append(f"{k}:{v}")
                elif isinstance(v, dict):
                    try:
                        serialized = json.dumps(v, sort_keys=True)
                        key_parts.append(f"{k}:{serialized}")
                    except (TypeError, ValueError):
                        # If we can't serialize, we skip this in the key
                        pass

            cache_key = hashlib.md5(":".join(key_parts).encode()).hexdigest()

            # Check cache
            now = time.time()
            if cache_key in _response_cache:
                timestamp, cached_result = _response_cache[cache_key]
                if now - timestamp < actual_ttl:
                    logger.debug(f"Cache hit for {func.__name__}")
                    return cast(R, cached_result)

            # Cache miss - call function and update cache
            result = await func(*args, **kwargs)
            _response_cache[cache_key] = (now, result)
            return result

        return wrapper

    return decorator


def get_token_count_estimate(text: str) -> int:
    """Estimate token count for a text string.

    This is a simple approximation that should work for most English text.
    For more accurate counts, use the tiktoken library.

    Args:
        text: Text to estimate token count for

    Returns:
        Estimated number of tokens
    """
    # Rough approximation: 4 characters per token for English
    return len(text) // 4


def validate_response(response: Any) -> bool:
    """Validate that a model response contains expected fields.

    Args:
        response: The response object to validate

    Returns:
        True if the response is valid
    """
    if isinstance(response, (ChatCompletion, Response)):
        return True
    return False
