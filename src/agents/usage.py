from __future__ import annotations

from dataclasses import field
from typing import Annotated

from openai.types.completion_usage import CompletionTokensDetails, PromptTokensDetails
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails
from pydantic import BeforeValidator
from pydantic.dataclasses import dataclass


def _normalize_input_tokens_details(
    v: InputTokensDetails | PromptTokensDetails | None,
) -> InputTokensDetails:
    """Converts None or PromptTokensDetails to InputTokensDetails."""
    if v is None:
        return InputTokensDetails(cached_tokens=0)
    if isinstance(v, PromptTokensDetails):
        return InputTokensDetails(cached_tokens=v.cached_tokens or 0)
    return v


def _normalize_output_tokens_details(
    v: OutputTokensDetails | CompletionTokensDetails | None,
) -> OutputTokensDetails:
    """Converts None or CompletionTokensDetails to OutputTokensDetails."""
    if v is None:
        return OutputTokensDetails(reasoning_tokens=0)
    if isinstance(v, CompletionTokensDetails):
        return OutputTokensDetails(reasoning_tokens=v.reasoning_tokens or 0)
    return v


@dataclass
class RequestUsage:
    """Usage details for a single API request."""

    input_tokens: int
    """Input tokens for this individual request."""

    output_tokens: int
    """Output tokens for this individual request."""

    total_tokens: int
    """Total tokens (input + output) for this individual request."""

    input_tokens_details: InputTokensDetails
    """Details about the input tokens for this individual request."""

    output_tokens_details: OutputTokensDetails
    """Details about the output tokens for this individual request."""


@dataclass
class Usage:
    requests: int = 0
    """Total requests made to the LLM API."""

    input_tokens: int = 0
    """Total input tokens sent, across all requests."""

    input_tokens_details: Annotated[
        InputTokensDetails, BeforeValidator(_normalize_input_tokens_details)
    ] = field(default_factory=lambda: InputTokensDetails(cached_tokens=0))
    """Details about the input tokens, matching responses API usage details."""
    output_tokens: int = 0
    """Total output tokens received, across all requests."""

    output_tokens_details: Annotated[
        OutputTokensDetails, BeforeValidator(_normalize_output_tokens_details)
    ] = field(default_factory=lambda: OutputTokensDetails(reasoning_tokens=0))
    """Details about the output tokens, matching responses API usage details."""

    total_tokens: int = 0
    """Total tokens sent and received, across all requests."""

    request_usage_entries: list[RequestUsage] = field(default_factory=list)
    """List of RequestUsage entries for accurate per-request cost calculation.

    Each call to `add()` automatically creates an entry in this list if the added usage
    represents a new request (i.e., has non-zero tokens).

    Example:
        For a run that makes 3 API calls with 100K, 150K, and 80K input tokens each,
        the aggregated `input_tokens` would be 330K, but `request_usage_entries` would
        preserve the [100K, 150K, 80K] breakdown, which could be helpful for detailed
        cost calculation or context window management.
    """

    def __post_init__(self) -> None:
        # Some providers don't populate optional token detail fields
        # (cached_tokens, reasoning_tokens), and the OpenAI SDK's generated
        # code can bypass Pydantic validation (e.g., via model_construct),
        # allowing None values. We normalize these to 0 to prevent TypeErrors.
        input_details_none = self.input_tokens_details is None
        input_cached_none = (
            not input_details_none and self.input_tokens_details.cached_tokens is None
        )
        if input_details_none or input_cached_none:
            self.input_tokens_details = InputTokensDetails(cached_tokens=0)

        output_details_none = self.output_tokens_details is None
        output_reasoning_none = (
            not output_details_none and self.output_tokens_details.reasoning_tokens is None
        )
        if output_details_none or output_reasoning_none:
            self.output_tokens_details = OutputTokensDetails(reasoning_tokens=0)

    def add(self, other: Usage) -> None:
        """Add another Usage object to this one, aggregating all fields.

        This method automatically preserves request_usage_entries.

        Args:
            other: The Usage object to add to this one.
        """
        self.requests += other.requests if other.requests else 0
        self.input_tokens += other.input_tokens if other.input_tokens else 0
        self.output_tokens += other.output_tokens if other.output_tokens else 0
        self.total_tokens += other.total_tokens if other.total_tokens else 0

        # Null guards for nested token details (other may bypass validation via model_construct)
        other_cached = (
            other.input_tokens_details.cached_tokens
            if other.input_tokens_details and other.input_tokens_details.cached_tokens
            else 0
        )
        other_reasoning = (
            other.output_tokens_details.reasoning_tokens
            if other.output_tokens_details and other.output_tokens_details.reasoning_tokens
            else 0
        )
        self_cached = (
            self.input_tokens_details.cached_tokens
            if self.input_tokens_details and self.input_tokens_details.cached_tokens
            else 0
        )
        self_reasoning = (
            self.output_tokens_details.reasoning_tokens
            if self.output_tokens_details and self.output_tokens_details.reasoning_tokens
            else 0
        )

        self.input_tokens_details = InputTokensDetails(cached_tokens=self_cached + other_cached)

        self.output_tokens_details = OutputTokensDetails(
            reasoning_tokens=self_reasoning + other_reasoning
        )

        # Automatically preserve request_usage_entries.
        # If the other Usage represents a single request with tokens, record it.
        if other.requests == 1 and other.total_tokens > 0:
            input_details = other.input_tokens_details or InputTokensDetails(cached_tokens=0)
            output_details = other.output_tokens_details or OutputTokensDetails(reasoning_tokens=0)
            request_usage = RequestUsage(
                input_tokens=other.input_tokens,
                output_tokens=other.output_tokens,
                total_tokens=other.total_tokens,
                input_tokens_details=input_details,
                output_tokens_details=output_details,
            )
            self.request_usage_entries.append(request_usage)
        elif other.request_usage_entries:
            # If the other Usage already has individual request breakdowns, merge them.
            self.request_usage_entries.extend(other.request_usage_entries)
