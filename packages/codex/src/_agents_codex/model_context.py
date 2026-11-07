"""Application-owned projections of the context sent to a model."""

from collections.abc import Awaitable as Awaitable, Callable as Callable, Sequence as Sequence
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


@dataclass(frozen=True, eq=False)
class ModelContextSnapshot:
    """Current model context captured by an idle Session.

    ``items`` contains detached Responses-shaped JSON in native context order.
    Editing the JSON does not change the originals used by ``Session.compact``.
    Pass the issued object back to the same Session while its context is current.
    Capture a new snapshot after restoring a Session or replacing its context.
    """

    items: tuple[dict[str, Any], ...] = field(repr=False)


@dataclass(frozen=True)
class ModelRequestContext(Generic[ContextT]):
    """Detached input and identity for one awaited context transformation.

    ``items`` contains native Responses-shaped input items. Return replacement
    items from the callback, or ``None`` to keep the original input. Replacements
    do not rewrite the original history items. Generated replies and compaction
    summaries are recorded normally. Preserve valid tool-call and result
    relationships when filtering items. Tool results may already be truncated
    in these items. To reduce a full result before native truncation and history
    recording, use ``function_tool(after=...)`` or MCP ``transform_result``.
    The existing instruction and tool APIs control base instructions and tool
    definitions separately from these input items.

    The callback also runs for native children, output repair, and model-based
    compaction. Transport retries reuse its result; a new sampling request runs
    it again. Request-only changes do not redact stored history or other logs.
    Native children use their parent Python Session's ``session_id`` and their
    own ``turn_id`` and ``subagent_id``. Output repair reports the identifiers
    of its separate internal Session.
    Async callbacks run on the Session event loop; sync callbacks run in a
    worker. Cancellation joins admitted callback work and discards late results.
    """

    context: ContextT = field(repr=False)
    items: tuple[dict[str, Any], ...] = field(repr=False)
    session_id: str
    turn_id: str
    subagent_id: str | None
    request_id: str
    model: str
    purpose: Literal["turn", "compaction", "output_repair"]


_ContextTransform: TypeAlias = Callable[
    [ModelRequestContext[ContextT]],
    Sequence[dict[str, Any]] | None | Awaitable[Sequence[dict[str, Any]] | None],
]


_preserve_module_identity(globals(), "agents.model_context")
