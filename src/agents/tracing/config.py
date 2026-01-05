from typing_extensions import NotRequired, TypedDict


class TracingConfig(TypedDict, total=False):
    """Configuration for tracing export."""

    api_key: str
