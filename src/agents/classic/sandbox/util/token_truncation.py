"""Compatibility aliases for the shared implementation."""

import sys as _sys

from _agents_shared import token_truncation as _implementation
from _agents_shared.token_truncation import (
    APPROX_BYTES_PER_TOKEN as APPROX_BYTES_PER_TOKEN,
    TruncationMode as TruncationMode,
    TruncationPolicy as TruncationPolicy,
    __all__ as __all__,
    _byte_len as _byte_len,
    _truncate_token_output as _truncate_token_output,
    _truncate_utf8 as _truncate_utf8,
    approx_bytes_for_tokens as approx_bytes_for_tokens,
    approx_token_count as approx_token_count,
    approx_tokens_from_byte_count as approx_tokens_from_byte_count,
    assemble_truncated_output as assemble_truncated_output,
    format_truncation_marker as format_truncation_marker,
    formatted_truncate_text as formatted_truncate_text,
    formatted_truncate_text_with_token_count as formatted_truncate_text_with_token_count,
    removed_units_for_source as removed_units_for_source,
    split_budget as split_budget,
    split_string as split_string,
    truncate_text as truncate_text,
    truncate_with_byte_estimate as truncate_with_byte_estimate,
    truncate_with_token_budget as truncate_with_token_budget,
)

_sys.modules[__name__] = _implementation
