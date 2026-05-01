import uuid

_FAKE_ID_PREFIX = "__fake__id__"


def make_fake_responses_id() -> str:
    """Return a unique placeholder ID for use when a real Responses API ID is unavailable.

    The returned value is prefixed with ``__fake__id__`` followed by a UUID4, making it both
    recognisable as synthetic and unique across calls.
    """
    return f"{_FAKE_ID_PREFIX}{uuid.uuid4()}"


def is_fake_responses_id(value: object) -> bool:
    """Return True if *value* is a placeholder ID created by :func:`make_fake_responses_id`."""
    return isinstance(value, str) and value.startswith(_FAKE_ID_PREFIX)
