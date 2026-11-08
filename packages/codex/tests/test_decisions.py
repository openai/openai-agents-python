"""Typed decision calls without a native model run."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from typing import Literal

import pytest
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
    decide,
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
    category: Literal["accept", "review"] = Field(description="Use review for missing evidence")
    approved: bool = Field(description="The customer has verified their identity")


async def test_default_boolean_and_explicit_input_reach_swappable_provider():
    provider = Provider({"response": True})
    assert await decide("Is this allowed?", input="Evidence", provider=provider) is True
    request = provider.requests[0]
    assert request.question == "Is this allowed?"
    assert request.input == "Evidence"
    assert request.output_schema["properties"]["response"]["type"] == "boolean"
    assert request.output_schema["additionalProperties"] is False


async def test_choices_and_pydantic_output_reuse_existing_schema_validation():
    choices = Provider({"response": "technical"})
    assert (
        await decide(
            "Select a specialist", input=[], choices=["billing", "technical"], provider=choices
        )
        == "technical"
    )
    assert choices.requests[0].input == []
    assert choices.requests[0].output_schema["properties"]["response"]["enum"] == [
        "billing",
        "technical",
    ]
    provider = Provider({"category": "review", "approved": False})
    assert await decide(
        "Assess", input="Evidence", output_type=Assessment, provider=provider
    ) == Assessment(category="review", approved=False)
    schema = provider.requests[0].output_schema
    assert (
        schema["properties"]["approved"]["description"]
        == "The customer has verified their identity"
    )
    assert schema["required"] == ["category", "approved"]
    provider.value = {"category": "invented", "approved": True}
    with pytest.raises(OutputValidationError):
        await decide("Assess", input="Evidence", output_type=Assessment, provider=provider)


@pytest.mark.parametrize(
    "options",
    [
        {"question": ""},
        {"choices": []},
        {"choices": ["one", "one"]},
        {"choices": ["one"], "output_type": Assessment},
        {"input": {"arbitrary": "json"}},
    ],
)
async def test_invalid_requests_fail_before_provider_work(options):
    provider = Provider(None)
    values = {"question": "Question", "input": "Evidence", **options}
    with pytest.raises((TypeError, ValueError)):
        await decide(provider=provider, **values)
    assert not provider.requests


async def test_agent_default_and_per_call_override_do_not_start_an_agent_run():
    first, second = Provider({"response": True}), Provider({"response": False})
    agent = Agent(instructions="Answer", decisions=first)
    assert await agent.decide("Allowed?", input="evidence") is True
    assert await agent.decide("Allowed?", input="evidence", provider=second) is False
    assert agent.decisions is first and agent.model is None
    with pytest.raises(FrozenInstanceError):
        agent.decisions = second
    with pytest.raises(ValueError, match="Configure decisions"):
        await Agent(instructions="Answer").decide("Allowed?", input="evidence")


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


@pytest.mark.parametrize("owner", contexts())
async def test_bound_contexts_record_before_validation_and_reject_expiry(owner):
    recorded = []
    live = True

    async def begin():
        async def record(response):
            recorded.append(response)

        return record

    provider = Provider({"response": "invalid"})
    with _bind_decisions(owner, lambda: live, begin, provider):
        with pytest.raises(OutputValidationError):
            await owner.decide("Allowed?", input="evidence")
        assert recorded[0].usage == USAGE
        assert recorded[0].request_id == "req_test"
        live = False
        with pytest.raises(RuntimeError, match="exact live callback"):
            await owner.decide("Allowed?", input="evidence")
    with pytest.raises(RuntimeError, match="exact live callback"):
        await owner.decide("Allowed?", input="evidence")
    assert len(provider.requests) == 1


async def test_bound_known_failure_records_once_and_never_falls_back():
    owner = contexts()[1]
    recorded = []

    async def begin():
        async def record(response):
            recorded.append(response)

        return record

    failure = DecisionError("refusal", usage=USAGE, request_id="refused", model="custom")
    provider = Provider(failure)
    with _bind_decisions(owner, lambda: True, begin, provider):
        with pytest.raises(DecisionError) as caught:
            await owner.decide("Allowed?", input="evidence")
    assert caught.value is failure
    assert recorded == [
        DecisionResponse(value=None, usage=USAGE, request_id="refused", model="custom")
    ]


async def test_bound_calls_require_current_task_and_capture_before_dispatch():
    owner, other = contexts()[:2]
    provider = Provider({"response": True})
    captures = []

    async def begin():
        captures.append("capture")

        async def record(response):
            captures.append("record")

        return record

    with _bind_decisions(owner, lambda: True, begin, provider):
        with pytest.raises(RuntimeError, match="exact live callback"):
            await other.decide("Question", input="evidence")
        with pytest.raises(RuntimeError, match="exact live callback"):
            await asyncio.create_task(owner.decide("Question", input="evidence"))
        assert await owner.decide("Question", input="evidence") is True
    assert captures == ["capture", "record"]
    assert len(provider.requests) == 1


async def test_cancelling_bound_call_joins_provider_cleanup_without_manufactured_completion():
    owner = contexts()[1]
    started, retired = asyncio.Event(), asyncio.Event()
    recorded = []

    class Blocking:
        async def get_decision(self, request):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                retired.set()

    async def begin():
        async def record(response):
            recorded.append(response)

        return record

    async def callback():
        with _bind_decisions(owner, lambda: True, begin, Blocking()):
            await owner.decide("Question", input="evidence")

    task = asyncio.create_task(callback())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert retired.is_set()
    assert recorded == []


class Client:
    def __init__(self, data):
        self.data = data
        self.requests = []
        self.closed = False

    async def post(self, path, *, cast_to, body):
        self.requests.append((path, body))
        return SimpleNamespace(model_dump=lambda: self.data, _request_id="req_openai")

    async def close(self):
        self.closed = True


@pytest.fixture
def compiler(monkeypatch):
    # These adapter tests isolate HTTP selection/metadata from the pure native
    # compiler, whose real artifact has dedicated compiler/integration tests.
    state = {"supported": True, "error": None, "schemas": []}

    def questions(schema, question):
        state["schemas"].append(json.loads(schema))
        return json.dumps(
            {
                "supported": state["supported"],
                "questions": [{"type": "predicate", "name": "field_1", "instructions": question}]
                if state["supported"]
                else [],
            }
        )

    def result(schema, question, answers):
        if state["error"]:
            raise ValueError(state["error"])
        return json.dumps({"response": json.loads(answers)[0]["probability"] >= 0.5})

    monkeypatch.setitem(
        sys.modules,
        "agents._native",
        SimpleNamespace(
            _decision_questions=questions,
            _decision_result=result,
        ),
    )
    return state


async def test_openai_adapter_uses_fixed_backend_and_keeps_borrowed_client_open(compiler):
    client = Client(
        {
            "model": "luna-resolved",
            "answers": [{"probability": 0.8}],
            "usage": {
                "input_tokens": 7,
                "output_tokens": 3,
                "total_tokens": 10,
            },
        }
    )
    async with OpenAIDecisions(client=client) as provider:
        response = await provider.get_decision(
            DecisionRequest(
                question="Allowed?",
                input="evidence",
                output_schema={"type": "boolean"},
            )
        )
    assert response == DecisionResponse(
        value={"response": True},
        usage=TokenUsage(7, 0, 0, 3, 0, 10),
        request_id="req_openai",
        model="luna-resolved",
    )
    assert client.requests[0][0] == "/decisions"
    assert client.requests[0][1]["model"] == "gpt-6-luna"
    assert not client.closed
    with pytest.raises(RuntimeError, match="closed"):
        await provider.decide("Allowed?", input="evidence")


async def test_openai_fallback_is_selected_before_inference_only(compiler):
    compiler["supported"] = False
    client = Client(
        {
            "model": "generation",
            "status": "completed",
            "output": [{"content": [{"type": "output_text", "text": '{"response":"accepted"}'}]}],
        }
    )
    async with OpenAIDecisions(client=client) as no_fallback:
        with pytest.raises(ValueError, match="Unsupported Decisions output schema"):
            await no_fallback.decide("Answer", input="evidence", output_type=str)
    assert not client.requests
    async with OpenAIDecisions(client=client, fallback_model="generation") as provider:
        assert await provider.decide("Answer", input="evidence", output_type=str) == "accepted"
    assert len(client.requests) == 1
    path, body = client.requests[0]
    assert path == "/responses" and body["model"] == "generation"
    assert body["text"]["format"]["schema"]["properties"]["response"]["type"] == "string"


@pytest.mark.parametrize(
    "reason,message",
    [
        ("refusal", "Decisions refused a question"),
        ("invalid_response", "invalid Decisions answer"),
    ],
)
async def test_openai_completed_errors_keep_metadata_without_raw_answers(compiler, reason, message):
    compiler["error"] = message
    client = Client(
        {
            "model": "luna-resolved",
            "answers": [{"secret": "DO_NOT_EXPOSE"}],
            "usage": {
                "input_tokens": 7,
                "output_tokens": 3,
                "total_tokens": 10,
            },
        }
    )
    async with OpenAIDecisions(client=client, fallback_model="generation") as provider:
        with pytest.raises(DecisionError) as caught:
            await provider.decide("Allowed?", input="evidence")
    error = caught.value
    assert error.reason == reason and error.usage.total_tokens == 10
    assert error.request_id == "req_openai" and error.model == "luna-resolved"
    assert "DO_NOT_EXPOSE" not in str(error)
    assert error.__context__ is None
    assert len(client.requests) == 1 and client.requests[0][0] == "/decisions"


async def test_invalid_usage_remains_unknown_and_transport_failure_is_not_a_completion(compiler):
    client = Client(
        {
            "answers": [{"probability": 1}],
            "usage": {
                "input_tokens": -1,
                "output_tokens": 1,
                "total_tokens": 0,
            },
        }
    )
    provider = OpenAIDecisions(client=client)
    response = await provider.get_decision(
        DecisionRequest(question="Allowed?", input="", output_schema={"type": "boolean"})
    )
    assert response.usage is None

    async def fail(*args, **kwargs):
        raise OSError("transport failed")

    client.post = fail
    with pytest.raises(OSError, match="transport failed"):
        await provider.decide("Allowed?", input="evidence")


@pytest.mark.parametrize("entry", ["session", "run", "run_sync"])
@pytest.mark.parametrize("override", [False, True])
async def test_openings_capture_provider_precedence_without_native_acquisition(
    monkeypatch, entry, override
):
    import _agents_codex.session as implementation

    first, second = Provider({"response": True}), Provider({"response": False})
    captured = []
    resolve = implementation._capture_decisions

    class Captured(Exception):
        pass

    def capture(default, selected):
        captured.append(resolve(default, selected))
        raise Captured

    monkeypatch.setattr(implementation, "_capture_decisions", capture)
    agent = Agent(instructions="Answer", decisions=first)
    options = {"decisions": second} if override else {}
    with pytest.raises(Captured):
        if entry == "session":
            async with agent.session(**options):
                pytest.fail("Unexpected native opening")
        elif entry == "run":
            await agent.run("Evidence", **options)
        else:
            await asyncio.to_thread(agent.run_sync, "Evidence", **options)
    assert captured == [second if override else first]
    assert agent.decisions is first


@pytest.mark.parametrize("sync_wrapper", [False, True])
async def test_existing_callback_executor_binds_decision_scope_to_actual_callback(sync_wrapper):
    from agents import Session

    owner = contexts()[1]
    first, override = Provider({"response": True}), Provider({"response": False})
    session = object.__new__(Session)
    session._decisions_provider = first
    recorded = []

    async def begin(state, message):
        assert message == {"step_id": 7}

        async def record(response):
            recorded.append(response)

        return record

    session._begin_decision_response = begin

    async def body(ctx):
        assert await ctx.decide("Allowed?", input="evidence") is True
        return await ctx.decide("Allowed?", input="evidence", provider=override)

    callback = (lambda ctx: body(ctx)) if sync_wrapper else body
    assert (
        await session._call_decision_callback(
            object(),
            {"step_id": 7},
            callback,
            lambda: True,
            owner,
        )
        is False
    )
    assert [response.model for response in recorded] == ["custom", "custom"]
    with pytest.raises(RuntimeError, match="exact live callback"):
        await owner.decide("Allowed?", input="evidence", provider=first)


async def test_owned_client_is_created_lazily_and_closed(compiler, monkeypatch):
    import openai

    created = []
    client = Client({"answers": [{"probability": 1}]})

    def make_client():
        created.append(client)
        return client

    monkeypatch.setattr(openai, "AsyncOpenAI", make_client)
    provider = OpenAIDecisions()
    assert not created
    async with provider:
        assert await provider.decide("Allowed?", input="evidence") is True
    assert created == [client] and client.closed


async def test_public_raw_post_preserves_actual_openai_request_id(compiler):
    import httpx2
    import openai

    requests = []

    async def respond(request):
        requests.append(request)
        return httpx2.Response(
            200,
            headers={"x-request-id": "req_actual"},
            json={
                "model": "luna-resolved",
                "answers": [{"probability": 0.8}],
                "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
            },
        )

    async with openai.AsyncOpenAI(
        api_key="synthetic-decision-key",
        base_url="https://example.invalid/v1",
        http_client=openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(respond)),
    ) as client:
        async with OpenAIDecisions(client=client) as provider:
            result = await provider.get_decision(
                DecisionRequest(
                    question="Allowed?",
                    input="evidence",
                    output_schema={"type": "boolean"},
                )
            )
        assert not client.is_closed()
    assert result.request_id == "req_actual"
    assert result.usage == TokenUsage(7, 0, 0, 3, 0, 10)
    assert requests[0].headers["Authorization"] == "Bearer synthetic-decision-key"
    assert requests[0].url.path == "/v1/decisions"
    assert json.loads(requests[0].content)["model"] == "gpt-6-luna"


async def test_expired_capture_prevents_provider_dispatch():
    owner = contexts()[1]
    live = True
    provider = Provider({"response": True})

    async def begin():
        nonlocal live
        live = False

        async def record(response):
            pytest.fail("No completed response exists")

        return record

    with _bind_decisions(owner, lambda: live, begin, provider):
        with pytest.raises(asyncio.CancelledError):
            await owner.decide("Allowed?", input="Evidence")
    assert not provider.requests


@pytest.mark.parametrize("failure", [False, True])
async def test_late_response_records_known_usage_then_discards_revoked_callback(failure):
    owner = contexts()[1]
    live = True
    recorded = []

    async def begin():
        async def record(response):
            recorded.append(response)

        return record

    class LateProvider:
        async def get_decision(self, request):
            nonlocal live
            live = False
            if failure:
                raise DecisionError("refusal", usage=USAGE)
            return DecisionResponse(value={"response": True}, usage=USAGE)

    with _bind_decisions(owner, lambda: live, begin, LateProvider()):
        with pytest.raises(asyncio.CancelledError):
            await owner.decide("Allowed?", input="Evidence")
    assert len(recorded) == 1 and recorded[0].usage == USAGE


@pytest.mark.parametrize("status", [None, "incomplete", "failed", "cancelled"])
async def test_fallback_requires_completed_status_even_when_partial_json_is_valid(compiler, status):
    compiler["supported"] = False
    client = Client(
        {
            "status": status,
            "output": [{"content": [{"type": "output_text", "text": '{"response":"partial"}'}]}],
        }
    )
    async with OpenAIDecisions(client=client, fallback_model="generation") as provider:
        with pytest.raises(DecisionError) as caught:
            await provider.decide("Answer", input="Evidence", output_type=str)
    assert caught.value.reason == "invalid_response"
    assert len(client.requests) == 1 and client.requests[0][0] == "/responses"


@pytest.mark.parametrize("value", [False, 0, "0", "false", 7, None])
async def test_choices_preserve_json_scalar_identity(value):
    provider = Provider({"response": value})
    result = await decide(
        "Select", input="Evidence", choices=[False, 0, "0", "false", 7, None], provider=provider
    )
    assert result == value and type(result) is type(value)


@pytest.mark.parametrize("choices", [[1, 1.0], [0, -0.0]])
async def test_choices_reject_duplicate_json_numbers_before_provider_work(choices):
    provider = Provider(None)
    with pytest.raises(ValueError, match="distinct"):
        await decide("Select", input="Evidence", choices=choices, provider=provider)
    assert not provider.requests


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {"not", "json"}])
async def test_non_json_provider_output_is_a_safe_completed_error(value):
    provider = Provider({"response": value})
    with pytest.raises(DecisionError) as caught:
        await decide("Allowed?", input="Evidence", provider=provider)
    error = caught.value
    assert error.reason == "invalid_response"
    assert error.usage == USAGE and error.request_id == "req_test" and error.model == "custom"
    assert error.__context__ is None
    assert len(provider.requests) == 1


async def test_non_finite_model_input_fails_before_provider_work():
    provider = Provider(None)
    with pytest.raises(ValueError):
        await decide(
            "Allowed?", input=[{"role": "user", "content": float("nan")}], provider=provider
        )
    assert not provider.requests


@pytest.mark.parametrize("reason", ["refusal", "invalid_response"])
@pytest.mark.parametrize("cleanup", ["failure", "cancel"])
async def test_completed_provider_error_remains_primary_when_recording_fails(reason, cleanup):
    owner = contexts()[1]
    failure = DecisionError(reason, usage=USAGE, request_id="completed", model="custom")
    cleanup_error = (
        asyncio.CancelledError() if cleanup == "cancel" else RuntimeError(cleanup + " failed")
    )
    recorded = []

    async def begin():
        async def record(response):
            recorded.append(response)
            raise cleanup_error

        return record

    with _bind_decisions(owner, lambda: True, begin, Provider(failure)):
        with pytest.raises(DecisionError) as caught:
            await owner.decide("Allowed?", input="Synthetic evidence")
    assert caught.value is failure
    assert caught.value.__context__ is cleanup_error
    assert recorded == [
        DecisionResponse(value=None, usage=USAGE, request_id="completed", model="custom")
    ]


async def test_completed_provider_error_joins_recording_when_caller_is_cancelled():
    owner = contexts()[1]
    failure = DecisionError("refusal", usage=USAGE)
    entered, release = asyncio.Event(), asyncio.Event()
    recorded = []

    async def begin():
        async def record(response):
            entered.set()
            await release.wait()
            recorded.append(response)

        return record

    async def invoke():
        with _bind_decisions(owner, lambda: True, begin, Provider(failure)):
            await owner.decide("Allowed?", input="Synthetic evidence")

    task = asyncio.create_task(invoke())
    try:
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(DecisionError) as caught:
            await task
        assert caught.value is failure
        assert recorded == [DecisionResponse(value=None, usage=USAGE)]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("envelope", [None, [], "malformed"])
async def test_real_openai_client_rejects_nonobject_completed_envelopes(
    compiler, fallback, envelope
):
    import httpx2
    import openai

    compiler["supported"] = not fallback
    requests = []

    async def respond(request):
        requests.append(request)
        return httpx2.Response(
            200,
            headers={"content-type": "application/json", "x-request-id": "req_malformed"},
            content=json.dumps(envelope).encode(),
        )

    async with openai.AsyncOpenAI(
        api_key="synthetic-decision-key",
        base_url="https://example.invalid/v1",
        max_retries=0,
        http_client=openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(respond)),
    ) as client:
        async with OpenAIDecisions(
            client=client, fallback_model="generative-fallback" if fallback else None
        ) as provider:
            with pytest.raises(DecisionError) as caught:
                await provider.get_decision(
                    DecisionRequest(
                        question="Allowed?", input="evidence", output_schema={"type": "boolean"}
                    )
                )
        assert not client.is_closed()
    assert caught.value.reason == "invalid_response"
    assert caught.value.usage is None and caught.value.model is None
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert len(requests) == 1
    assert requests[0].url.path == ("/v1/responses" if fallback else "/v1/decisions")


async def test_real_openai_client_preserves_normal_http_failures(compiler):
    import httpx2
    import openai

    async def respond(request):
        return httpx2.Response(
            429,
            headers={"x-request-id": "req_limited"},
            json={"error": {"message": "Synthetic rate limit"}},
        )

    async with openai.AsyncOpenAI(
        api_key="synthetic-decision-key",
        base_url="https://example.invalid/v1",
        max_retries=0,
        http_client=openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(respond)),
    ) as client:
        async with OpenAIDecisions(client=client) as provider:
            with pytest.raises(openai.RateLimitError) as caught:
                await provider.get_decision(
                    DecisionRequest(
                        question="Allowed?", input="evidence", output_schema={"type": "boolean"}
                    )
                )
    assert caught.value.request_id == "req_limited" and caught.value.status_code == 429
