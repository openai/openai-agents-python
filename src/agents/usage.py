from dataclasses import dataclass, field

from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails


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

    def add(self, other: "Usage") -> None:
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
