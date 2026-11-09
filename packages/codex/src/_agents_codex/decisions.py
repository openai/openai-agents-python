"""Typed decisions through an application-selected provider."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, Union, cast

from ._compat import cancel_once
from ._lifecycle import _close_after_error, _join
from ._public import preserve_module_identity as _preserve_module_identity
from .schema import OutputValidationError, _prepare_output, _PreparedOutput
from .telemetry import TraceOptions, TraceScope, _owned_span, current_context
from .usage import TokenUsage

if TYPE_CHECKING:
    from openai import AsyncOpenAI

DecisionInput = str | Sequence[dict[str, Any]]


@dataclass(frozen=True, kw_only=True)
class DecisionRequest:
    """One question, explicit input, and the exact JSON schema a provider must satisfy.

    Return the application JSON value matching ``output_schema``. Scalar
    schemas and values use their original JSON type without a wrapper.
    Providers must not execute tools or change the owning conversation.
    """

    question: str
    input: DecisionInput = field(repr=False)
    output_schema: dict[str, Any] = field(repr=False)


@dataclass(frozen=True, kw_only=True)
class DecisionResponse:
    """Completed provider output and optional trustworthy response metadata.

    ``value`` is JSON matching the request schema, before local validation.
    Missing usage is unknown, not a zero-token response.
    """

    value: Any = field(repr=False)
    usage: TokenUsage | None = None
    request_id: str | None = None
    model: str | None = None


class DecisionProvider(Protocol):
    """A replaceable provider for one typed decision."""

    async def get_decision(self, request: DecisionRequest) -> DecisionResponse: ...


class DecisionError(ValueError):
    """A completed response refused or could not answer the question.

    The error retains trustworthy metadata, never the raw response. Transport
    and pre-request schema errors retain their own exception types.
    """

    def __init__(
        self,
        reason: Literal["refusal", "invalid_response"],
        *,
        usage: TokenUsage | None = None,
        request_id: str | None = None,
        model: str | None = None,
    ) -> None:
        if reason not in ("refusal", "invalid_response"):
            raise ValueError("Invalid decision failure reason")
        super().__init__("Decision refused" if reason == "refusal" else "Invalid decision response")
        self.reason: Literal["refusal", "invalid_response"] = reason
        self.usage = usage
        self.request_id = request_id
        self.model = model

    def _response(self) -> DecisionResponse:
        return DecisionResponse(
            value=None, usage=self.usage, request_id=self.request_id, model=self.model
        )


@dataclass
class _DecisionScope:
    owner: object
    active: Callable[[], bool]
    provider: DecisionProvider | None
    task: asyncio.Task[Any]
    live: bool = True
    pending: set[asyncio.Task[Any]] = field(default_factory=set)

    def check(self, owner: object, *, after_await: bool = False) -> None:
        if (
            owner is not self.owner
            or not self.live
            or self.task.get_loop() is not asyncio.get_running_loop()
            or not self.active()
        ):
            if after_await:
                raise asyncio.CancelledError
            raise RuntimeError("decide() requires its exact live callback owner")


_decision_scope: ContextVar[_DecisionScope | None] = ContextVar(
    "agents_decision_scope", default=None
)


async def _cancel_decisions(tasks: tuple[asyncio.Task[Any], ...]) -> None:
    for task in tasks:
        cancel_once(task)
    await asyncio.gather(*tasks, return_exceptions=True)
    for task in tasks:
        if not task.cancelled() and (error := task.exception()) is not None:
            raise error


@asynccontextmanager
async def _bind_decisions(
    owner: object,
    active: Callable[[], bool],
    provider: DecisionProvider | None,
) -> AsyncIterator[None]:
    callback_task = asyncio.current_task()
    assert callback_task is not None
    scope = _DecisionScope(owner, active, provider, callback_task)
    token = _decision_scope.set(scope)
    failure: BaseException | None = None
    try:
        yield
    except BaseException as error:
        failure = error
        raise
    finally:
        scope.live = False
        _decision_scope.reset(token)
        pending = tuple(task for task in scope.pending if not task.done())
        if pending:
            if failure is None:
                await _join(asyncio.create_task(_cancel_decisions(pending)))
            else:
                await _close_after_error(failure, lambda: _cancel_decisions(pending))


def _check_provider(provider: DecisionProvider | None) -> DecisionProvider:
    if provider is None:
        raise ValueError("Configure decisions on the Agent or pass provider to Agent.decide()")
    if not callable(getattr(provider, "get_decision", None)):
        raise TypeError("provider must implement async get_decision(request)")
    return provider


async def _bound_decide(
    owner: object,
    question: str,
    *,
    input: DecisionInput,
    provider: DecisionProvider | None = None,
    choices: Sequence[Any] | None = None,
    output_type: Any = None,
) -> Any:
    scope = _decision_scope.get()
    if scope is None:
        raise RuntimeError("decide() requires its exact live callback owner")
    scope.check(owner)
    operation = _decide(
        question,
        input=input,
        provider=_check_provider(provider if provider is not None else scope.provider),
        choices=choices,
        output_type=output_type,
        scope=scope,
    )
    if scope.task is asyncio.current_task():
        return await operation

    # Own the decision itself so cleanup never cancels an application task.
    task = asyncio.create_task(operation)
    scope.pending.add(task)
    try:
        try:
            await asyncio.wait((task,))
        except asyncio.CancelledError as error:
            await _close_after_error(error, lambda: _cancel_decisions((task,)))
            raise
        # Retrieve any error even if the callback closed before this task resumed.
        if not task.cancelled():
            task.exception()
        scope.check(owner, after_await=True)
        return task.result()
    finally:
        scope.pending.discard(task)


def _choice_key(value: Any) -> tuple[str, Any]:
    return ("number", value) if type(value) in (int, float) else (type(value).__name__, value)


async def _decide(
    question: str,
    *,
    input: DecisionInput,
    provider: DecisionProvider,
    choices: Sequence[Any] | None,
    output_type: Any,
    scope: _DecisionScope | None = None,
) -> Any:
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a nonempty string")
    if not isinstance(input, str) and (
        not isinstance(input, Sequence)
        or isinstance(input, (bytes, bytearray))
        or any(not isinstance(item, dict) for item in input)
    ):
        raise TypeError("input must be a string or a sequence of input items")
    if not isinstance(input, str):
        json.dumps(input, allow_nan=False)
    if choices is not None:
        if output_type is not None:
            raise ValueError("choices and output_type are mutually exclusive")
        if isinstance(choices, (str, bytes)) or not isinstance(choices, Sequence) or not choices:
            raise ValueError("choices must be a nonempty sequence of JSON scalar values")
        choices = tuple(choices)
        keys = []
        for value in choices:
            if not (value is None or type(value) in (str, bool, int, float)):
                raise ValueError("choices must contain JSON scalar values")
            json.dumps(value, allow_nan=False)
            key = _choice_key(value)
            if key in keys:
                raise ValueError("choices must contain distinct values")
            keys.append(key)
        if len({type(value) for value in choices}) > 1:
            # Pydantic's mixed Literal enum omits type. Typed alternatives keep
            # the existing strict output-schema admission and scalar identity.
            output_type = cast(Any, Union)[tuple(cast(Any, Literal)[value] for value in choices)]
        else:
            output_type = cast(Any, Literal)[tuple(choices)]
    adapter, schema, wrapped = _prepare_output(
        bool if output_type is None else output_type, wrap_scalars=False
    )
    output = _PreparedOutput("native", adapter, schema, wrapped)
    provider = _check_provider(provider)
    request = DecisionRequest(
        question=question, input=deepcopy(input), output_schema=deepcopy(schema)
    )
    from jsonschema import Draft202012Validator
    from referencing import Registry

    validator = Draft202012Validator(schema, registry=Registry())
    if scope is not None:
        scope.check(scope.owner)
    with _owned_span("decision", "operation"):
        try:
            response = await provider.get_decision(request)
        except DecisionError as error:
            _record_decision_usage(error._response(), outcome=error.reason)
            if scope is not None:
                scope.check(scope.owner, after_await=True)
            raise
        if not isinstance(response, DecisionResponse):
            raise TypeError("get_decision must return DecisionResponse")
        try:
            _record_decision_usage(
                DecisionResponse(
                    value=None,
                    usage=response.usage,
                    request_id=response.request_id,
                    model=response.model,
                )
            )
            if scope is not None:
                scope.check(scope.owner, after_await=True)
        except BaseException:
            del response
            raise
        try:
            text = json.dumps(response.value, allow_nan=False)
            # Validate the same JSON snapshot that the application parser consumes.
            raw_value = json.loads(text)
        except (TypeError, ValueError, RecursionError):
            text = None
            raw_value = None
        if text is None:
            failure = DecisionError(
                "invalid_response",
                usage=response.usage,
                request_id=response.request_id,
                model=response.model,
            )
            del response
            raise failure from None
        if not validator.is_valid(raw_value):
            raise OutputValidationError(text)
        validated = output.validate(text)
        if choices is not None:
            # Pydantic Literal unions can coerce 0 to False. Match the raw
            # scalar by JSON type before returning the original declared value.
            selected = _choice_key(raw_value)
            for choice in choices:
                if _choice_key(choice) == selected:
                    return choice
            raise OutputValidationError(text)
        return validated


def _usage(raw: Any) -> TokenUsage | None:
    if not isinstance(raw, dict):
        return None
    details_in = raw.get("input_tokens_details")
    details_out = raw.get("output_tokens_details")
    if details_in is None:
        details_in = {}
    if details_out is None:
        details_out = {}
    if not isinstance(details_in, dict) or not isinstance(details_out, dict):
        return None
    values = (
        raw.get("input_tokens"),
        details_in.get("cached_tokens", 0),
        details_in.get("cache_write_tokens", 0),
        raw.get("output_tokens"),
        details_out.get("reasoning_tokens", 0),
        raw.get("total_tokens"),
    )
    if any(type(value) is not int or not 0 <= value <= 2**53 - 1 for value in values):
        return None
    counts = cast(tuple[int, int, int, int, int, int], values)
    input_tokens, cached, written, output_tokens, reasoning, total = counts
    if (
        cached > input_tokens
        or written > input_tokens
        or reasoning > output_tokens
        or total != input_tokens + output_tokens
    ):
        return None
    return TokenUsage(*counts)


def _record_decision_usage(response: DecisionResponse, *, outcome: str = "completed") -> None:
    context = current_context()
    if context is None or context.disabled:
        return
    names = (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    counts = (
        {name: getattr(response.usage, name) for name in names}
        if isinstance(response.usage, TokenUsage)
        else None
    )
    if counts is not None and (
        any(type(value) is not int or value < 0 for value in counts.values())
        or counts["cached_input_tokens"] > counts["input_tokens"]
        or counts["cache_write_input_tokens"] > counts["input_tokens"]
        or counts["reasoning_output_tokens"] > counts["output_tokens"]
        or counts["total_tokens"] != counts["input_tokens"] + counts["output_tokens"]
    ):
        counts = None
    metadata: dict[str, Any] = {"usage_known": counts is not None, "outcome": outcome}
    if counts is not None:
        metadata.update(
            {name: value if value <= 2**53 - 1 else str(value) for name, value in counts.items()}
        )
    for name in ("request_id", "model"):
        value = getattr(response, name)
        if isinstance(value, str) and len(value) <= 256 and value.isprintable():
            metadata[name] = value
    options = TraceOptions(metadata=metadata)
    try:
        with TraceScope("decision.usage", "operation", options):
            pass
    except Exception:
        # Optional usage tracing must not change the decision outcome.
        pass


class OpenAIDecisions:
    """Answer supported bounded schemas through the generated Decisions client.

    Install the matching SDK build with its ``decisions`` extra for this adapter.
    A supplied AsyncOpenAI client remains caller-owned. Otherwise the adapter
    lazily creates a client; use ``async with`` or ``aclose()`` to close it.
    Unsupported schemas fail before inference. Transport retries and
    cancellation follow the supplied client's ordinary behavior.
    """

    def __init__(self, *, client: AsyncOpenAI | None = None) -> None:
        self._client = client
        self._owned = client is None
        self._closed = False

    async def __aenter__(self) -> OpenAIDecisions:
        if self._closed:
            raise RuntimeError("Decisions provider is closed")
        return self

    async def __aexit__(self, *args: Any) -> None:
        if args[1] is None:
            await self.aclose()
        else:
            await _close_after_error(args[1], self.aclose)

    async def aclose(self) -> None:
        self._closed = True
        if self._owned and self._client is not None:
            await self._client.close()

    async def get_decision(self, request: DecisionRequest) -> DecisionResponse:
        if self._closed:
            raise RuntimeError("Decisions provider is closed")
        from importlib import import_module

        native = import_module("agents._native")
        schema_json = json.dumps(request.output_schema, allow_nan=False)
        compiled = json.loads(native._decision_questions(schema_json, request.question))
        if not compiled["supported"]:
            raise ValueError("Unsupported Decisions output schema")
        questions = compiled["questions"]
        if questions == []:
            value = json.loads(native._decision_result(schema_json, request.question, "[]"))
            return DecisionResponse(value=value, usage=TokenUsage(0, 0, 0, 0, 0, 0))
        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise ImportError("Install the matching SDK build with its decisions extra") from None
        if self._client is None:
            self._client = AsyncOpenAI()
        response = await _request_openai_decision(
            self._client, request, questions, native=native, schema_json=schema_json
        )
        if isinstance(response, DecisionError):
            raise response
        return response


async def _request_openai_decision(
    client: AsyncOpenAI,
    request: DecisionRequest,
    questions: Any,
    *,
    native: Any,
    schema_json: str,
) -> DecisionResponse | DecisionError:
    """Keep raw response aliases and parser failures outside the raising frame."""
    raw = await client.decisions.with_raw_response.create(
        model="gpt-6-luna", input=cast(Any, request.input), questions=questions
    )
    return _parse_openai_response(
        raw, native=native, schema_json=schema_json, question=request.question, questions=questions
    )


def _parse_openai_response(
    raw: Any,
    *,
    native: Any,
    schema_json: str,
    question: str,
    questions: list[dict[str, Any]],
) -> DecisionResponse | DecisionError:
    """Return metadata-only failures so no payload frame reaches the raised error."""
    reported_request_id = raw.headers.get("x-request-id")
    request_id = reported_request_id if isinstance(reported_request_id, str) else None
    try:
        data = raw.http_response.json()
    except (ValueError, TypeError, RecursionError):
        return DecisionError("invalid_response", request_id=request_id)
    if not isinstance(data, dict):
        return DecisionError("invalid_response", request_id=request_id)
    usage = _usage(data.get("usage"))
    reported_model = data.get("model")
    model = reported_model if isinstance(reported_model, str) else None
    failure: Literal["refusal", "invalid_response"] | None = None
    value = None
    try:
        answers = data.get("answers")
        if not isinstance(answers, list) or len(answers) != len(questions):
            raise ValueError("Invalid answer count")
        try:
            value = json.loads(
                native._decision_result(schema_json, question, json.dumps(answers, allow_nan=False))
            )
        except ValueError as error:
            if str(error) == "Decisions refused a question":
                raise DecisionError("refusal") from None
            raise ValueError("Invalid decision answer") from None
    except DecisionError as error:
        failure = error.reason
    except (
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        StopIteration,
        AttributeError,
        RecursionError,
    ):
        failure = "invalid_response"
    if failure is not None:
        return DecisionError(failure, usage=usage, request_id=request_id, model=model)
    return DecisionResponse(value=value, usage=usage, request_id=request_id, model=model)


_preserve_module_identity(globals(), "agents.decisions")
