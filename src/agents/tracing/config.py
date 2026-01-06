from __future__ import annotations

from typing_extensions import TypedDict


class TracingConfig(TypedDict, total=False):
    """Configuration for tracing export."""

    api_key: str
