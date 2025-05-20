from dataclasses import dataclass, field
from typing import TypeVar

from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def add_numeric_fields(current: T, other: T) -> T:
    """
    Add numeric fields from other to current.
    """
    clone = current.model_copy()
    for key, v1 in current.model_dump().items():
        v2 = getattr(other, key, 0)
        if isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
            setattr(clone, key, (v1 or 0) + (v2 or 0))
    return clone


def add_input_tokens_details(
    current: InputTokensDetails, other: InputTokensDetails
) -> InputTokensDetails:
    return add_numeric_fields(current, other)


def add_output_tokens_details(
    current: OutputTokensDetails, other: OutputTokensDetails
) -> OutputTokensDetails:
    return add_numeric_fields(current, other)


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
        self.input_tokens_details = add_input_tokens_details(
            self.input_tokens_details, other.input_tokens_details
        )
        self.output_tokens_details = add_output_tokens_details(
            self.output_tokens_details, other.output_tokens_details
        )
