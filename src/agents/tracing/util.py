from .setup import GLOBAL_TRACE_PROVIDER


def time_iso() -> str:
    """Return the current time in ISO 8601 format."""
    return GLOBAL_TRACE_PROVIDER.time_iso()


def gen_trace_id() -> str:
    """Generate a new trace ID."""
    return GLOBAL_TRACE_PROVIDER.gen_trace_id()


def gen_span_id() -> str:
    """Generate a new span ID."""
    return GLOBAL_TRACE_PROVIDER.gen_span_id()


def gen_group_id() -> str:
    """Generate a new group ID."""
    return GLOBAL_TRACE_PROVIDER.gen_group_id()
