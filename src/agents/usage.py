"""Token usage reported by the native thread."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["TokenUsage", "TokenUsageInfo"]


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Native token counts. Cached and reasoning tokens are subsets, not additions."""

    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class TokenUsageInfo:
    """The native thread's cumulative usage and its most recent usage.

    These are thread snapshots, not this submission's cost or a bill. Native
    counts can include restored history or context estimates. They do not
    aggregate child threads. The most recent usage may span only one response
    even when a submission required several model responses.
    """

    total_token_usage: TokenUsage
    last_token_usage: TokenUsage
    model_context_window: int | None


def _token_usage_info(info: dict[str, Any] | None) -> TokenUsageInfo | None:
    if info is None:
        return None
    return TokenUsageInfo(
        total_token_usage=TokenUsage(**info["total_token_usage"]),
        last_token_usage=TokenUsage(**info["last_token_usage"]),
        model_context_window=info["model_context_window"],
    )
