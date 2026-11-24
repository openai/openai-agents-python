"""Typed Decisions and specialist handoffs."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
from typing import Any, Literal, TypeVar, cast

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from . import _debug
from ._decision_schema import _Plan
from ._decision_types import DecisionInput, DecisionProvider, DecisionRequest, DecisionResponse
from ._public_agent import get_public_agent
from ._redaction import current_redactor
from ._run_state_agent_identity import _DecisionHandoff, _iter_agent_graph
from .agent import Agent
from .exceptions import _data_redacted_error_boundary, _mark_error_data_redacted
from .handoffs import Handoff, HandoffInputFilter
from .run_context import RunContextWrapper
from .usage import Usage

__all__ = [
    "DecisionError",
    "DecisionInput",
    "DecisionProvider",
    "DecisionRequest",
    "DecisionResponse",
    "OpenAIDecisions",
    "decision_handoff",
]

_T = TypeVar("_T")
_CHOICE_COUNTS = range(2, 256)


class DecisionError(ValueError):
    """A refused or invalid response. Only metadata is retained."""

    def __init__(
        self,
        reason: Literal["refusal", "invalid_response"],
        *,
        request_id: str | None = None,
        model: str | None = None,
        usage: Usage | None = None,
    ):
        if reason not in ("refusal", "invalid_response"):
            raise ValueError("Unknown decision failure")
        super().__init__("Decision refused" if reason == "refusal" else "Invalid decision response")
        self.reason = reason
        metadata = _metadata(request_id, model, usage)
        self.request_id: str | None = metadata["request_id"]
        self.model: str | None = metadata["model"]
        self.usage: Usage | None = metadata["usage"]


def _usage(value: Any) -> Usage | None:
    if not isinstance(value, (dict, Usage)):
        return None

    def count(name: str) -> Any:
        return value.get(name) if isinstance(value, dict) else getattr(value, name)

    inp, out, total = (count(k) for k in ("input_tokens", "output_tokens", "total_tokens"))
    if any(type(n) is not int or n < 0 for n in (inp, out, total)) or inp + out != total:
        return None
    usage = Usage(requests=1, input_tokens=inp, output_tokens=out, total_tokens=total)
    for field, key, bound in (
        ("input_tokens_details", "cached_tokens", inp),
        ("input_tokens_details", "cache_write_tokens", inp),
        ("output_tokens_details", "reasoning_tokens", out),
    ):
        details = count(field)
        n = details.get(key, 0) if isinstance(details, dict) else getattr(details, key, 0)
        if type(n) is not int or not 0 <= n <= bound:
            return None
        setattr(getattr(usage, field), key, n)
    return usage


def _metadata(request_id: Any, model: Any, usage: Any) -> dict[str, Any]:
    def identifier(v: Any) -> str | None:
        return (
            v
            if current_redactor() is None and type(v) is str and len(v) <= 256 and v.isprintable()
            else None
        )

    return {
        "request_id": identifier(request_id),
        "model": identifier(model),
        "usage": _usage(usage),
    }


def _read_answer(raw: Any, plan: _Plan) -> DecisionResponse | DecisionError:
    request_id = raw.headers.get("x-request-id")
    metadata = _metadata(request_id, None, None)
    try:
        data = raw.http_response.json()
        metadata = _metadata(request_id, data.get("model"), data.get("usage"))
        answers = data["answers"]
        if not isinstance(answers, list) or len(answers) != len(plan.questions):
            return DecisionError("invalid_response", **metadata)
        for question, answer in zip(plan.questions, answers, strict=True):
            if answer["name"] != question["name"]:
                return DecisionError("invalid_response", **metadata)
            if answer["type"] == "refusal":
                return DecisionError("refusal", **metadata)
            if answer["type"] != question["type"]:
                return DecisionError("invalid_response", **metadata)
        return DecisionResponse(value=plan.decode(answers), **metadata)
    except (ValueError, KeyError, AttributeError, TypeError, RecursionError, OverflowError):
        return DecisionError("invalid_response", **metadata)


def _evidence(question: str, input: DecisionInput) -> DecisionInput:
    if not isinstance(question, str) or not question.strip():
        raise ValueError("A nonblank question is required.")
    if not isinstance(input, str):
        if (
            not isinstance(input, Sequence)
            or isinstance(input, (bytes, bytearray))
            or any(not isinstance(v, dict) for v in input)
        ):
            raise TypeError("Decision input must be text or a sequence of input items.")
        input = json.loads(json.dumps(list(input), allow_nan=False))
    return input


class OpenAIDecisions:
    """Use the generated Decisions service without retries. Only owned clients are closed."""

    def __init__(self, *, client: AsyncOpenAI | None = None):
        if client is not None and not isinstance(client, AsyncOpenAI):
            raise TypeError("Expected an AsyncOpenAI client.")
        if not getattr(AsyncOpenAI, "decisions", None):
            raise ValueError("Install an OpenAI client with Decisions support.")
        self._client = client
        self._owned = client is None
        self._closed = False

    async def __aenter__(self) -> OpenAIDecisions:
        if self._closed:
            raise RuntimeError("Decisions provider is closed")
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._closed = True
        if self._owned and self._client is not None:
            await self._client.close()

    async def get_decision(self, request: DecisionRequest) -> DecisionResponse:
        if self._closed:
            raise RuntimeError("Decisions provider is closed")
        evidence = _evidence(request.question, request.input)
        plan = _Plan(request.question, request.output_schema)
        if not plan.questions:
            return DecisionResponse(value=plan.decode([]), usage=Usage())
        if self._client is None:
            self._client = AsyncOpenAI()
        resource = self._client.with_options(max_retries=0).decisions
        response = _read_answer(
            await resource.with_raw_response.create(
                model="gpt-6-luna",
                input=cast(Any, evidence),
                questions=cast(Any, plan.questions),
            ),
            plan,
        )
        if isinstance(response, DecisionError):
            raise response
        return response


def _provider(value: Any) -> DecisionProvider:
    if value is None:
        raise ValueError("Configure Agent.decisions or supply a decision provider.")
    if not callable(getattr(value, "get_decision", None)):
        raise TypeError("Expected a DecisionProvider with get_decision().")
    return cast(DecisionProvider, value)


class _DecisionRun:
    """Snapshot this run's handoff graph. Nested runners capture a separate root."""

    def __init__(self, root: Agent[Any], tracing_disabled: bool, context: object):
        parent = _run.get()
        scope = _scope.get()
        self.parents: tuple[_DecisionRun, ...] = ()
        if parent and scope and scope.delegate:
            agent, owner = scope.delegate
            if agent is root and owner is context:
                scope.delegate = None
                self.parents = (parent, *parent.parents)
        self.tracing_disabled = tracing_disabled
        self.providers: dict[int, tuple[Agent[Any], DecisionProvider | None]] = {}
        self.scopes: set[_Scope] = set()
        self.live = True
        for agent in _iter_agent_graph(root, include_tools=False):
            provider = getattr(agent, "decisions", None)
            if provider is None:
                provider = getattr(root, "decisions", None)
            if any(isinstance(h, _DecisionHandoff) and h._source is agent for h in agent.handoffs):
                _provider(provider)
            self.providers[id(agent)] = agent, provider

    @asynccontextmanager
    async def bind(self) -> AsyncIterator[None]:
        token = _run.set(self)
        callback = _scope.set(None)
        try:
            yield
        finally:
            self.live = False
            _scope.reset(callback)
            _run.reset(token)
            self.providers.clear()
            scopes = tuple(self.scopes)
            for scope in scopes:
                scope.live = False
                scope.cancel(tuple(scope.pending))
            interrupted = False
            for scope in scopes:
                try:
                    await scope.close(tuple(scope.pending))
                except asyncio.CancelledError:
                    interrupted = True
            if interrupted:
                raise asyncio.CancelledError


