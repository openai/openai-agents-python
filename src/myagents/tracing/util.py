import uuid
from datetime import datetime, timezone


def time_iso() -> str:
    """Returns the current time in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


def gen_trace_id() -> str:
    """Generates a new trace ID."""
    return f"trace_{uuid.uuid4().hex}"


def gen_span_id() -> str:
    """Generates a new span ID."""
    return f"span_{uuid.uuid4().hex[:24]}"


def gen_group_id() -> str:
    """Generates a new group ID."""
    return f"group_{uuid.uuid4().hex[:24]}"
