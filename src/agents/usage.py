from __future__ import annotations

from collections.abc import Mapping
from dataclasses import field
from typing import Annotated, Any

from openai.types.completion_usage import CompletionTokensDetails, PromptTokensDetails
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails
from pydantic import BeforeValidator, TypeAdapter, ValidationError
from pydantic.dataclasses import dataclass


def _make_input_tokens_details(
    *,
    cached_tokens: int | None = 0,
    cache_write_tokens: int | None = 0,
) -> InputTokensDetails:
    """Build input-token details accepted by OpenAI Python 2.44 and 2.45+."""
    return InputTokensDetails.model_validate(
        {
            "cached_tokens": cached_tokens or 0,
            "cache_write_tokens": cache_write_tokens or 0,
        }
    )


def _cached_tokens(details: Any | None) -> int:
    """Read cached tokens from provider details, defaulting missing values to zero."""
    return getattr(details, "cached_tokens", 0) or 0


def _cache_write_tokens(details: Any | None) -> int:
    """Read cache-write tokens across OpenAI Python versions."""
    return getattr(details, "cache_write_tokens", 0) or 0


def _coerce_input_token_details(raw_value: Any) -> InputTokensDetails:
    """Deserialize input details while accepting snapshots written before cache writes."""
    candidate = raw_value
    if isinstance(candidate, list) and candidate:
        candidate = candidate[0]
    if isinstance(candidate, Mapping):
        candidate = {
            **candidate,
            "cache_write_tokens": candidate.get("cache_write_tokens", 0) or 0,
        }
    try:
        return TypeAdapter(InputTokensDetails).validate_python(candidate)
    except ValidationError:
        return _make_input_tokens_details()


def deserialize_usage(usage_data: Mapping[str, Any]) -> Usage:
    """Rebuild a Usage object from serialized JSON data."""
    input_tokens_details_raw = usage_data.get("input_tokens_details")
    output_tokens_details_raw = usage_data.get("output_tokens_details")
    input_details = _coerce_input_token_details(input_tokens_details_raw)
    output_details = _coerce_token_details(
        TypeAdapter(OutputTokensDetails),
        output_tokens_details_raw or {"reasoning_tokens": 0},
        OutputTokensDetails(reasoning_tokens=0),
    )

    request_entries: list[RequestUsage] = []
    request_entries_raw = usage_data.get("request_usage_entries") or []
    for entry in request_entries_raw:
        request_entries.append(
            RequestUsage(
                input_tokens=entry.get("input_tokens", 0),
                output_tokens=entry.get("output_tokens", 0),
                total_tokens=entry.get("total_tokens", 0),
                input_tokens_details=_coerce_input_token_details(entry.get("input_tokens_details")),
                output_tokens_details=_coerce_token_details(
                    TypeAdapter(OutputTokensDetails),
                    entry.get("output_tokens_details") or {"reasoning_tokens": 0},
                    OutputTokensDetails(reasoning_tokens=0),
                ),
            )
        )

    return Usage(
        requests=usage_data.get("requests", 0),
        input_tokens=usage_data.get("input_tokens", 0),
        output_tokens=usage_data.get("output_tokens", 0),
        total_tokens=usage_data.get("total_tokens", 0),
        input_tokens_details=input_details,
        output_tokens_details=output_details,
        request_usage_entries=request_entries,
    )


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