_run: ContextVar[_DecisionRun | None] = ContextVar("decision_run", default=None)


def _selected(agent: Any) -> DecisionProvider | None:
    run = _run.get()
    if run is None:
        return getattr(agent, "decisions", None)
    entry = run.providers.get(id(agent))
    return entry[1] if entry is not None and entry[0] is agent else None


class _Scope:
    def __init__(self, owner: object, provider: DecisionProvider | None):
        self.delegate: tuple[Agent[Any], object] | None = None
        self.owner = owner
        self.provider = provider
        self.loop = asyncio.get_running_loop()
        self.live = True
        self.pending: dict[asyncio.Task[Any], bool] = {}

    def check(self, owner: object) -> None:
        if (
            not self.live
            or self.owner is not owner
            or self.loop is not asyncio.get_running_loop()
            or _scope.get() is not self
        ):
            raise RuntimeError("Decisions require the exact live callback context.")

    def cancel(self, tasks: tuple[asyncio.Task[Any], ...]) -> None:
        for t in tasks:
            if not t.done() and not self.pending[t]:
                self.pending[t] = True
                t.cancel()

    async def close(self, tasks: tuple[asyncio.Task[Any], ...]) -> None:
        self.cancel(tasks)
        interrupted = False
        for t in tasks:
            while not t.done():
                try:
                    await asyncio.wait((t,))
                except asyncio.CancelledError:
                    interrupted = True
            if not t.cancelled():
                t.exception()
            self.pending.pop(t, None)
        if interrupted:
            raise asyncio.CancelledError


