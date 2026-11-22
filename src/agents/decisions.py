"""Opt-in, explicit-input decisions and specialist handoffs for Runner."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, Generic, Literal, Protocol, TypeVar, overload

from openai import AsyncOpenAI
from openai.types.responses.response_usage import ResponseUsage
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from ._public_agent import get_public_agent
from ._run_state_agent_identity import _DecisionHandoff
from .agent import Agent
from .handoffs import Handoff
from .run_context import RunContextWrapper
from .usage import Usage, _response_usage_to_usage

__all__ = [
    "DecisionError",
    "DecisionProvider",
    "DecisionRequest",
    "DecisionResult",
    "OpenAIDecisions",
    "decide",
    "decision_handoff",
    "decision_scope",
]

_Input = str | Sequence[dict[str, Any]]
_Scalar = str | bool | int | float | None
_S = TypeVar("_S", bound=_Scalar)
_T = TypeVar("_T")
_json: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


@dataclass(frozen=True)
class DecisionRequest:
    """One question, explicit evidence and the JSON schema of the application value."""

    question: str = field(repr=False)
    input: _Input = field(repr=False)
    output_schema: dict[str, Any] = field(repr=False)


@dataclass(frozen=True)
class DecisionResult(Generic[_T]):
    """A value with observed metadata. Missing usage is unknown."""

    value: _T = field(repr=False)
    request_id: str | None = None
    model: str | None = None
    usage: Usage | None = None


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
        super().__init__("Decision refused" if reason == "refusal" else "Invalid decision response")
        self.reason = reason
        self.request_id = request_id
        self.model = model
        self.usage = usage


class DecisionProvider(Protocol):
    """Trusted application code returning JSON or raising DecisionError on refusal."""

    async def get_decision(self, request: DecisionRequest) -> DecisionResult[Any]: ...


@dataclass
class _Scope:
    owner: object
    loop: asyncio.AbstractEventLoop
    live: bool = True
    pending: dict[asyncio.Task[Any], bool] = field(default_factory=dict)

    def check(self, owner: object) -> None:
        if (
            not self.live
            or self.owner is not owner
            or _current.get() is not self
            or asyncio.get_running_loop() is not self.loop
        ):
            raise RuntimeError("Decision requires its exact live callback owner")

    async def cancel(self, tasks: Sequence[asyncio.Task[Any]]) -> None:
        # A deadline and callback exit can overlap. Cancel each provider only once.
        for task in tasks:
            if not task.done() and not self.pending[task]:
                self.pending[task] = True
                task.cancel()
        interrupted = False
        while any(not task.done() for task in tasks):
            try:
                await asyncio.wait(tasks)
            except asyncio.CancelledError:
                interrupted = True
        for task in tasks:
            if not task.cancelled():
                task.exception()
        if interrupted:
            raise asyncio.CancelledError


_current: ContextVar[_Scope | None] = ContextVar("decision_scope", default=None)


@asynccontextmanager
async def decision_scope(owner: object) -> AsyncIterator[None]:
    """Bind decisions to a callback owner. Exit revokes and joins outstanding requests."""
    if owner is None:
        raise ValueError("A callback owner is required")
    scope = _Scope(owner, asyncio.get_running_loop())
    token = _current.set(scope)
    try:
        yield
    finally:
        scope.live = False
        _current.reset(token)
        await scope.cancel(tuple(scope.pending))


def _check_request(question: str, timeout: float) -> None:
    if not isinstance(question, str) or not question.strip():
        raise ValueError("A nonblank question is required")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")


def _snapshot(value: Any) -> str:
    return json.dumps(_json.validate_python(value, strict=True), allow_nan=False)


def _key(value: _Scalar) -> tuple[type, _Scalar]:
    # JSON numbers compare by value, independently of booleans.
    return (int if type(value) is float else type(value), value)


@overload
def decide(
    question: str,
    *,
    provider: DecisionProvider,
    input: _Input,
    choices: Sequence[_S],
    owner: object = None,
    timeout: float = 30,
) -> Coroutine[Any, Any, DecisionResult[_S]]: ...


@overload
def decide(
    question: str,
    *,
    provider: DecisionProvider,
    input: _Input,
    output_type: type[_T],
    owner: object = None,
    timeout: float = 30,
) -> Coroutine[Any, Any, DecisionResult[_T]]: ...


@overload
def decide(
    question: str,
    *,
    provider: DecisionProvider,
    input: _Input,
    owner: object = None,
    timeout: float = 30,
) -> Coroutine[Any, Any, DecisionResult[bool]]: ...


def decide(
    question: str,
    *,
    provider: DecisionProvider,
    input: _Input,
    choices: Sequence[_Scalar] | None = None,
    output_type: type[Any] | None = None,
    owner: object = None,
    timeout: float = 30,
) -> Coroutine[Any, Any, DecisionResult[Any]]:
    """Capture explicit evidence now. Await once for a validated value and provider metadata.

    Inside decision_scope, pass the same owner. The timeout in seconds covers the
    provider request. Caller cancellation waits for cooperative provider cleanup.
    Independent cancellation of the provider request raises RuntimeError.
    """
    scope = _current.get()
    if scope is not None:
        scope.check(owner)
    elif owner is not None:
        raise RuntimeError("Decision requires its exact live callback owner")
    _check_request(question, timeout)
    if input == "":
        raise ValueError("Decision input is required")
    if not isinstance(input, str):
        if (
            not isinstance(input, Sequence)
            or not input
            or not all(isinstance(i, dict) for i in input)
        ):
            raise ValueError("Expected text or a nonempty sequence of JSON objects")
        input = json.loads(_snapshot(list(input)))
    if choices is not None:
        if output_type is not None:
            raise ValueError("choices and output_type are mutually exclusive")
        if (
            not choices
            or isinstance(choices, (str, bytes, bytearray))
            or not isinstance(choices, Sequence)
        ):
            raise ValueError("Expected a nonempty sequence of choices")
        values = list(choices)
        if any(type(v) not in (str, int, float, bool, type(None)) for v in values):
            raise ValueError("Choices must be JSON scalars")
        _snapshot(values)
        admitted = {_key(v): v for v in values}
        if len(admitted) != len(values):
            raise ValueError("Choices must be distinct")
        schema: dict[str, Any] = {"enum": values}

        def parse(text: str) -> Any:
            return admitted[_key(json.loads(text))]
    else:
        from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
        from referencing import Registry

        adapter: TypeAdapter[Any] = TypeAdapter(bool if output_type is None else output_type)
        schema = adapter.json_schema()
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, registry=Registry())

        def parse(text: str) -> Any:
            validator.validate(json.loads(text))
            return adapter.validate_json(text, strict=True)

    request = DecisionRequest(question, input, deepcopy(schema))

    async def execute() -> DecisionResult[Any]:
        if scope is not None:
            scope.check(owner)
        loop = asyncio.get_running_loop()
        work = scope or _Scope(None, loop)
        deadline = loop.time() + timeout
        task = asyncio.create_task(provider.get_decision(request))
        work.pending[task] = False
        try:
            await asyncio.wait((task,), timeout=timeout)
            if not work.live:
                raise asyncio.CancelledError
            if not task.done() or loop.time() >= deadline:
                raise asyncio.TimeoutError("Decision timed out")
            if task.cancelled():
                raise RuntimeError("Decision provider cancelled its request")
            result = _validate(task.result(), parse)
        finally:
            try:
                await work.cancel((task,))
            finally:
                del work.pending[task]
                del task
        if isinstance(result, DecisionError):
            raise result
        return result

    return execute()


def _validate(
    response: DecisionResult[Any], parse: Callable[[str], Any]
) -> DecisionResult[Any] | DecisionError:
    if not isinstance(response, DecisionResult):
        return DecisionError("invalid_response")
    try:
        return replace(response, value=parse(_snapshot(response.value)))
    except Exception:
        # Keep parser exceptions and raw values out of the raising frame.
        return DecisionError(
            "invalid_response",
            request_id=response.request_id,
            model=response.model,
            usage=response.usage,
        )


_OPENAI_CHOICE_COUNTS = range(2, 256)


class OpenAIDecisions:
    """Ask a predicate or string/boolean choice with a caller-owned client and no retries."""

    def __init__(self, client: AsyncOpenAI, *, model: str):
        self._client = client
        self._model = model

    async def get_decision(self, request: DecisionRequest) -> DecisionResult[Any]:
        schema = request.output_schema
        question: dict[str, Any] = {"instructions": request.question, "name": "decision"}
        if schema == {"type": "boolean"}:
            question["type"] = "predicate"
        elif (
            set(schema) == {"enum"}
            and len(schema["enum"]) in _OPENAI_CHOICE_COUNTS
            and all(type(value) in (bool, str) for value in schema["enum"])
        ):
            question.update(type="choice", choices=[{"value": v} for v in schema["enum"]])
        else:
            raise ValueError("Expected a predicate or 2 to 255 string or boolean choices")
        resource = getattr(self._client.with_options(max_retries=0), "decisions", None)
        if resource is None:
            raise RuntimeError("The Decisions client requires openai>=3.26.0")
        result = _read_answer(
            await resource.with_raw_response.create(
                model=self._model, input=request.input, questions=[question]
            ),
            question["type"],
        )
        if isinstance(result, DecisionError):
            raise result
        return result


def _read_answer(raw: Any, kind: str) -> DecisionResult[Any] | DecisionError:
    metadata: dict[str, Any] = {"request_id": raw.headers.get("x-request-id")}
    try:
        data = raw.http_response.json()
        if isinstance(data.get("model"), str):
            metadata["model"] = data["model"]
        if data.get("usage") is not None:
            usage = ResponseUsage.model_validate(data["usage"], strict=True, extra="ignore")
            metadata["usage"] = _response_usage_to_usage(usage)
        answers = data["answers"]
        if isinstance(answers, list) and len(answers) == 1 and answers[0]["name"] == "decision":
            answer = answers[0]
            if answer["type"] == "refusal":
                return DecisionError("refusal", **metadata)
            if answer["type"] == kind:
                if kind == "choice":
                    return DecisionResult(answer["choice"], **metadata)
                probability = answer["probability"]
                if type(probability) in (int, float) and 0 <= probability <= 1:
                    return DecisionResult(probability >= 0.5, **metadata)
    except (ValueError, KeyError, AttributeError, TypeError, RecursionError):
        pass
    return DecisionError("invalid_response", **metadata)


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
    provider: DecisionProvider,
    question: str,
    targets: Mapping[str, Agent[_T]],
    tool_name: str = "select_specialist",
    tool_description: str = "Transfer to a specialist. Supply only evidence needed for routing.",
    is_available: Callable[[RunContextWrapper[_T], str], bool] | None = None,
    timeout: float = 30,
) -> Handoff[_T, Agent[_T]]:
    """Build a handoff for source.handoffs. Decisions chooses a label on a model request.

    Labels bind to concrete agents at construction. Removing or replacing a target
    revokes it. A synchronous is_available callback can further restrict each run.
    OpenAIDecisions requires 2 to 255 available labels to offer or invoke the handoff.
    Restoring a specialist from serialized RunState checks its incoming selectors.
    Supply a fresh context_override if any of them uses is_available.
    """
    _check_request(question, timeout)
    if not tool_name.strip() or not targets:
        raise ValueError("Expected a handoff name and targets")
    if any(
        not isinstance(k, str) or not k.strip() or not isinstance(v, Agent)
        for k, v in targets.items()
    ):
        raise ValueError("Expected nonblank labels mapped to agents")
    roster = {label: (agent, agent.name) for label, agent in targets.items()}

    def admitted(ctx: RunContextWrapper[_T] | None, label: str) -> bool:
        agent, name = roster[label]
        return (
            (is_available is None or (ctx is not None and is_available(ctx, label) is True))
            and targets.get(label) is agent
            and agent.name == name
        )

    def available_choices(ctx: RunContextWrapper[_T]) -> list[str]:
        labels = [label for label in roster if admitted(ctx, label)]
        if isinstance(provider, OpenAIDecisions) and len(labels) not in _OPENAI_CHOICE_COUNTS:
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

    async def invoke(ctx: RunContextWrapper[_T], arguments: str) -> Agent[_T]:
        evidence = _routing_input(arguments)
        choices = available_choices(ctx)
        if not choices or not attached():
            raise ValueError("Decision handoff is unavailable")
        async with decision_scope(ctx):
            try:
                result = await decide(
                    question,
                    provider=provider,
                    input=evidence,
                    choices=choices,
                    owner=ctx,
                    timeout=timeout,
                )
            except DecisionError as error:
                if error.usage is not None:
                    ctx.usage.add(error.usage)
                raise
        if result.usage is not None:
            ctx.usage.add(result.usage)
        if not authorized(ctx, result.value):
            raise ValueError("Selected specialist is unavailable")
        return roster[result.value][0]

    route: _DecisionHandoff[_T] = _DecisionHandoff(
        tool_name=tool_name,
        tool_description=tool_description,
        input_json_schema=_RoutingInput.model_json_schema(),
        on_invoke_handoff=invoke,
        agent_name=tool_name,
        is_enabled=enabled,
    )
    route._source = source
    route._roster = roster
    route._can_restore = authorized
    return route
