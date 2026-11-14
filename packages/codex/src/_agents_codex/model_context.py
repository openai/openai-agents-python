"""Application-owned projections of the context sent to a model."""

from __future__ import annotations

from collections.abc import Awaitable as Awaitable, Callable as Callable, Sequence as Sequence
from dataclasses import dataclass as dataclass, field as field
from typing import (
    TYPE_CHECKING as TYPE_CHECKING,
    Any as Any,
    Generic as Generic,
    Literal as Literal,
    TypeAlias as TypeAlias,
    TypedDict as TypedDict,
    TypeVar as TypeVar,
)

from ._compiled_dataclasses import dataclass as _sdk_dataclass
from ._public import preserve_module_identity as _preserve_module_identity

if TYPE_CHECKING:
    from .agent import Agent

ContextT = TypeVar("ContextT")


@_sdk_dataclass(frozen=True, eq=False)
class ModelContextSnapshot:
    """Current model context captured by an idle Session.

    ``items`` contains detached Responses-shaped JSON in native context order.
    Editing the JSON does not change the originals used by ``Session.compact``.
    Pass the issued object back to the same Session while its context is current.
    Capture a new snapshot after restoring a Session or replacing its context.
    """

    items: tuple[dict[str, Any], ...] = field(repr=False)


@_sdk_dataclass(frozen=True)
class ModelRequestContext(Generic[ContextT]):
    """Detached input and identity for one awaited context transformation.

    ``items`` contains native Responses-shaped input items. Return replacement
    items from the callback, or ``None`` to keep the original input. Return
    ``ModelContextProjection`` to replace application instructions and
    optionally input items together for an ordinary root turn. Replacements
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
    ``agent`` identifies the original application Agent captured for a root
    Session request, including a selected handoff destination. Native child
    requests have no distinct Python Agent and report ``None``.
    ``instructions`` contains the accepted application instruction text for
    this step. It is ``None`` for compaction, hosted prompts that omit local
    instructions, and steps without a captured application instruction owner.
    Native base instructions and provider-specific wire changes are separate.
    These fields observe this transformation's input; later transformations may
    still replace the request. Both fields default to ``None`` for contexts
    constructed by application code.

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
    agent: Agent[Any, Any] | None = field(default=None, repr=False, kw_only=True)
    instructions: str | None = field(default=None, repr=False, kw_only=True)


@dataclass(frozen=True, kw_only=True)
class ModelContextProjection:
    """Project application instructions for one native root turn request.

    The request appends a directive that supersedes earlier accepted application
    text in processing order. Already acknowledged provider context may still
    contain the original text. Pass ``instructions=None`` to clear application
    instructions for this request. ``items=None`` keeps the original input.
    Native system instructions, unrelated
    developer messages, and saved history retain their existing owners.
    Return this only for an ordinary root request with captured application
    instructions. Hosted prompts, compaction, and output repair use their
    existing context callback forms.
    """

    instructions: str | None = field(repr=False)
    items: Sequence[dict[str, Any]] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.instructions is not None and not isinstance(self.instructions, str):
            raise TypeError("instructions must be str or None")


@_sdk_dataclass(frozen=True)
class ModelCompletionContext(Generic[ContextT]):
    """The accepted response of one native model request, before direct tool execution."""

    context: ContextT = field(repr=False)
    items: tuple[dict[str, Any], ...] = field(repr=False)
    usage: dict[str, Any] | None = field(repr=False)
    session_id: str
    turn_id: str
    subagent_id: str | None
    response_id: str
    model: str


_AfterModel: TypeAlias = Callable[[ModelCompletionContext[ContextT]], None | Awaitable[None]]


class _PromptIdentity(TypedDict):
    id: str


class HostedPromptReference(_PromptIdentity, total=False):
    version: str
    variables: dict[str, Any]


HostedPromptSource: TypeAlias = (
    HostedPromptReference
    | Callable[[ModelRequestContext[Any]], HostedPromptReference | Awaitable[HostedPromptReference]]
)


_ContextTransformResult: TypeAlias = Sequence[dict[str, Any]] | ModelContextProjection | None
_ContextTransform: TypeAlias = Callable[
    [ModelRequestContext[ContextT]], _ContextTransformResult | Awaitable[_ContextTransformResult]
]


_preserve_module_identity(globals(), "agents.model_context")
