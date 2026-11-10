"""Direct application schemas, callback lifetime and generated Decisions transport."""

from __future__ import annotations

import asyncio
import json
import sys
from types import MethodType, SimpleNamespace
from typing import Annotated, Literal

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from _agents_codex.decisions import _bind_decisions
from agents import (
    Agent,
    ChildInvocation,
    ChildStepContext,
    CompletionContext,
    DecisionError,
    DecisionRequest,
    DecisionResponse,
    OpenAIDecisions,
    OutputValidationError,
    ToolContext,
    ToolRef,
    Turn,
)
from agents.tools import ToolCallInfo
from agents.usage import TokenUsage

USAGE = TokenUsage(7, 1, 0, 3, 0, 10)


class Provider:
    def __init__(self, value):
        self.value = value
        self.requests = []

    async def get_decision(self, request):
        self.requests.append(request)
        if isinstance(self.value, Exception):
            raise self.value
        return DecisionResponse(
            value=self.value, usage=USAGE, request_id="req_test", model="custom"
        )


class Assessment(BaseModel):
    category: Literal["accept", "review"]
    approved: bool


def contexts():
    turn = object.__new__(Turn)
    child = ChildInvocation(
        session_id="session", child_id="child", instance_id="instance", turn_id="turn"
    )
    return [
        turn,
        CompletionContext(None, "Proposed text", "session", "turn", None, "generation", 0),
        ChildStepContext(context=None, child=child, step_index=0),
        ToolContext(
            None,
            child,
            "call",
            call=ToolCallInfo(
                tool=ToolRef("lookup"),
                session_id="session",
                turn_id="turn",
                call_id="call",
                child_id="child",
                child_incarnation="instance",
                role=None,
                model=None,
                route="direct",
            ),
        ),
    ]


def assert_no_response_in_sdk_traceback(error, marker):
    def contains(value, seen):
        if id(value) in seen:
            return False
        seen.add(id(value))
        if isinstance(value, str):
            return marker in value
        if isinstance(value, dict):
            return any(contains(item, seen) for pair in value.items() for item in pair)
        if isinstance(value, (list, tuple, set, frozenset)):
            return any(contains(item, seen) for item in value)
        if isinstance(value, MethodType):
            return contains(value.__self__, seen)
        if isinstance(value, (DecisionResponse, SimpleNamespace, BaseModel)):
            return contains(vars(value), seen)
        # Caller-owned clients, providers, and input objects keep their own data.
        return False

    assert error.__cause__ is None and error.__context__ is None
    assert not contains(vars(error), set())
    frames = []
    tb = error.__traceback__
    while tb is not None:
        frame = tb.tb_frame
        if frame.f_globals.get("__name__", "").startswith(("agents.", "_agents_codex.")):
            frames.append(frame.f_code.co_name)
            for name, value in frame.f_locals.items():
                assert not contains(value, set()), (frame.f_code.co_name, name)
        tb = tb.tb_next
    assert frames


async def test_application_schema_and_value_reach_swappable_provider():
    provider = Provider(True)
    agent = Agent(instructions="Answer.", decisions=provider)
    assert await agent.decide("Allowed?", input="Evidence") is True
    assert provider.requests[0].output_schema == {"type": "boolean"}
    assert provider.requests[0].input == "Evidence"
    choice = Provider("review")
    assert (
        await agent.decide("Select", input=[], choices=["accept", "review"], provider=choice)
        == "review"
    )
    assert choice.requests[0].output_schema["enum"] == ["accept", "review"]
    model = Provider({"category": "review", "approved": False})
    assert await agent.decide(
        "Assess", input="Evidence", output_type=Assessment, provider=model
    ) == Assessment(category="review", approved=False)
    assert model.requests[0].output_schema["required"] == ["category", "approved"]
    assert model.requests[0].output_schema["additionalProperties"] is False
    scalar = Provider(0.75)
    assert (
        await agent.decide(
            "Score",
            input="Evidence",
            output_type=Annotated[float, Field(ge=0, le=1)],
            provider=scalar,
        )
        == 0.75
    )
    assert scalar.requests[0].output_schema["type"] == "number"


