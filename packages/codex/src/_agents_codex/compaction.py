"""Application-supplied summaries for native conversation compaction."""

from collections.abc import Awaitable as Awaitable, Callable as Callable
from dataclasses import dataclass as dataclass, field as field
from typing import (
    Any as Any,
    Generic as Generic,
    Literal as Literal,
    TypeAlias as TypeAlias,
    TypeVar as TypeVar,
)

from ._public import preserve_module_identity as _preserve_module_identity

ContextT = TypeVar("ContextT")


@dataclass(frozen=True)
class CompactionContext(Generic[ContextT]):
    """Detached conversation input for an awaited ``before_compact`` callback.

    Return a nonempty summary string to replace native summarization, or
    ``None`` to let Codex generate the summary. Codex keeps ownership of
    compaction, including retained user messages, instructions, and history.
    The supplied summary becomes conversation data and is included in saved
    history; it does not replace application instructions.
    Include every detail the application needs from the discarded assistant
    and tool messages, or return ``None`` to use the native summary instead.

    ``items`` contains the active Responses-shaped context before the
    summarization prompt is added. Mutating these detached items has no effect
    on native history. ``trigger`` distinguishes automatic compaction from
    ``Session.compact()``. An explicit ``Session.compact(summary=...)`` bypasses
    this callback. Returning ``None`` allows ``transform_context`` to run on
    the subsequent summarization request.

    Native children use their parent Python Session's ``session_id`` and their
    own ``turn_id`` and ``subagent_id``. Restored sessions use current callbacks
    and application context. Async callbacks run on the Session event loop;
    sync callbacks run in a worker. Cancellation joins admitted callback work
    and discards late results. A callback must not mutate its owning Session.
    Raising an exception fails the compaction instead of using a fallback.
    """

    context: ContextT = field(repr=False)
    items: tuple[dict[str, Any], ...] = field(repr=False)
    session_id: str
    turn_id: str
    subagent_id: str | None
    model: str
    trigger: Literal["auto", "manual"]


_BeforeCompact: TypeAlias = Callable[
    [CompactionContext[ContextT]], str | None | Awaitable[str | None]
]


_preserve_module_identity(globals(), "agents.compaction")