def _normalize_input_tokens_details(
    v: InputTokensDetails | PromptTokensDetails | None,
) -> InputTokensDetails:
    """Converts None or PromptTokensDetails to InputTokensDetails."""
    if v is None:
        return _make_input_tokens_details()
    if isinstance(v, PromptTokensDetails):
        return _make_input_tokens_details(
            cached_tokens=v.cached_tokens,
            cache_write_tokens=_cache_write_tokens(v),
        )
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
class Usage:
    requests: int = 0
    """Total requests made to the LLM API."""

    input_tokens: int = 0
    """Total input tokens sent, across all requests."""

    input_tokens_details: Annotated[
        InputTokensDetails, BeforeValidator(_normalize_input_tokens_details)
    ] = field(default_factory=_make_input_tokens_details)
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
        # (cached_tokens, cache_write_tokens, reasoning_tokens), and the OpenAI SDK's generated
        # code can bypass Pydantic validation (e.g., via model_construct),
        # allowing None values. We normalize these to 0 to prevent TypeErrors.
        input_details_none = self.input_tokens_details is None
        input_cached_none = (
            not input_details_none and self.input_tokens_details.cached_tokens is None
        )
        input_cache_write_none = (
            not input_details_none
            and getattr(self.input_tokens_details, "cache_write_tokens", 0) is None
        )
        if input_details_none or input_cached_none or input_cache_write_none:
            self.input_tokens_details = _make_input_tokens_details(
                cached_tokens=_cached_tokens(self.input_tokens_details),
                cache_write_tokens=_cache_write_tokens(self.input_tokens_details),
            )

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
        other_cached = _cached_tokens(other.input_tokens_details)
        other_cache_write = _cache_write_tokens(other.input_tokens_details)
        other_reasoning = (
            other.output_tokens_details.reasoning_tokens
            if other.output_tokens_details and other.output_tokens_details.reasoning_tokens
            else 0
        )
        self_cached = _cached_tokens(self.input_tokens_details)
        self_cache_write = _cache_write_tokens(self.input_tokens_details)
        self_reasoning = (
            self.output_tokens_details.reasoning_tokens
            if self.output_tokens_details and self.output_tokens_details.reasoning_tokens
            else 0
        )

        self.input_tokens_details = _make_input_tokens_details(
            cached_tokens=self_cached + other_cached,
            cache_write_tokens=self_cache_write + other_cache_write,
        )

        self.output_tokens_details = OutputTokensDetails(
            reasoning_tokens=self_reasoning + other_reasoning
        )

        # Automatically preserve request_usage_entries.
        # If the other Usage already has individual request breakdowns, merge them
        # (this preserves nested token details that would otherwise be discarded
        # when synthesizing an entry from only the top-level fields).
        if other.request_usage_entries:
            self.request_usage_entries.extend(other.request_usage_entries)
        elif other.requests == 1 and other.total_tokens > 0:
            # Otherwise, if the other Usage represents a single request with tokens, record it.
            input_details = other.input_tokens_details or _make_input_tokens_details()
            output_details = other.output_tokens_details or OutputTokensDetails(reasoning_tokens=0)
            request_usage = RequestUsage(
                input_tokens=other.input_tokens,
                output_tokens=other.output_tokens,
                total_tokens=other.total_tokens,
                input_tokens_details=input_details,
                output_tokens_details=output_details,
            )
            self.request_usage_entries.append(request_usage)


def _response_usage_to_usage(response_usage: Any) -> Usage:
    """Convert Responses API usage, including adapter-supplied per-request details."""
    request_usages = getattr(response_usage, "_agents_sdk_request_usages", None)
    request_count = getattr(response_usage, "_agents_sdk_request_count", 1)

    if isinstance(request_usages, list):
        usage = Usage()
        for request_usage in request_usages:
            usage.add(
                Usage(
                    requests=1,
                    input_tokens=request_usage.input_tokens,
                    output_tokens=request_usage.output_tokens,
                    total_tokens=request_usage.total_tokens,
                    input_tokens_details=request_usage.input_tokens_details,
                    output_tokens_details=request_usage.output_tokens_details,
                )
            )
        usage.requests = max(usage.requests, request_count)
        return usage

    return Usage(
        requests=request_count,
        input_tokens=response_usage.input_tokens,
        output_tokens=response_usage.output_tokens,
        total_tokens=response_usage.total_tokens,
        input_tokens_details=response_usage.input_tokens_details,
        output_tokens_details=response_usage.output_tokens_details,
    )


