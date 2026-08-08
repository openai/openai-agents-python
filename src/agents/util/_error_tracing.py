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
    if span is not None:
        attach_error_to_span(span, error)
    elif _debug.DONT_LOG_MODEL_DATA or _debug.DONT_LOG_TOOL_DATA:
        logger.warning("No active span; trace error was not attached")
    else:
        logger.warning("No span to add error %s to", error)


def _model_error_text(error: Exception, *, trace_include_sensitive_data: bool) -> str:
    """Render the span text for a failed model call.

    The exception is only stringified when its text will actually be exported, so a
    provider exception with a side-effecting `__str__` is not invoked just to have
    its output thrown away by redaction.
    """
    if not trace_include_sensitive_data:
        return REDACTED_TRACE_ERROR_MESSAGE
    try:
        return str(error)
    except Exception:
        logger.warning(
            "Could not stringify %s for the model span; recording the type only",
            type(error).__name__,
        )
        return f"Unrenderable {type(error).__name__}"


def record_model_error_on_span(
    span: Span[Any],
    *,
    message: str,
    error: Exception,
    trace_include_sensitive_data: bool,
) -> None:
    """Record an already-known model failure on its span.

    Streaming providers learn about a terminal failure before they raise it, and a
    consumer that stops at that terminal event closes the generator, so the raise
    never happens. Recording at the point of knowledge keeps the span accurate in
    that case. Best-effort: never raises, so annotating a span cannot change what
    the caller sees.
    """
    try:
        attach_error_to_span(
            span,
            SpanError(
                message=message,
                data={
                    "error": _model_error_text(
                        error,
                        trace_include_sensitive_data=trace_include_sensitive_data,
                    )
                },
            ),
        )
    except Exception:
        logger.warning("Could not record the model error on the span", exc_info=True)


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

    Recording is best-effort on purpose: the exception the caller sees is always the
    one the provider raised, never one produced while annotating the span.
    """
    try:
        yield
    except Exception as error:
        record_model_error_on_span(
            span,
            message=message,
            error=error,
            trace_include_sensitive_data=trace_include_sensitive_data,
        )
        raise