@pytest.mark.parametrize("value", ["yes", 1, {}, None, {"response": True}])
async def test_boolean_schema_rejects_coercion_and_transport_wrappers(value):
    with pytest.raises(OutputValidationError):
        await Agent(instructions="Assess.", decisions=Provider(value)).decide(
            "Allowed?", input="Evidence"
        )


class _CoercingRecord(dict[str, object]):
    def __getitem__(self, key: str) -> object:
        return True if key == "allow" else super().__getitem__(key)


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"allow": "yes"},
        {"allow": True, "extra": 1},
        pytest.param(_CoercingRecord(allow="yes"), id="serialized-value"),
    ],
)
async def test_required_defaulted_fields_cannot_be_fabricated(value):
    class Gate(BaseModel):
        allow: bool = True

    provider = Provider(value)
    with pytest.raises(OutputValidationError):
        await Agent(instructions="Assess.", decisions=provider).decide(
            "Allowed?", input="Evidence", output_type=Gate
        )
    schema = provider.requests[0].output_schema
    assert schema["required"] == ["allow"] and "default" not in schema["properties"]["allow"]


@pytest.mark.parametrize(
    "options",
    [
        {"question": ""},
        {"choices": []},
        {"choices": [1, 1.0]},
        {"choices": ["a"], "output_type": Assessment},
        {"input": {"arbitrary": "json"}},
        {"input": [{"role": "user", "content": float("nan")}]},
    ],
)
async def test_invalid_requests_fail_before_provider_work(options):
    provider = Provider(None)
    arguments = {"question": "Assess", "input": "Evidence", **options}
    with pytest.raises((TypeError, ValueError)):
        await Agent(instructions="Assess.", decisions=provider).decide(**arguments)
    assert not provider.requests


@pytest.mark.parametrize("value", [False, 0, "0", "false", 7, None])
async def test_choices_preserve_json_scalar_identity(value):
    result = await Agent(instructions="Select.", decisions=Provider(value)).decide(
        "Select", input="Evidence", choices=[False, 0, "0", "false", 7, None]
    )
    assert result == value and type(result) is type(value)


@pytest.mark.parametrize("owner", contexts())
async def test_bound_contexts_use_captured_default_and_expire(owner):
    provider, override = Provider(True), Provider(False)
    async with _bind_decisions(owner, lambda: True, provider):
        assert await owner.decide("Allowed?", input="Evidence") is True
        assert await owner.decide("Allowed?", input="Evidence", provider=override) is False
    with pytest.raises(RuntimeError, match="exact live callback"):
        await owner.decide("Allowed?", input="Evidence")
    assert len(provider.requests) == len(override.requests) == 1


async def test_bound_calls_overlap_and_keep_callback_ownership():
    owner, other = contexts()[:2]
    provider, override = Provider(True), Provider(False)
    release = asyncio.Event()

    async def use_expired_scope():
        await release.wait()
        await owner.decide("Expired?", input="Evidence")

    async with _bind_decisions(owner, lambda: True, provider):
        with pytest.raises(RuntimeError, match="exact live callback"):
            await other.decide("Allowed?", input="Evidence")
        assert await asyncio.gather(
            owner.decide("Allowed?", input="Evidence"),
            owner.decide("Allowed?", input="Evidence", provider=override),
        ) == [True, False]
        late = asyncio.create_task(use_expired_scope())
    release.set()
    with pytest.raises(RuntimeError, match="exact live callback"):
        await late
    assert len(provider.requests) == len(override.requests) == 1


