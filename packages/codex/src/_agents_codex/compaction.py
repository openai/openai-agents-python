"""Application-supplied summaries for native conversation compaction."""

from __future__ import annotations

import asyncio as asyncio
import json as json
from collections.abc import (
    Awaitable as Awaitable,
    Callable as Callable,
    Mapping as Mapping,
    Sequence as Sequence,
)
from copy import deepcopy as deepcopy
from dataclasses import dataclass as dataclass, field as field, replace as replace
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
from .model_context import ModelRequestContext, _ContextTransform
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

    Auto mode has been selecting an acknowledged, unchanged native provider window
    for a matching plain SDK client and account and otherwise supplying input.
    Custom routing and transports have been using input. Explicit previous-response
    mode has been rejecting without that native receipt and matching client scope.
    Hosted prompts omitting the model have been requiring an explicit compaction model.
    """

    client: AsyncOpenAI = field(repr=False)
    model: str | None = None
    mode: Literal["auto", "input", "previous_response_id"] = "auto"
    should_compact: _ShouldCompact[ContextT] | None = field(default=None, repr=False)
    _compact: Callable[..., Awaitable[Any]] = field(init=False, repr=False, compare=False)

    _client_scope: tuple[str, str, str | None, str | None] | None = field(
        init=False, repr=False, compare=False
    )

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
        object.__setattr__(self, "_client_scope", _responses_client_scope(self.client))

    def _matches_provider(self, api_key: str | None, config: Mapping[str, Any]) -> bool:
        scope = self._client_scope
        if scope is None:
            return False
        if _responses_client_scope(self.client) != scope:
            return False
        provider = config.get("model_providers." + str(config.get("model_provider")))
        if not isinstance(provider, dict) or provider.get("query_params"):
            return False
        headers = {name.lower(): value for name, value in provider.get("http_headers", {}).items()}
        if headers.keys() - {"openai-organization", "openai-project"}:
            return False
        return scope == (
            str(provider.get("base_url", "")).rstrip("/"),
            api_key,
            headers.get("openai-organization"),
            headers.get("openai-project"),
        )

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
        original_controls: tuple[dict[str, Any], ...] = (),
    ) -> dict[str, Any]:
        """Capture one completed compact result, retaining its entire output."""
        model = response = value = usage = window = None
        body: dict[str, Any] | None = None
        control_echoes: tuple[tuple[str, dict[str, Any]], ...] = ()
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
                control_echoes = _compaction_control_echoes(original_controls, request.items)
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
            response_id = value.get("id")
            if response_id is not None and (
                not isinstance(response_id, str) or not response_id.strip()
            ):
                raise ValueError("Compaction response ID must be a nonempty string")
            window = {"items": value["output"], "response_id": response_id, "model": model}
            window = cast(dict[str, Any], json.loads(json.dumps(window, allow_nan=False)))
            if control_echoes:
                _restore_compaction_control_echoes(window["items"], control_echoes)
            return window
        finally:
            if completed and not reported:
                observe(None)
            del self, request, provider_response, observe, response, body, value, window, usage
            del model, control_echoes, original_controls


def _compaction_context(
    context: ContextT, session_id: str, message: dict[str, Any]
) -> CompactionContext[ContextT]:
    return CompactionContext(
        context=context,
        items=tuple(message["items"]),
        session_id=session_id,
        turn_id=message["turn_id"],
        subagent_id=message["thread"] if message["instance"] is not None else None,
        model=message["model"],
        trigger=message["trigger"],
    )


def _validate_compaction_summary(summary: Any) -> None:
    if summary is not None:
        if not isinstance(summary, str):
            raise TypeError("before_compact must return a summary string or None")
        if not summary.strip():
            raise ValueError("before_compact summary must not be empty")


async def _run_responses_compaction(
    factory: ResponsesCompaction[ContextT],
    request: CompactionContext[ContextT],
    *,
    policy: bool,
    transform: _ContextTransform[ContextT] | None,
    provider_response: dict[str, Any] | None,
    candidate_indexes: Sequence[int],
    model_omitted: bool,
    step_id: int,
    active: Callable[[], bool],
    invoke: Callable[[Callable[..., Any], Any], Awaitable[Any]],
    matches_provider: Callable[[], bool],
    observe: Callable[[TokenUsage | None], None],
) -> Any:
    """Run borrowed provider work inside the Session-owned awaited callback."""
    provider_response = (
        provider_response
        if transform is None
        and provider_response is not None
        and factory.mode != "input"
        and matches_provider()
        else None
    )
    prepared = factory._context(
        request,
        candidates=tuple(request.items[index] for index in candidate_indexes),
        provider_response=provider_response,
        model_omitted=model_omitted,
    )
    if policy:
        predicate = factory.should_compact
        if predicate is None:
            decision = len(prepared.candidates) >= 10
        else:
            decision = await invoke(predicate, prepared)
        if not active():
            raise asyncio.CancelledError
        if type(decision) is not bool:
            raise TypeError("should_compact must return a bool")
        if decision and prepared.mode == "previous_response_id" and not matches_provider():
            raise ValueError("Compaction client provider scope changed before dispatch")
        return decision
    if not active():
        raise asyncio.CancelledError
    original_controls: tuple[dict[str, Any], ...] = ()
    if prepared.mode == "input" and transform is not None:
        original_controls = deepcopy(_compaction_instruction_messages(prepared.items))
        replacement = await invoke(
            transform,
            ModelRequestContext(
                context=prepared.context,
                items=prepared.items,
                session_id=prepared.session_id,
                turn_id=prepared.turn_id,
                subagent_id=prepared.subagent_id,
                request_id=str(step_id),
                model=prepared.model,
                purpose="compaction",
            ),
        )
        if not active():
            raise asyncio.CancelledError
        if replacement is not None:
            if (
                not isinstance(replacement, Sequence)
                or isinstance(replacement, (str, bytes, bytearray))
                or any(not isinstance(item, Mapping) for item in replacement)
            ):
                raise TypeError("transform_context must return input items or None")
            prepared = replace(prepared, items=tuple(replacement))
    verified_provider_scope = matches_provider()
    if prepared.mode == "previous_response_id" and not verified_provider_scope:
        raise ValueError("Compaction client provider scope changed before dispatch")
    window = await factory._window(
        prepared,
        provider_response=provider_response,
        observe=observe,
        original_controls=original_controls,
    )
    verified_provider_scope = verified_provider_scope and matches_provider()
    return window, verified_provider_scope


def _responses_client_scope(client: Any) -> tuple[str, str, str | None, str | None] | None:
    try:
        from importlib import import_module

        from openai import AsyncOpenAI
        from openai._base_client import AsyncHttpxClientWrapper
        from openai.resources.responses import AsyncResponses

        # No response-store attestation for unknown clients or request rewriting.
        if type(client) is not AsyncOpenAI or type(client._client) is not AsyncHttpxClientWrapper:
            return None
        if (
            type(client.responses) is not AsyncResponses
            or client.responses._client is not client
            or getattr(client.responses.compact, "__func__", None) is not AsyncResponses.compact
        ):
            return None
        # A mounted transport can rewrite the endpoint or account independently.
        if any(transport is not None for transport in client._client._mounts.values()):
            return None
        transport = client._client._transport
        if type(transport).__name__ != "AsyncHTTPTransport" or type(transport).__module__ not in (
            "httpx",
            "httpx2",
            "httpx._transports.default",
            "httpx2._transports.default",
        ):
            return None
        if type(transport) is not getattr(
            import_module(type(transport).__module__), "AsyncHTTPTransport", None
        ):
            return None
        if (
            client._custom_headers
            or client._custom_query
            or client._client._auth is not None
            or client._client.params
            or client._client.cookies
            or set(client._client.headers)
            - {"accept", "accept-encoding", "connection", "user-agent"}
            or any(client._client.event_hooks.values())
            or getattr(client, "_api_key_provider", None) is not None
            or not isinstance(client.api_key, str)
            or not client.api_key
        ):
            return None
        return (
            str(client.base_url).rstrip("/"),
            client.api_key,
            client.organization,
            client.project,
        )
    except (AttributeError, ImportError, TypeError, ValueError):
        return None


def _compaction_instruction_messages(
    items: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    return tuple(
        dict(item)
        for item in items
        if item.get("type") == "message" and item.get("role") in ("system", "developer")
    )


def _compaction_control_echoes(
    original: tuple[dict[str, Any], ...], outbound: Sequence[Mapping[str, Any]]
) -> tuple[tuple[str, dict[str, Any]], ...]:
    if not original:
        return ()
    transformed = _compaction_instruction_messages(outbound)
    if [(item["role"], item.get("id")) for item in original] != [
        (item["role"], item.get("id")) for item in transformed
    ]:
        return ()
    # Capture the outbound representation before entering the borrowed client.
    return tuple(
        (json.dumps(sent, sort_keys=True, allow_nan=False), canonical)
        for canonical, sent in zip(original, transformed, strict=True)
    )


def _restore_compaction_control_echoes(
    items: list[Any], echoes: tuple[tuple[str, dict[str, Any]], ...]
) -> None:
    # Unmatched controls still reach Core's canonical provenance check.
    cursor = 0
    for index, item in enumerate(items):
        if (
            not isinstance(item, dict)
            or item.get("type") != "message"
            or item.get("role") not in ("system", "developer")
        ):
            continue
        fingerprint = json.dumps(item, sort_keys=True, allow_nan=False)
        for position in range(cursor, len(echoes)):
            sent, canonical = echoes[position]
            if sent == fingerprint:
                items[index] = canonical
                cursor = position + 1
                break


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
