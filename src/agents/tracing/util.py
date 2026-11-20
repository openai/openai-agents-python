import re
from datetime import datetime

from .setup import get_trace_provider


def _parse_iso_timestamp(value: str) -> tuple[datetime, int]:
    """Parse timezone-aware source time without losing its nanosecond fraction."""
    fraction = re.search(
        r"^\d{4}-\d{2}-\d{2}.\d{2}:\d{2}:\d{2}\.(\d+)(?=Z$|[+-]\d{2}:\d{2}$|$)", value
    )
    # Python 3.10 cannot parse arbitrary fractional precision; retain it separately.
    whole_seconds = value[: fraction.start(1) - 1] + value[fraction.end(1) :] if fraction else value
    parsed = datetime.fromisoformat(whole_seconds.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Source timestamp must include timezone.")
    nanos = int(fraction.group(1).ljust(9, "0")[:9]) if fraction else parsed.microsecond * 1000
    return parsed.replace(microsecond=0), nanos


def time_iso() -> str:
    """Return the current time in ISO 8601 format."""
    return get_trace_provider().time_iso()


def gen_trace_id() -> str:
    """Generate a new trace ID."""
    return get_trace_provider().gen_trace_id()


def gen_span_id() -> str:
    """Generate a new span ID."""
    return get_trace_provider().gen_span_id()


def gen_group_id() -> str:
    """Generate a new group ID."""
    return get_trace_provider().gen_group_id()
