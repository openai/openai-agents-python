from __future__ import annotations

from typing import Any

from openai.types.responses import ResponseUsage
from openai.types.responses.response_usage import OutputTokensDetails

from ..usage import _make_input_tokens_details, _response_usage_to_usage


def _usage(total: dict[str, Any]) -> ResponseUsage:
    return ResponseUsage(
        input_tokens=total.get("input_tokens", 0),
        output_tokens=total.get("output_tokens", 0),
        total_tokens=total.get(
            "total_tokens", total.get("input_tokens", 0) + total.get("output_tokens", 0)
        ),
        input_tokens_details=_make_input_tokens_details(
            cached_tokens=total.get("cached_input_tokens", 0),
            cache_write_tokens=total.get("cache_write_input_tokens"),
        ),
        output_tokens_details=OutputTokensDetails(
            reasoning_tokens=total.get("reasoning_output_tokens", 0)
        ),
    )


def _add_sample_usage(previous: ResponseUsage | None, sample: dict[str, Any]) -> ResponseUsage:
    current = _usage(sample)
    if previous is None:
        return current
    total = _response_usage_to_usage(previous)
    total.add(_response_usage_to_usage(current))
    return ResponseUsage(
        input_tokens=total.input_tokens,
        output_tokens=total.output_tokens,
        total_tokens=total.total_tokens,
        input_tokens_details=total.input_tokens_details,
        output_tokens_details=total.output_tokens_details,
    )
