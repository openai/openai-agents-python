from __future__ import annotations

from typing_extensions import TypedDict


class TracingConfig(TypedDict, total=False):
    """Configuration for tracing behavior and export."""

    api_key: str
    """Optional API key used to export traces."""

    include_task_and_turn_spans: bool
    """Whether the runner creates task and turn spans. Defaults to True when omitted."""


def include_task_and_turn_spans(config: TracingConfig | None) -> bool:
    """Resolve whether automatic runner task and turn spans are enabled."""
    return config is None or config.get("include_task_and_turn_spans", True)
