import contextlib
from collections.abc import Iterator
from typing import Any

from .. import _debug
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
    elif _debug.DONT_LOG_MODEL_DATA or _debug.DONT_LOG_TOOL_DATA:
        logger.warning("No active span; trace error was not attached")
    else:
        logger.warning("No span to add error %s to", error)


@contextlib.contextmanager
def model_span_errors(
    span: Span[Any],
    *,
    message: str,
    trace_include_sensitive_data: bool,
) -> Iterator[None]:
    """Record a failing model call on the span it happened in, then re-raise.

    `Span.__exit__` finishes a span without attaching an exception, so a provider
    that does not annotate its own span exports a failed model call that is
    indistinguishable from a successful one.
    """
    try:
        yield
    except Exception as error:
        attach_error_to_span(
            span,
            SpanError(
                message=message,
                data={
                    "error": get_trace_error(
                        trace_include_sensitive_data=trace_include_sensitive_data,
                        error_message=str(error),
                    )
                },
            ),
        )
        raise