_scope: ContextVar[_Scope | None] = ContextVar("decision_callback", default=None)


def _delegate_decisions(agent: Agent[Any], owner: object, context: object) -> None:
    # Only the nested Runner created by Agent.as_tool shares cancellation ownership.
    scope = _scope.get()
    if scope is not None and scope.owner is owner:
        scope.delegate = (agent, context)


@asynccontextmanager
async def _bind_decisions(owner: object, agent: Any) -> AsyncIterator[None]:
    scope = _Scope(owner, _selected(agent))
    run = _run.get()
    runs = (run, *run.parents) if run is not None else ()
    for active in runs:
        scope.live = scope.live and active.live
        active.scopes.add(scope)
    token = _scope.set(scope)
    try:
        yield
    finally:
        scope.live = False
        _scope.reset(token)
        try:
            await scope.close(tuple(scope.pending))
        finally:
            for active in runs:
                active.scopes.discard(scope)


def _record(response: DecisionResponse | DecisionError) -> None:
    from .tracing import custom_span, get_current_trace

    run = _run.get()
    if (run is not None and run.tracing_disabled) or get_current_trace() is None:
        return
    try:
        metadata = _metadata(response.request_id, response.model, response.usage)
        usage = metadata["usage"]
        metadata["usage_known"] = usage is not None
        metadata["usage"] = (
            None
            if usage is None
            else {k: getattr(usage, k) for k in ("input_tokens", "output_tokens", "total_tokens")}
        )
        metadata["outcome"] = (
            response.reason if isinstance(response, DecisionError) else "completed"
        )
        with custom_span("decision.usage", data=metadata):
            pass
    except Exception:
        pass


def _validate(response: Any, plan: _Plan, adapter: TypeAdapter[Any] | None) -> Any:
    if not isinstance(response, DecisionResponse):
        return DecisionError("invalid_response")
    try:
        plan.validate(response.value)
        if adapter is None:
            return plan.choice(response.value)
        return adapter.validate_json(json.dumps(response.value, allow_nan=False), strict=True)
    except (ValueError, TypeError, RecursionError, OverflowError):
        return DecisionError(
            "invalid_response", **_metadata(response.request_id, response.model, response.usage)
        )


async def _decide(
    owner: object,
    question: str,
    input: DecisionInput,
    provider: DecisionProvider | None,
    choices: Sequence[Any] | None,
    output_type: Any,
    *,
    timeout: float | None = None,
) -> Any:
    scope = _scope.get()
    if isinstance(owner, Agent):
        default = owner.decisions
        scope = None
    else:
        if scope is None:
            raise RuntimeError("Decisions require the exact live callback context.")
        scope.check(owner)
        default = scope.provider
    selected = _provider(default if provider is None else provider)
    input = _evidence(question, input)
    adapter: TypeAdapter[Any] | None = None
    if choices is not None:
        if output_type is not None:
            raise ValueError("choices and output_type are mutually exclusive")
        if (
            not isinstance(choices, Sequence)
            or isinstance(choices, (str, bytes, bytearray))
            or not 1 <= len(choices) <= 255
        ):
            raise ValueError("Expected 1 to 255 distinct choices")
        schema = {"enum": list(choices)}
    else:
        adapter = TypeAdapter(bool if output_type is None else output_type)
        schema = adapter.json_schema()
    plan = _Plan(question, schema)
    request = DecisionRequest(question=question, input=input, output_schema=deepcopy(schema))

    work = scope or _Scope(owner, selected)
    deadline = None if timeout is None else work.loop.time() + timeout
    task = asyncio.create_task(selected.get_decision(request))
    work.pending[task] = False
    try:
        await asyncio.wait((task,), timeout=timeout)
        if not work.live:
            raise asyncio.CancelledError
        if not task.done() or (deadline is not None and work.loop.time() >= deadline):
            raise asyncio.TimeoutError("Decision timed out")
        if task.cancelled():
            raise RuntimeError("Decisions request was cancelled")
        try:
            response = task.result()
            value = _validate(response, plan, adapter)
            _record(value if isinstance(value, DecisionError) else response)
            del response
        except DecisionError as error:
            _record(error)
            if timeout is None:
                _mark_error_data_redacted(error)
            raise
    finally:
        await work.close((task,))
        del task
    if isinstance(value, DecisionError):
        if timeout is None:
            _mark_error_data_redacted(value)
        raise value
    return value


