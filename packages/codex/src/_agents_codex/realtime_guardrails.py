"""Checks of the voice model's streamed text and audio transcripts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from ._public import preserve_module_identity as _preserve_module_identity

if TYPE_CHECKING:
    from .agent import Agent


@dataclass(frozen=True)
class RealtimeGuardrailInput:
    """An immutable text snapshot from one actual provider output item.

    ``context`` is the Session's application context. ``agent`` is the Agent
    that produced this response, even if another Agent is now active.
    """

    text: str
    response_id: str
    item_id: str
    content_index: int | None
    output_index: int | None
    output_kind: Literal["text", "audio_transcript"]
    agent: Agent[Any, Any] | None
    context: Any
    final: bool


@dataclass(frozen=True)
class RealtimeGuardrailResult:
    """A tripwire and optional application-local diagnostic information."""

    tripwire_triggered: bool
    output_info: Any = None

    def __post_init__(self) -> None:
        if type(self.tripwire_triggered) is not bool:
            raise TypeError("tripwire_triggered must be a bool")


@dataclass(frozen=True)
class RealtimeOutputGuardrail:
    """Check streamed voice output, which may already have started playing.

    Only explicit ``feedback`` can be sent to the voice model after a trip.
    Callback exceptions and ``output_info`` stay local. An exception skips
    that check and emits a guardrail event without closing the voice session.
    """

    name: str
    callback: Callable[
        [RealtimeGuardrailInput],
        RealtimeGuardrailResult | Awaitable[RealtimeGuardrailResult],
    ]
    feedback: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Guardrail name must be a nonempty string")
        if not callable(self.callback):
            raise TypeError("Guardrail callback must be callable")
        if self.feedback is not None and (
            not isinstance(self.feedback, str) or not self.feedback.strip()
        ):
            raise ValueError("Guardrail feedback must be a nonempty string or None")


@dataclass(frozen=True)
class RealtimeGuardrailEvent:
    name: str
    input: RealtimeGuardrailInput
    result: RealtimeGuardrailResult | None = None
    error: Exception | None = None


_preserve_module_identity(globals(), "agents.realtime")
