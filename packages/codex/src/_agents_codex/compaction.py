"""Application-supplied summaries for native conversation compaction."""

from __future__ import annotations

from collections.abc import Awaitable as Awaitable, Callable as Callable
from dataclasses import dataclass as dataclass, field as field
from typing import (
    TYPE_CHECKING as TYPE_CHECKING,
    Any as Any,
    Generic as Generic,
    Literal as Literal,
    TypeAlias as TypeAlias,
    TypeVar as TypeVar,
    cast as cast,
)

if TYPE_CHECKING:
    from openai import AsyncOpenAI as AsyncOpenAI

from ._public import preserve_module_identity as _preserve_module_identity
from .usage import TokenUsage, _UsageCollector

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


@dataclass(frozen=True)
class ResponsesCompactionContext(CompactionContext[ContextT]):
    """Captured native window and resolved request for a compaction predicate."""

    candidates: tuple[dict[str, Any], ...] = field(repr=False)
    mode: Literal["input", "previous_response_id"]


_ShouldCompact: TypeAlias = Callable[[ResponsesCompactionContext[ContextT]], bool | Awaitable[bool]]


@dataclass(frozen=True)
class ResponsesCompaction(Generic[ContextT]):
    """Use a borrowed Responses client for native conversation compaction.

    The client stays application-owned. Native compaction keeps ownership of
    history, cancellation, and persistence. A custom predicate controls optional
    post-turn compaction; manual and required compaction bypass the predicate.
    Without a predicate, optional compaction starts after ten eligible items.
    ``auto`` uses input unless an acknowledged provider anchor covers that exact
    input. Configuration and callbacks never become checkpoint authority.

    Current native sessions have been supplying input windows. Explicit previous
    response mode has been rejecting without a native acknowledged-window receipt.
    Hosted prompts omitting the model have been requiring an explicit compaction model.
    """

    client: AsyncOpenAI = field(repr=False)
    model: str | None = None
    mode: Literal["auto", "input", "previous_response_id"] = "auto"
    should_compact: _ShouldCompact[ContextT] | None = field(default=None, repr=False)
    _compact: Callable[..., Awaitable[Any]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.model is not None and (not isinstance(self.model, str) or not self.model.strip()):
            raise ValueError("compaction model must be a nonempty string or None")
        if self.mode not in ("auto", "input", "previous_response_id"):
            raise ValueError("invalid compaction mode")
        if self.should_compact is not None and not callable(self.should_compact):
            raise TypeError("should_compact must be callable or None")
        compact = getattr(getattr(self.client, "responses", None), "compact", None)
        if not callable(compact):
            raise TypeError("compaction client must provide responses.compact")
        object.__setattr__(self, "_compact", compact)

    def _context(
        self,
        request: CompactionContext[ContextT],
        *,
        candidates: tuple[dict[str, Any], ...],
        provider_response: dict[str, Any] | None,
        model_omitted: bool,
    ) -> ResponsesCompactionContext[ContextT]:
        acknowledged_id = (
            None if provider_response is None else provider_response.get("response_id")
        )
        if acknowledged_id is not None and (
            not isinstance(acknowledged_id, str) or not acknowledged_id.strip()
        ):
            raise ValueError("Invalid acknowledged compaction input")
        if self.mode == "previous_response_id" and acknowledged_id is None:
            raise ValueError("Compaction requires an acknowledged current provider window")
        actual_model = (None if provider_response is None else provider_response.get("model")) or (
            None if model_omitted else request.model
        )
        model = self.model if self.model is not None else actual_model
        if not isinstance(model, str) or not model.strip():
            raise ValueError(
                "Compaction needs an explicit model when the provider model is unknown"
            )
        return ResponsesCompactionContext(
            context=request.context,
            items=request.items,
            session_id=request.session_id,
            turn_id=request.turn_id,
            subagent_id=request.subagent_id,
            trigger=request.trigger,
            model=model,
            candidates=candidates,
            mode="previous_response_id"
            if self.mode != "input" and acknowledged_id is not None
            else "input",
        )

    async def _window(
        self,
        request: ResponsesCompactionContext[ContextT],
        *,
        provider_response: dict[str, Any] | None,
        observe: Callable[[TokenUsage | None], None],
    ) -> dict[str, Any]:
        """Capture one completed compact result, retaining its entire output."""
        import json

        model = response = value = usage = window = None
        body: dict[str, Any] | None = None
        completed = reported = False
        try:
            model = request.model
            body = {"model": model}
            if request.mode == "previous_response_id":
                if provider_response is None:
                    raise ValueError("Compaction requires an acknowledged current provider window")
                body["previous_response_id"] = provider_response["response_id"]
            else:
                body["input"] = list(request.items)
            response = await self._compact(**body)
            completed = True
            value = (
                response.model_dump(mode="json") if hasattr(response, "model_dump") else response
            )
            usage = _compact_usage(value.get("usage") if isinstance(value, dict) else None)
            reported = True
            observe(usage)
            if not isinstance(value, dict) or not isinstance(value.get("output"), list):
                raise ValueError("Compaction client must return an output window")
            window = {"items": value["output"], "response_id": value.get("id"), "model": model}
            return cast(dict[str, Any], json.loads(json.dumps(window, allow_nan=False)))
        finally:
            if completed and not reported:
                observe(None)
            del self, request, provider_response, observe, response, body, value, window, usage
            del model


def _compact_usage(raw: Any) -> TokenUsage | None:
    if raw is None:
        return None
    try:
        input_details = raw.get("input_tokens_details")
        output_details = raw.get("output_tokens_details")
        cached = None if input_details is None else input_details.get("cached_tokens")
        reasoning = None if output_details is None else output_details.get("reasoning_tokens")
        usage = TokenUsage(
            input_tokens=raw["input_tokens"],
            cached_input_tokens=0 if cached is None else cached,
            cache_write_input_tokens=0,
            output_tokens=raw["output_tokens"],
            reasoning_output_tokens=0 if reasoning is None else reasoning,
            total_tokens=raw["total_tokens"],
        )
        _UsageCollector.capture(usage)
        return usage
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


_preserve_module_identity(globals(), "agents.compaction")
