from dataclasses import field

from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails
from pydantic.dataclasses import dataclass


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

    input_tokens_details: InputTokensDetails = field(
        default_factory=lambda: InputTokensDetails(cached_tokens=0)
    )
    """Details about the input tokens, matching responses API usage details."""
    output_tokens: int = 0
    """Total output tokens received, across all requests."""

    output_tokens_details: OutputTokensDetails = field(
        default_factory=lambda: OutputTokensDetails(reasoning_tokens=0)
    )
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

    def add(self, other: "Usage") -> None:
        """Add another Usage object to this one, aggregating all fields.

        This method automatically preserves request_usage_entries.

        Args:
            other: The Usage object to add to this one.
        """
        self.requests += other.requests if other.requests else 0
        self.input_tokens += other.input_tokens if other.input_tokens else 0
        self.output_tokens += other.output_tokens if other.output_tokens else 0
        self.total_tokens += other.total_tokens if other.total_tokens else 0
        self.input_tokens_details = InputTokensDetails(
            cached_tokens=self.input_tokens_details.cached_tokens
            + other.input_tokens_details.cached_tokens
        )

        self.output_tokens_details = OutputTokensDetails(
            reasoning_tokens=self.output_tokens_details.reasoning_tokens
            + other.output_tokens_details.reasoning_tokens
        )

        # Automatically preserve request_usage_entries.
        # If the other Usage represents a single request with tokens, record it.
        if other.requests == 1 and other.total_tokens > 0:
            request_usage = RequestUsage(
                input_tokens=other.input_tokens,
                output_tokens=other.output_tokens,
                total_tokens=other.total_tokens,
                input_tokens_details=other.input_tokens_details,
                output_tokens_details=other.output_tokens_details,
            )
            self.request_usage_entries.append(request_usage)
        elif other.request_usage_entries:
            # If the other Usage already has individual request breakdowns, merge them.
            self.request_usage_entries.extend(other.request_usage_entries)
