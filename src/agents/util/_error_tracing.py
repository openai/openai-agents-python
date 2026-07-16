from typing import Any

from ..logger import logger
from ..tracing import Span, SpanError, get_current_span

REDACTED_TRACE_ERROR_MESSAGE = "Error details are redacted."


def get_trace_error(
    *,
    trace_include_sensitive_data: bool,
    error_message: str,
    redacted_message: str = REDACTED_TRACE_ERROR_MESSAGE,
) -> str:
    """Return a trace-safe error string based on the sensitive-data setting."""
    return error_message if trace_include_sensitive_data else redacted_message


def attach_error_to_span(span: Span[Any], error: SpanError) -> None:
    span.set_error(error)


def attach_error_to_current_span(error: SpanError) -> None:
    span = get_current_span()
    if span:
        attach_error_to_span(span, error)
    else:
        logger.warning("No span to add error %s to", error)
