"""Typed decisions through an application-selected provider."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, Union, cast

from ._lifecycle import _close_after_error
from ._public import preserve_module_identity as _preserve_module_identity
from .schema import OutputValidationError, _prepare_output, _PreparedOutput
from .telemetry import _owned_span
from .usage import TokenUsage

if TYPE_CHECKING:
    from openai import AsyncOpenAI

DecisionInput = str | Sequence[dict[str, Any]]


@dataclass(frozen=True, kw_only=True)
class DecisionRequest:
    """One question, explicit input, and the exact JSON schema a provider must satisfy.

    Non-object output types use the SDK's ``response`` wrapper. Return JSON
    matching ``output_schema``; the typed facade validates and unwraps it.
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
    begin: Callable[[], Awaitable[Callable[[DecisionResponse], Awaitable[None]]]]
    provider: DecisionProvider | None
    task: asyncio.Task[Any] | None
    live: bool = True

    def check(self, owner: object, *, after_await: bool = False) -> None:
        if (
            owner is not self.owner
            or not self.live
            or self.task is not asyncio.current_task()
            or not self.active()
        ):
            if after_await:
                raise asyncio.CancelledError
            raise RuntimeError("decide() requires its exact live callback owner")


_decision_scope: ContextVar[_DecisionScope | None] = ContextVar(
    "agents_decision_scope", default=None
)


@contextmanager
def _bind_decisions(
    owner: object,
    active: Callable[[], bool],
    begin: Callable[[], Awaitable[Callable[[DecisionResponse], Awaitable[None]]]],
    provider: DecisionProvider | None,
) -> Iterator[None]:
    scope = _DecisionScope(owner, active, begin, provider, asyncio.current_task())
    token = _decision_scope.set(scope)
    try:
        yield
    finally:
        scope.live = False
        _decision_scope.reset(token)


def _check_provider(provider: DecisionProvider | None) -> DecisionProvider:
    if provider is None:
        raise ValueError("Configure decisions on the Agent or pass provider to Agent.decide()")
    if not callable(getattr(provider, "get_decision", None)):
        raise TypeError("provider must implement async get_decision(request)")
    return provider