async def test_callback_cancellation_joins_provider_cleanup():
    owner = contexts()[1]
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Blocking:
        async def get_decision(self, request):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                cleaning.set()
                await release.wait()

    async def callback():
        async with _bind_decisions(owner, lambda: True, Blocking()):
            await owner.decide("Allowed?", input="Evidence")

    pending = asyncio.create_task(callback())
    await entered.wait()
    pending.cancel()
    try:
        await cleaning.wait()
        assert not pending.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object()])
async def test_non_json_provider_output_is_sanitized(value):
    with pytest.raises(DecisionError) as caught:
        await Agent(instructions="Assess.", decisions=Provider(value)).decide(
            "Allowed?", input="Evidence"
        )
    assert caught.value.reason == "invalid_response" and caught.value.usage == USAGE
    assert caught.value.__context__ is None and caught.value.__cause__ is None


class Client:
    def __init__(self, data):
        self.data = data
        self.requests = []
        self.closed = False
        self.decisions = SimpleNamespace(with_raw_response=SimpleNamespace(create=self.create))

    async def create(self, **body):
        self.requests.append(body)
        return SimpleNamespace(
            headers={"x-request-id": "req_openai"},
            http_response=SimpleNamespace(json=lambda: self.data),
        )

    async def close(self):
        self.closed = True


@pytest.fixture
def compiler(monkeypatch):
    state = {"supported": True, "error": None, "schemas": []}

    def questions(schema, question):
        state["schemas"].append(json.loads(schema))
        return json.dumps(
            {
                "supported": state["supported"],
                "questions": [{"type": "predicate", "name": "allowed", "instructions": question}],
            }
        )

    def result(schema, question, answers):
        if state["error"]:
            raise ValueError(state["error"])
        return json.dumps(json.loads(answers)[0]["probability"] >= 0.5)

    monkeypatch.setitem(
        sys.modules,
        "agents._native",
        SimpleNamespace(_decision_questions=questions, _decision_result=result),
    )
    return state


def completed():
    return {
        "model": "luna-resolved",
        "answers": [{"probability": 0.8}],
        "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
    }


async def test_adapter_uses_generated_resource_and_leaves_borrowed_client_open(compiler):
    client = Client(completed())
    async with OpenAIDecisions(client=client) as provider:
        assert (
            await Agent(instructions="Assess.", decisions=provider).decide(
                "Allowed?", input="Evidence"
            )
            is True
        )
    assert client.requests[0]["model"] == "gpt-6-luna" and client.requests[0]["input"] == "Evidence"
    assert not client.closed
    with pytest.raises(RuntimeError, match="closed"):
        await provider.get_decision(
            DecisionRequest(
                question="Allowed?", input="Evidence", output_schema={"type": "boolean"}
            )
        )


async def test_unsupported_schema_fails_without_client_or_fallback(compiler):
    compiler["supported"] = False
    client = Client(completed())
    with pytest.raises(ValueError, match="Unsupported Decisions output schema"):
        await Agent(instructions="Assess.", decisions=OpenAIDecisions(client=client)).decide(
            "Explain", input="Evidence", output_type=str
        )
    assert not client.requests
    with pytest.raises(TypeError):
        OpenAIDecisions(fallback_model="removed")
    with pytest.raises(TypeError):
        Agent(instructions="Assess.").session(decisions=Provider(True))
    with pytest.raises(TypeError):
        await Agent(instructions="Assess.").run("Answer", decisions=Provider(True))


