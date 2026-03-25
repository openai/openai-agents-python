from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ReasoningContentSource:
    """The reasoning item being considered for replay into the next request."""

    item: Any
    """The raw reasoning item."""

    origin_model: str | None
    """The model that originally produced the reasoning item, if known."""

    provider_data: Mapping[str, Any]
    """Provider-specific metadata captured on the reasoning item."""


@dataclass
class ReasoningContentReplayContext:
    """Context passed to reasoning-content replay hooks."""

    model: str
    """The model that will receive the next Chat Completions request."""

    base_url: str | None
    """The request base URL, if the SDK knows the concrete endpoint."""

    reasoning: ReasoningContentSource
    """The reasoning item candidate being evaluated for replay."""


ShouldReplayReasoningContent = Callable[[ReasoningContentReplayContext], bool]


def default_should_replay_reasoning_content(context: ReasoningContentReplayContext) -> bool:
    """Return whether the SDK should replay reasoning content by default."""

    if "deepseek" not in context.model.lower():
        return False

    origin_model = context.reasoning.origin_model
    # Replay only when the current request targets DeepSeek and the reasoning item either
    # came from a DeepSeek model or predates provider tracking. This avoids mixing reasoning
    # content from a different model family into the DeepSeek assistant message.
    return (
        origin_model is not None and "deepseek" in origin_model.lower()
    ) or context.reasoning.provider_data == {}


__all__ = [
    "ReasoningContentReplayContext",
    "ReasoningContentSource",
    "ShouldReplayReasoningContent",
    "default_should_replay_reasoning_content",
]