class _RoutingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: str = Field(min_length=1, max_length=8192)


def _routing_input(arguments: str) -> str:
    try:
        value = _RoutingInput.model_validate_json(arguments, strict=True).input
        if value.strip() and len(value.encode()) <= 8192:
            return value
    except ValueError:
        pass
    raise ValueError("Expected nonblank routing input of at most 8192 UTF-8 bytes")


def decision_handoff(
    source: Agent[_T],
    *,
    question: str,
    targets: Mapping[str, Agent[_T]],
    tool_name: str = "select_specialist",
    tool_description: str = "Transfer to a specialist. Supply only evidence needed for routing.",
    is_available: Callable[[RunContextWrapper[_T], str], bool] | None = None,
    timeout: float = 30,
    input_filter: HandoffInputFilter | None = None,
) -> Handoff[_T, Agent[_T]]:
    """Build a handoff for source.handoffs. Decisions chooses a label on a model request.

    Labels bind to concrete agents at construction. Removing or replacing a target
    revokes it. A synchronous is_available callback can further restrict each run.
    The primary model supplies the routing evidence. The optional input_filter
    changes the selected specialist's history only, after the Decisions request.
    Supply fresh context_override on protected reload. Resuming rechecks the
    selected grant without another request. Use separate specialist instances
    for different labels or ordinary handoffs.
    """
    if not isinstance(question, str) or not question.strip():
        raise ValueError("A nonblank question is required.")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if not tool_name.strip() or not targets:
        raise ValueError("Expected a handoff name and targets")
    if any(
        not isinstance(k, str) or not k.strip() or not isinstance(v, Agent)
        for k, v in targets.items()
    ):
        raise ValueError("Expected nonblank labels mapped to agents")
    roster = {label: (agent, agent.name) for label, agent in targets.items()}

    if len(roster) < 2 or (len(roster) > 255 and is_available is None):
        raise ValueError(
            "Decisions requires 2 to 255 available labels. Use handoff(target) for one."
        )
    if any(agent is source or not name.strip() for agent, name in roster.values()) or len(
        {id(agent) for agent, _ in roster.values()}
    ) != len(roster):
        raise ValueError("Create a separate named specialist for each decision label.")

    def admitted(ctx: RunContextWrapper[_T] | None, label: str) -> bool:
        agent, name = roster[label]
        return (
            (is_available is None or (ctx is not None and is_available(ctx, label) is True))
            and targets.get(label) is agent
            and agent.name == name
        )

    def available_choices(ctx: RunContextWrapper[_T]) -> list[str]:
        labels = [label for label in roster if admitted(ctx, label)]
        if len(labels) not in _CHOICE_COUNTS:
            return []
        return labels

    def enabled(ctx: RunContextWrapper[_T], agent: Any) -> bool:
        return get_public_agent(agent) is source and bool(available_choices(ctx))

    def attached() -> bool:
        return (
            route.tool_name == tool_name
            and route.on_invoke_handoff is invoke
            and route.is_enabled is enabled
            and any(h is route for h in source.handoffs)
        )

    def authorized(ctx: RunContextWrapper[_T] | None, label: str) -> bool:
        return admitted(ctx, label) and attached()

    @_data_redacted_error_boundary
    async def invoke(ctx: RunContextWrapper[_T], arguments: str) -> Agent[_T]:
        try:
            return await select(ctx, arguments)
        except (Exception, asyncio.CancelledError) as error:
            if _debug.DONT_LOG_MODEL_DATA or _debug.DONT_LOG_TOOL_DATA:
                _mark_error_data_redacted(error)
            raise

    async def select(ctx: RunContextWrapper[_T], arguments: str) -> Agent[_T]:
        evidence = _routing_input(arguments)
        choices = available_choices(ctx)
        if not choices or not attached():
            raise ValueError("Decision handoff is unavailable")
        async with _bind_decisions(route, source):
            answer = await _decide(route, question, evidence, None, choices, None, timeout=timeout)
        if not authorized(ctx, answer):
            raise ValueError("Selected specialist is unavailable")
        ctx._decision_binding = (route, answer)
        return roster[answer][0]

    route: _DecisionHandoff[_T] = _DecisionHandoff(
        tool_name=tool_name,
        tool_description=tool_description,
        input_json_schema=_RoutingInput.model_json_schema(),
        on_invoke_handoff=invoke,
        input_filter=input_filter,
        agent_name=tool_name,
        is_enabled=enabled,
    )
    route._source = source
    route._roster = roster
    route._can_restore = authorized
    return route