@pytest.mark.parametrize(
    "reason,message",
    [("refusal", "Decisions refused a question"), ("invalid_response", "Invalid decision answer")],
)
async def test_completed_errors_keep_only_trustworthy_metadata(compiler, reason, message):
    compiler["error"] = message
    client = Client(completed())
    with pytest.raises(DecisionError) as caught:
        await Agent(instructions="Assess.", decisions=OpenAIDecisions(client=client)).decide(
            "Allowed?", input="Evidence"
        )
    assert caught.value.reason == reason and caught.value.request_id == "req_openai"
    assert caught.value.usage == TokenUsage(7, 0, 0, 3, 0, 10)
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "body,entry",
    [
        pytest.param(b'{"PRIVATE_RESPONSE', "provider", id="malformed-provider"),
        pytest.param(b'{"PRIVATE_RESPONSE', "agent", id="malformed-agent"),
        pytest.param(b'{"PRIVATE_RESPONSE', "bound", id="malformed-bound"),
        pytest.param(b'["PRIVATE_RESPONSE"]', "provider", id="non-object-provider"),
    ],
)
async def test_generated_raw_response_boundary_sanitizes_malformed_200(compiler, body, entry):
    async def handle(request):
        assert request.url.path == "/v1/decisions"
        return httpx2.Response(
            200,
            headers={"x-request-id": "req_safe_200", "content-type": "application/json"},
            content=body,
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http:
        client = AsyncOpenAI(
            api_key="synthetic",
            base_url="https://synthetic.invalid/v1",
            http_client=http,
            max_retries=0,
        )
        provider = OpenAIDecisions(client=client)
        with pytest.raises(DecisionError) as caught:
            if entry == "provider":
                await provider.get_decision(
                    DecisionRequest(
                        question="Allowed?", input="Evidence", output_schema={"type": "boolean"}
                    )
                )
            elif entry == "bound":
                owner = contexts()[1]
                async with _bind_decisions(owner, lambda: True, provider):
                    await owner.decide("Allowed?", input="Evidence")
            else:
                await Agent(instructions="Assess.", decisions=provider).decide(
                    "Allowed?", input="Evidence"
                )
    assert caught.value.request_id == "req_safe_200" and caught.value.reason == "invalid_response"
    assert_no_response_in_sdk_traceback(caught.value, "PRIVATE_RESPONSE")


@pytest.mark.parametrize("failure", ["http", "timeout"])
async def test_generated_resource_preserves_normal_http_errors(compiler, failure):
    from openai import APITimeoutError, AuthenticationError

    async def handle(request):
        if failure == "timeout":
            raise httpx2.ReadTimeout("Synthetic timeout", request=request)
        return httpx2.Response(401, json={"error": {"message": "synthetic rejection"}})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http:
        client = AsyncOpenAI(
            api_key="synthetic",
            base_url="https://synthetic.invalid/v1",
            http_client=http,
            max_retries=0,
        )
        with pytest.raises(AuthenticationError if failure == "http" else APITimeoutError):
            await Agent(instructions="Assess.", decisions=OpenAIDecisions(client=client)).decide(
                "Allowed?", input="Evidence"
            )


async def test_owned_client_is_lazy_and_closes_after_primary_failure(compiler, monkeypatch):
    import openai

    client = Client(completed())
    created = []

    def create():
        created.append(True)
        return client

    monkeypatch.setattr(openai, "AsyncOpenAI", create)
    primary = ValueError("synthetic application failure")
    with pytest.raises(ValueError) as caught:
        async with OpenAIDecisions() as provider:
            assert not created
            assert (
                await Agent(instructions="Assess.", decisions=provider).decide(
                    "Allowed?", input="Evidence"
                )
                is True
            )
            raise primary
    assert caught.value is primary and client.closed and len(created) == 1


@pytest.mark.parametrize("details", [None, [], "invalid", {}, {"cached_tokens": -1}])
async def test_invalid_usage_stays_unknown(compiler, details):
    data = completed()
    data["usage"]["input_tokens_details"] = details
    client = Client(data)
    response = await OpenAIDecisions(client=client).get_decision(
        DecisionRequest(question="Allowed?", input="Evidence", output_schema={"type": "boolean"})
    )
    if details is None or details == {}:
        assert response.usage == TokenUsage(7, 0, 0, 3, 0, 10)
    else:
        assert response.usage is None


async def test_provider_schema_adaptation_does_not_weaken_private_validation():
    class Gate(BaseModel):
        allow: bool = True

    class Adapting:
        async def get_decision(self, request):
            request.output_schema["required"].remove("allow")
            return DecisionResponse(value={})

    with pytest.raises(OutputValidationError):
        await Agent(instructions="Assess.", decisions=Adapting()).decide(
            "Allowed?", input="Evidence", output_type=Gate
        )


@pytest.mark.parametrize("failure", ["decision", "application", "cancellation"])
async def test_owned_decisions_cleanup_preserves_primary_failure(compiler, monkeypatch, failure):
    import openai

    primary = (
        ValueError("Application failed")
        if failure == "application"
        else asyncio.CancelledError("Application cancelled")
    )
    cleanup_error = RuntimeError("Owned client cleanup failed")
    client = Client({"answers": [{"probability": 1}]})
    closed = []

    async def close():
        closed.append(True)
        raise cleanup_error

    client.close = close
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda: client)
    if failure == "decision":
        compiler["error"] = "Decisions refused a question"
    with pytest.raises(BaseException) as caught:
        async with OpenAIDecisions() as provider:
            try:
                await Agent(instructions="Assess", decisions=provider).decide(
                    "Allowed?", input="Synthetic evidence"
                )
                raise primary
            except BaseException as error:
                primary = error
                raise
    assert caught.value is primary
    assert caught.value.__context__ is cleanup_error
    assert closed == [True] and provider._closed
    assert len(client.requests) == 1