def _serialize_usage_details(details: Any, default: dict[str, int]) -> dict[str, Any]:
    """Serialize token details while applying the given default when empty."""
    if hasattr(details, "model_dump"):
        serialized = details.model_dump()
        if isinstance(serialized, dict) and serialized:
            return serialized
    return dict(default)


def _serialize_input_tokens_details(details: Any) -> dict[str, Any]:
    """Serialize both cache-read and cache-write counts across dependency versions."""
    serialized = _serialize_usage_details(details, {"cached_tokens": 0})
    serialized["cached_tokens"] = serialized.get("cached_tokens", 0) or 0
    serialized["cache_write_tokens"] = (
        serialized.get("cache_write_tokens", _cache_write_tokens(details)) or 0
    )
    return serialized


def serialize_usage(usage: Usage) -> dict[str, Any]:
    """Serialize a Usage object into a JSON-friendly dictionary."""
    input_details = _serialize_input_tokens_details(usage.input_tokens_details)
    output_details = _serialize_usage_details(usage.output_tokens_details, {"reasoning_tokens": 0})

    def _serialize_request_entry(entry: RequestUsage) -> dict[str, Any]:
        return {
            "input_tokens": entry.input_tokens,
            "output_tokens": entry.output_tokens,
            "total_tokens": entry.total_tokens,
            "input_tokens_details": _serialize_input_tokens_details(entry.input_tokens_details),
            "output_tokens_details": _serialize_usage_details(
                entry.output_tokens_details, {"reasoning_tokens": 0}
            ),
        }

    return {
        "requests": usage.requests,
        "input_tokens": usage.input_tokens,
        "input_tokens_details": [input_details],
        "output_tokens": usage.output_tokens,
        "output_tokens_details": [output_details],
        "total_tokens": usage.total_tokens,
        "request_usage_entries": [
            _serialize_request_entry(entry) for entry in usage.request_usage_entries
        ],
    }


def model_usage_to_span_usage(usage: Usage) -> dict[str, Any]:
    """Serialize full per-model-call usage for tracing span data."""
    return {
        "requests": usage.requests,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "input_tokens_details": _serialize_input_tokens_details(usage.input_tokens_details),
        "output_tokens_details": _serialize_usage_details(
            usage.output_tokens_details,
            {"reasoning_tokens": 0},
        ),
    }


def total_usage_to_span_metadata(usage: Usage) -> dict[str, int]:
    """Serialize aggregate task/run usage for tracing span metadata."""
    return {
        "requests": usage.requests,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "cached_input_tokens": _cached_input_tokens(usage),
        "cache_write_input_tokens": _cache_write_input_tokens(usage),
    }


def _cached_input_tokens(usage: Usage) -> int:
    return _cached_tokens(usage.input_tokens_details)


def _cache_write_input_tokens(usage: Usage) -> int:
    return _cache_write_tokens(usage.input_tokens_details)


def turn_usage_to_span_data(usage: Usage) -> dict[str, int]:
    """Serialize aggregate per-turn usage for custom turn span data."""
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_input_tokens": _cached_input_tokens(usage),
        "cache_write_input_tokens": _cache_write_input_tokens(usage),
    }


def task_usage_to_span_data(usage: Usage) -> dict[str, int]:
    """Serialize aggregate per-task usage for custom task span data."""
    return {
        **turn_usage_to_span_data(usage),
        "requests": usage.requests,
        "total_tokens": usage.total_tokens,
    }


def _coerce_token_details(adapter: TypeAdapter[Any], raw_value: Any, default: Any) -> Any:
    """Deserialize token details safely with a fallback value."""
    candidate = raw_value
    if isinstance(candidate, list) and candidate:
        candidate = candidate[0]
    try:
        return adapter.validate_python(candidate)
    except ValidationError:
        return default
