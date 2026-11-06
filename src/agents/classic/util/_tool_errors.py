"""Helpers for rendering tool errors in trace-safe form."""

from ._error_tracing import get_trace_error

REDACTED_TOOL_ERROR_MESSAGE = "Tool execution failed. Error details are redacted."


def get_trace_tool_error(*, trace_include_sensitive_data: bool, error_message: str) -> str:
    """Return a trace-safe tool error string based on the sensitive-data setting."""
    return get_trace_error(
        trace_include_sensitive_data=trace_include_sensitive_data,
        error_message=error_message,
        redacted_message=REDACTED_TOOL_ERROR_MESSAGE,
    )
