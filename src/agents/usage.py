from dataclasses import dataclass
from typing import TypeVar

from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

T = TypeVar("T", bound="InputTokensDetails | OutputTokensDetails")


def add_numeric_fields(current: T, other: T) -> None:
    for field in current.__dataclass_fields__:
        v1 = getattr(current, field, 0)
        v2 = getattr(other, field, 0)
        if isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
            setattr(current, field, (v1 or 0) + (v2 or 0))


@dataclass
class Usage:
    requests: int = 0
    """Total requests made to the LLM API."""

    input_tokens: int = 0
    """Total input tokens sent, across all requests."""

    input_tokens_details: InputTokensDetails = InputTokensDetails(cached_tokens=0)

    output_tokens: int = 0
    """Total output tokens received, across all requests."""

    output_tokens_details: OutputTokensDetails = OutputTokensDetails(reasoning_tokens=0)

    total_tokens: int = 0
    """Total tokens sent and received, across all requests."""

    def add(self, other: "Usage") -> None:
        self.requests += other.requests if other.requests else 0
        self.input_tokens += other.input_tokens if other.input_tokens else 0
        self.output_tokens += other.output_tokens if other.output_tokens else 0
        self.total_tokens += other.total_tokens if other.total_tokens else 0
        add_numeric_fields(self.input_tokens_details, other.input_tokens_details)
        add_numeric_fields(self.output_tokens_details, other.output_tokens_details)
