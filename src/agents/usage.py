from __future__ import annotations

from collections.abc import Mapping
from dataclasses import field
from typing import Annotated, Any

from openai.types.completion_usage import CompletionTokensDetails, PromptTokensDetails
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails
from pydantic import BeforeValidator, TypeAdapter, ValidationError
from pydantic.dataclasses import dataclass


def deserialize_usage(usage_data: Mapping[str, Any]) -> Usage:
    """Rebuild a Usage object from serialized JSON data."""
    input_tokens_details_raw = usage_data.get("input_tokens_details")
    output_tokens_details_raw = usage_data.get("output_tokens_details")
    input_details = _coerce_token_details(
        TypeAdapter(InputTokensDetails),
        input_tokens_details_raw or {"cached_tokens": 0},
        InputTokensDetails(cached_tokens=0),
    )
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
                input_tokens_details=_coerce_token_details(
                    TypeAdapter(InputTokensDetails),
                    entry.get("input_tokens_details") or {"cached_tokens": 0},
                    InputTokensDetails(cached_tokens=0),
                ),
                output_tokens_details=_coerce_token_details(
                    TypeAdapter(OutputTokensDetails),
                    entry.get("output_tokens_details") or {"reasoning_tokens": 0},
                    OutputTokensDetails(reasoning_tokens=0),
                ),
                agent_name=entry.get("agent_name", None),
                model_name=entry.get("model_name", None),
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

    agent_name: str | None = None
    """Name of the agent that made this request, if available.

    Populated automatically when an agent makes a model call so that callers can attribute
    token usage and costs to specific agents in multi-agent workflows.
    """

    model_name: str | None = None
    """Name of the model used for this request, if available.

    Populated automatically when an agent makes a model call so that callers can attribute
    token usage and costs to specific models.
    """


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

    def add(
        self,
        other: Usage,
        *,
        agent_name: str | None = None,
        model_name: str | None = None,
    ) -> None:
        """Add another Usage object to this one, aggregating all fields.

        This method automatically preserves request_usage_entries.

        Args:
            other: The Usage object to add to this one.
            agent_name: Optional name of the agent making this request, used to annotate the
                resulting ``RequestUsage`` entry for per-agent cost attribution.
            model_name: Optional name of the model used for this request, used to annotate the
                resulting ``RequestUsage`` entry for per-model cost attribution.
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
            if other.request_usage_entries:
                # Pre-built entries (e.g. from a prior run) already carry per-request
                # breakdown and attribution. Merge them with the same annotation
                # semantics as the multi-entry path instead of replacing them with a
                # fresh RequestUsage that only reflects add() kwargs.
                for entry in other.request_usage_entries:
                    annotated_entry = RequestUsage(
                        input_tokens=entry.input_tokens,
                        output_tokens=entry.output_tokens,
                        total_tokens=entry.total_tokens,
                        input_tokens_details=entry.input_tokens_details,
                        output_tokens_details=entry.output_tokens_details,
                        agent_name=agent_name
                        if (agent_name is not None and entry.agent_name is None)
                        else entry.agent_name,
                        model_name=model_name
                        if (model_name is not None and entry.model_name is None)
                        else entry.model_name,
                    )
                    self.request_usage_entries.append(annotated_entry)
            else:
                input_details = other.input_tokens_details or InputTokensDetails(cached_tokens=0)
                output_details = other.output_tokens_details or OutputTokensDetails(
                    reasoning_tokens=0
                )
                request_usage = RequestUsage(
                    input_tokens=other.input_tokens,
                    output_tokens=other.output_tokens,
                    total_tokens=other.total_tokens,
                    input_tokens_details=input_details,
                    output_tokens_details=output_details,
                    agent_name=agent_name,
                    model_name=model_name,
                )
                self.request_usage_entries.append(request_usage)
        elif other.request_usage_entries:
            # If the other Usage already has individual request breakdowns, merge them.
            # Apply agent_name/model_name to entries that don't already have them set,
            # but copy each entry rather than mutating the original objects in place
            # to avoid silent mis-attribution when the same Usage is added multiple times.
            for entry in other.request_usage_entries:
                annotated_entry = RequestUsage(
                    input_tokens=entry.input_tokens,
                    output_tokens=entry.output_tokens,
                    total_tokens=entry.total_tokens,
                    input_tokens_details=entry.input_tokens_details,
                    output_tokens_details=entry.output_tokens_details,
                    agent_name=agent_name
                    if (agent_name is not None and entry.agent_name is None)
                    else entry.agent_name,
                    model_name=model_name
                    if (model_name is not None and entry.model_name is None)
                    else entry.model_name,
                )
                self.request_usage_entries.append(annotated_entry)


def _serialize_usage_details(details: Any, default: dict[str, int]) -> dict[str, Any]:
    """Serialize token details while applying the given default when empty."""
    if hasattr(details, "model_dump"):
        serialized = details.model_dump()
        if isinstance(serialized, dict) and serialized:
            return serialized
    return dict(default)


def serialize_usage(usage: Usage) -> dict[str, Any]:
    """Serialize a Usage object into a JSON-friendly dictionary."""
    input_details = _serialize_usage_details(usage.input_tokens_details, {"cached_tokens": 0})
    output_details = _serialize_usage_details(usage.output_tokens_details, {"reasoning_tokens": 0})

    def _serialize_request_entry(entry: RequestUsage) -> dict[str, Any]:
        result: dict[str, Any] = {
            "input_tokens": entry.input_tokens,
            "output_tokens": entry.output_tokens,
            "total_tokens": entry.total_tokens,
            "input_tokens_details": _serialize_usage_details(
                entry.input_tokens_details, {"cached_tokens": 0}
            ),
            "output_tokens_details": _serialize_usage_details(
                entry.output_tokens_details, {"reasoning_tokens": 0}
            ),
        }
        if entry.agent_name is not None:
            result["agent_name"] = entry.agent_name
        if entry.model_name is not None:
            result["model_name"] = entry.model_name
        return result

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
        "input_tokens_details": _serialize_usage_details(
            usage.input_tokens_details,
            {"cached_tokens": 0},
        ),
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
    }


def _cached_input_tokens(usage: Usage) -> int:
    return (
        usage.input_tokens_details.cached_tokens
        if usage.input_tokens_details and usage.input_tokens_details.cached_tokens
        else 0
    )


def turn_usage_to_span_data(usage: Usage) -> dict[str, int]:
    """Serialize aggregate per-turn usage for custom turn span data."""
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_input_tokens": _cached_input_tokens(usage),
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