def _capture_decisions(
    default: DecisionProvider | None, override: DecisionProvider | None
) -> DecisionProvider | None:
    selected = default if override is None else override
    return None if selected is None else _check_provider(selected)


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
    return await _decide(
        question,
        input=input,
        provider=_check_provider(provider if provider is not None else scope.provider),
        choices=choices,
        output_type=output_type,
        scope=scope,
    )


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
    adapter, schema, wrapped = _prepare_output(bool if output_type is None else output_type)
    output = _PreparedOutput("native", adapter, schema, wrapped)
    provider = _check_provider(provider)
    request = DecisionRequest(question=question, input=deepcopy(input), output_schema=schema)
    record = None
    if scope is not None:
        scope.check(scope.owner)
        record = await scope.begin()
        scope.check(scope.owner, after_await=True)
    with _owned_span("decision", "operation"):
        try:
            response = await provider.get_decision(request)
        except DecisionError as error:
            if record is not None:
                completed = error._response()
                await _close_after_error(error, lambda: record(completed))
                assert scope is not None
                scope.check(scope.owner, after_await=True)
            raise
        if not isinstance(response, DecisionResponse):
            raise TypeError("get_decision must return DecisionResponse")
        if scope is not None:
            # Retain a delivered completion before checking cancellation or the
            # local output validator. The recorder owns joined publication.
            # Accounting receives only metadata. Failed publication or revoked
            # ownership must not retain the completed value in traceback locals.
            assert record is not None
            try:
                await record(
                    DecisionResponse(
                        value=None,
                        usage=response.usage,
                        request_id=response.request_id,
                        model=response.model,
                    )
                )
                scope.check(scope.owner, after_await=True)
            except BaseException:
                del response
                raise
        try:
            text = json.dumps(response.value, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            text = None
        if text is None:
            failure = DecisionError(
                "invalid_response",
                usage=response.usage,
                request_id=response.request_id,
                model=response.model,
            )
            del response
            raise failure from None
        validated = output.validate(text)
        if choices is not None:
            # Pydantic Literal unions can coerce 0 to False. Match the raw
            # scalar by JSON type before returning the original declared value.
            selected = _choice_key(response.value["response"])
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


class OpenAIDecisions:
    """Answer bounded schemas with Luna, using an optional explicit generative fallback.

    Install the matching SDK build with its ``decisions`` extra for this adapter.
    A supplied AsyncOpenAI client remains caller-owned. Otherwise the adapter
    lazily creates a client; use ``async with`` or ``aclose()`` to close it.
    Closing the adapter rejects later calls. Unsupported schemas select
    ``fallback_model`` before inference. Refusal, malformed answers, transport
    failures and cancellation never retry
    through that fallback. Ordinary HTTP retries follow the client's settings.
    """

    def __init__(
        self, *, client: AsyncOpenAI | None = None, fallback_model: str | None = None
    ) -> None:
        if fallback_model is not None and (
            not isinstance(fallback_model, str) or not fallback_model.strip()
        ):
            raise ValueError("fallback_model must be a nonempty string")
        self._client = client
        self._owned = client is None
        self._fallback_model = fallback_model
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
        questions = compiled["questions"] if compiled["supported"] else None
        if questions is None and self._fallback_model is None:
            raise ValueError(
                "Unsupported Decisions output schema; "
                "configure fallback_model for generative output"
            )
        if questions == []:
            value = json.loads(native._decision_result(schema_json, request.question, "[]"))
            return DecisionResponse(value=value, usage=TokenUsage(0, 0, 0, 0, 0, 0))
        try:
            from openai import AsyncOpenAI, BaseModel as OpenAIObject
        except ImportError:
            raise ImportError("Install the matching SDK build with its decisions extra") from None
        if self._client is None:
            self._client = AsyncOpenAI()
        if questions is None:
            body = {
                "model": self._fallback_model,
                "stream": False,
                "store": False,
                "instructions": request.question,
                "input": request.input,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "decision",
                        "strict": True,
                        "schema": request.output_schema,
                    }
                },
            }
            path = "/responses"
        else:
            body = {"model": "gpt-6-luna", "input": request.input, "questions": questions}
            path = "/decisions"
        # The parser returns before this frame can raise, releasing raw payload
        # aliases (including bound model_dump methods) from the traceback.
        response = _parse_openai_response(
            await self._client.post(path, cast_to=OpenAIObject, body=body),
            native=native,
            schema_json=schema_json,
            question=request.question,
            questions=questions,
        )
        if isinstance(response, DecisionError):
            raise response
        return response


def _parse_openai_response(
    raw: Any,
    *,
    native: Any,
    schema_json: str,
    question: str,
    questions: list[dict[str, Any]] | None,
) -> DecisionResponse | DecisionError:
    """Return metadata-only failures so no payload frame reaches the raised error."""
    reported_request_id = getattr(raw, "_request_id", None)
    request_id = reported_request_id if isinstance(reported_request_id, str) else None
    dump = getattr(raw, "model_dump", None)
    data = dump() if callable(dump) else None
    if not isinstance(data, dict):
        return DecisionError("invalid_response", request_id=request_id)
    usage = _usage(data.get("usage"))
    reported_model = data.get("model")
    model = reported_model if isinstance(reported_model, str) else None
    failure: Literal["refusal", "invalid_response"] | None = None
    value = None
    try:
        if questions is None:
            if data.get("status") != "completed":
                raise ValueError("Responses fallback did not complete")
            texts = []
            for item in data.get("output", []):
                for part in item.get("content", []):
                    if part.get("type") == "refusal":
                        raise DecisionError("refusal")
                    if part.get("type") == "output_text":
                        texts.append(part["text"])
            value = json.loads("".join(texts))
        else:
            answers = data.get("answers")
            if not isinstance(answers, list) or len(answers) != len(questions):
                raise ValueError("Invalid answer count")
            try:
                value = json.loads(
                    native._decision_result(
                        schema_json, question, json.dumps(answers, allow_nan=False)
                    )
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
