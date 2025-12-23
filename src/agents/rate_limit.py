"""Rate limiting utilities for the Agents SDK.

This module provides rate limiting functionality to help users stay within
API rate limits when using free or low-budget LLM providers.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from .logger import logger

T = TypeVar("T")


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting LLM requests.

    Use this to prevent 429 (rate limit) errors when using providers with
    strict rate limits (e.g., free tiers with 3 requests/minute).

    Example:
        ```python
        run_config = RunConfig(
            model="groq/llama-3.1-8b-instant",
            rate_limit=RateLimitConfig(
                requests_per_minute=3,
                retry_on_rate_limit=True,
            )
        )
        ```
    """

    requests_per_minute: int | None = None
    """Maximum number of requests allowed per minute. If set, the SDK will
    automatically pace requests to stay under this limit."""

    retry_on_rate_limit: bool = True
    """If True, automatically retry requests that receive a 429 response
    with exponential backoff."""

    max_retries: int = 3
    """Maximum number of retry attempts for rate-limited requests."""

    initial_retry_delay: float = 1.0
    """Initial delay in seconds before the first retry attempt."""

    backoff_multiplier: float = 2.0
    """Multiplier for exponential backoff between retries."""

    max_retry_delay: float = 60.0
    """Maximum delay in seconds between retry attempts."""


class RateLimiter:
    """A simple rate limiter using the token bucket algorithm.

    This class helps pace requests to stay within a specified rate limit.
    It tracks request timestamps and waits if necessary before allowing
    new requests.
    """

    def __init__(self, config: RateLimitConfig):
        """Initialize the rate limiter.

        Args:
            config: The rate limit configuration.
        """
        self._config = config
        self._request_times: list[float] = []
        self._lock = asyncio.Lock()

    @property
    def is_enabled(self) -> bool:
        """Check if rate limiting is enabled."""
        return self._config.requests_per_minute is not None

    async def acquire(self) -> None:
        """Wait until a request slot is available.

        This method blocks until it's safe to make a new request without
        exceeding the configured rate limit.
        """
        if not self.is_enabled:
            return

        async with self._lock:
            requests_per_minute = self._config.requests_per_minute
            assert requests_per_minute is not None

            now = time.monotonic()
            window_start = now - 60.0  # 1 minute window

            # Remove requests outside the current window
            self._request_times = [t for t in self._request_times if t > window_start]

            # If we're at the limit, wait until a slot opens up
            if len(self._request_times) >= requests_per_minute:
                # Calculate how long to wait
                oldest_request = self._request_times[0]
                wait_time = oldest_request - window_start
                if wait_time > 0:
                    logger.debug(
                        f"Rate limit: waiting {wait_time:.2f}s "
                        f"({len(self._request_times)}/{requests_per_minute} requests in window)"
                    )
                    await asyncio.sleep(wait_time)
                    # Clean up again after waiting
                    now = time.monotonic()
                    window_start = now - 60.0
                    self._request_times = [t for t in self._request_times if t > window_start]

            # Record this request
            self._request_times.append(time.monotonic())

    async def execute_with_retry(
        self,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """Execute a function with rate limiting and automatic retry on 429 errors.

        Args:
            func: The async function to execute.
            *args: Positional arguments to pass to the function.
            **kwargs: Keyword arguments to pass to the function.

        Returns:
            The return value of the function.

        Raises:
            The last exception if all retries are exhausted.
        """
        # First, wait for rate limit slot
        await self.acquire()

        if not self._config.retry_on_rate_limit:
            return await func(*args, **kwargs)

        last_exception: Exception | None = None
        delay = self._config.initial_retry_delay

        for attempt in range(self._config.max_retries + 1):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                # Check if this is a rate limit error (429)
                error_str = str(e).lower()
                is_rate_limit = (
                    "429" in str(e)
                    or "rate" in error_str
                    or "too many requests" in error_str
                    or "rate_limit" in error_str
                )

                if not is_rate_limit:
                    raise

                last_exception = e

                if attempt < self._config.max_retries:
                    logger.warning(
                        f"Rate limit hit (attempt {attempt + 1}/{self._config.max_retries + 1}). "
                        f"Retrying in {delay:.1f}s..."
                    )
                    await asyncio.sleep(delay)
                    delay = min(
                        delay * self._config.backoff_multiplier, self._config.max_retry_delay
                    )
                    # Wait for a rate limit slot before retrying
                    await self.acquire()

        # All retries exhausted
        assert last_exception is not None
        raise last_exception
