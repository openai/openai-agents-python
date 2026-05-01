import uuid

_FAKE_RESPONSES_ID_PREFIX = "__fake__id__"


def make_fake_responses_id() -> str:
    """Return a unique placeholder ID to fill in the `id` field in Responses
    API responses and output items. Intended for creating these Responses objects
    from non-Responses APIs, e.g. the OpenAI Chat Completions API or other LLM providers.

    The returned value is prefixed with ``__fake__id__`` followed by a UUID4, making it both
    recognizable as synthetic and unique across calls.
    """
    return f"{_FAKE_RESPONSES_ID_PREFIX}{uuid.uuid4()}"


def is_fake_responses_id(value: object) -> bool:
    """Return True if *value* is a placeholder ID created by :func:`make_fake_responses_id`."""
    return isinstance(value, str) and value.startswith(_FAKE_RESPONSES_ID_PREFIX)