async def test_owned_decisions_normal_exit_surfaces_cleanup_failure(compiler, monkeypatch):
    import openai

    failure = RuntimeError("Owned client cleanup failed")
    client = Client({"answers": [{"probability": 1}]})

    async def close():
        raise failure

    client.close = close
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda: client)
    with pytest.raises(RuntimeError) as caught:
        async with OpenAIDecisions() as provider:
            assert await Agent(instructions="Assess", decisions=provider).decide(
                "Allowed?", input="Synthetic evidence"
            )
    assert caught.value is failure
    assert provider._closed


async def test_owned_decisions_primary_failure_joins_cancelled_cleanup(compiler, monkeypatch):
    import openai

    primary = ValueError("Application failed")
    cleanup_error = RuntimeError("Owned client cleanup failed")
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    client = Client({"answers": [{"probability": 1}]})

    async def close():
        entered.set()
        await release.wait()
        finished.set()
        raise cleanup_error

    client.close = close
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda: client)

    async def invoke():
        async with OpenAIDecisions() as provider:
            await Agent(instructions="Assess", decisions=provider).decide(
                "Allowed?", input="Synthetic evidence"
            )
            raise primary

    task = asyncio.create_task(invoke())
    try:
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not finished.is_set()
        release.set()
        with pytest.raises(ValueError) as caught:
            await task
        assert caught.value is primary and caught.value.__context__ is cleanup_error
        assert finished.is_set() and len(client.requests) == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("sync_wrapper", [False, True])
async def test_existing_callback_executor_keeps_scope_in_actual_callback(sync_wrapper):
    from agents import Session

    owner = contexts()[1]
    first, override = Provider(True), Provider(False)
    session = object.__new__(Session)
    session._decisions_provider = first
    session._closing = False
    state = SimpleNamespace(stopping=False, failure=asyncio.get_running_loop().create_future())

    async def body(ctx):
        assert await ctx.decide("Allowed?", input="Evidence") is True
        return await ctx.decide("Allowed?", input="Evidence", provider=override)

    callback = (lambda ctx: body(ctx)) if sync_wrapper else body
    assert (
        await session._call_decision_callback(state, {"step_id": 7}, callback, lambda: True, owner)
        is False
    )
    assert len(first.requests) == len(override.requests) == 1
    with pytest.raises(RuntimeError, match="exact live callback"):
        await owner.decide("Expired?", input="Evidence", provider=first)


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("failure", ["non-json", "depth"])
async def test_non_json_error_tracebacks_discard_completed_value(monkeypatch, bound, failure):
    marker = "PRIVATE_NON_JSON_RESPONSE_MARKER"
    provider = Provider({marker} if failure == "non-json" else marker)
    if failure == "depth":
        encode = json.dumps

        def fail_completed(value, *args, **kwargs):
            if value is provider.value:
                raise RecursionError(marker)
            return encode(value, *args, **kwargs)

        monkeypatch.setattr(json, "dumps", fail_completed)
    with pytest.raises(DecisionError) as caught:
        if bound:
            owner = contexts()[1]
            async with _bind_decisions(owner, lambda: True, provider):
                await owner.decide("Allowed?", input="Evidence")
        else:
            await Agent(instructions="Assess.", decisions=provider).decide(
                "Allowed?", input="Evidence"
            )
    assert caught.value.usage == USAGE and caught.value.request_id == "req_test"
    assert_no_response_in_sdk_traceback(caught.value, marker)


