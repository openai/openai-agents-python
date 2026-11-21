from __future__ import annotations

from typing import Any

from openai.types.responses import ResponseUsage
from openai.types.responses.response_usage import OutputTokensDetails

from ..usage import _make_input_tokens_details


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