def test_direct_decision_schema_keeps_generation_scalar_wrapper():
    from _agents_codex.schema import _prepare_output

    adapter, schema, wrapped, value_adapter = _prepare_output(bool)
    assert wrapped and schema["properties"]["response"]["type"] == "boolean"
    assert adapter.validate_json('{"response":true}').response is True
    assert value_adapter.validate_json("true") is True


def fail_usage_span(monkeypatch, error, phase):
    from _agents_codex import decisions as implementation

    class FailingScope:
        def __init__(self, name, kind, options):
            assert name == "decision.usage" and kind == "operation"
            if phase == "construct":
                raise error

        def __enter__(self):
            if phase == "enter":
                raise error
            return self

        def __exit__(self, *args):
            if phase == "exit":
                raise error

    monkeypatch.setattr(implementation, "current_context", lambda: SimpleNamespace(disabled=False))
    monkeypatch.setattr(implementation, "TraceScope", FailingScope)


@pytest.mark.parametrize("phase", ["construct", "enter", "exit"])
async def test_optional_usage_span_failure_preserves_success(monkeypatch, phase):
    fail_usage_span(monkeypatch, RuntimeError("Synthetic tracing failure"), phase)
    result = await Agent(instructions="Assess.", decisions=Provider(True)).decide(
        "Allowed?", input="Evidence"
    )
    assert result is True


@pytest.mark.parametrize("phase", ["construct", "enter", "exit"])
async def test_optional_usage_span_failure_preserves_completed_error(monkeypatch, phase):
    primary = DecisionError("refusal", usage=USAGE, request_id="req-refused")
    original_context = ValueError("Earlier safe context")
    primary.__context__ = original_context
    fail_usage_span(monkeypatch, RuntimeError("Synthetic tracing failure"), phase)
    with pytest.raises(DecisionError) as caught:
        await Agent(instructions="Assess.", decisions=Provider(primary)).decide(
            "Allowed?", input="Evidence"
        )
    assert caught.value is primary and caught.value.__context__ is original_context


@pytest.mark.parametrize("phase", ["construct", "enter", "exit"])
async def test_optional_usage_span_failure_keeps_output_validation(monkeypatch, phase):
    fail_usage_span(monkeypatch, RuntimeError("Synthetic tracing failure"), phase)
    with pytest.raises(OutputValidationError):
        await Agent(instructions="Assess.", decisions=Provider("yes")).decide(
            "Allowed?", input="Evidence"
        )


@pytest.mark.parametrize("phase", ["construct", "enter", "exit"])
@pytest.mark.parametrize("refused", [False, True])
async def test_optional_usage_span_cancellation_propagates(monkeypatch, phase, refused):
    cancellation = asyncio.CancelledError("Synthetic tracing cancellation")
    fail_usage_span(monkeypatch, cancellation, phase)
    provider = Provider(DecisionError("refusal") if refused else True)
    with pytest.raises(asyncio.CancelledError) as caught:
        await Agent(instructions="Assess.", decisions=provider).decide("Allowed?", input="Evidence")
    assert caught.value is cancellation
