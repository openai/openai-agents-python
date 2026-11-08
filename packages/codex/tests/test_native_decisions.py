"""Exercise native Decisions HTTP, tool execution, fallback and retained usage."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import BaseModel, Field

from agents import (
    Agent,
    ModelRequestContext,
    NativeError,
    NativeProvider,
    PreparedRequest,
    StopTurn,
    TurnStoppedError,
    function_tool,
)
from agents.observation import ModelRequestDispatching
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output
from tests.test_buffered_responses import JSONPeer, json_response

Answer = Callable[[dict[str, Any]], list[dict[str, Any]]]


def choice(question: dict[str, Any], value: str, probability: float = 1.0) -> dict[str, Any]:
    values = [item["value"] for item in question["choices"]]
    assert value in values and len(values) > 1
    return {
        "type": "choice",
        "name": question["name"],
        "choice": value,
        "confidence": probability,
        "probabilities": [
            {
                "value": item,
                "probability": probability
                if item == value
                else (1 - probability) / (len(values) - 1),
            }
            for item in values
        ],
    }


def route(index: int, probability: float = 1.0) -> Answer:
    def answer(body):
        assert len(body["questions"]) == 1
        question = body["questions"][0]
        assert question["name"] == "route"
        return [choice(question, f"route_{index}", probability)]

    return answer


def predicates(probability: float = 1.0) -> Answer:
    def answer(body):
        return [
            {"type": "predicate", "name": question["name"], "probability": probability}
            for question in body["questions"]
        ]

    return answer


@dataclass
class DecisionsPeer:
    replies: deque[Answer] = field(default_factory=deque)
    requests: list[dict[str, Any]] = field(default_factory=list)
    responses: ResponsesFixture = field(default_factory=ResponsesFixture)
    before_reply: Callable[[int], Awaitable[None]] | None = None
    url: str = ""
    http_status: int | None = None
    request_id_header: str | None = None
    expected_model: str = "decision-test"
    resolved_model: str = "decision-test"

    def provider(self, *, supports_websockets: bool = True) -> NativeProvider:
        # Default WebSocket support must be overridden by Decisions's HTTP profile.
        return NativeProvider(
            base_url=self.url,
            api_key="synthetic-decisions-key",
            supports_websockets=supports_websockets,
        )

    async def handle(self, request: web.Request) -> web.Response:
        assert request.headers["Authorization"] == "Bearer synthetic-decisions-key"
        body = await request.json()
        self.requests.append(body)
        index = len(self.requests)
        assert body["model"] == self.expected_model
        if self.http_status is not None:
            return web.json_response(
                {"error": {"message": "Synthetic provider rejection"}}, status=self.http_status
            )
        assert self.replies, "Unexpected Decisions request"
        answers = self.replies.popleft()(body)
        if self.before_reply is not None:
            await self.before_reply(index)
        return web.json_response(
            {
                "model": self.resolved_model,
                "answers": answers,
                "usage": {
                    "input_tokens": 7,
                    "input_tokens_details": {"cached_tokens": index, "cache_write_tokens": 0},
                    "output_tokens": 3,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 10,
                },
            },
            headers={"x-request-id": self.request_id_header} if self.request_id_header else None,
        )


def assert_decision_receipts(receipts, count):
    assert len(receipts) == count
    identifiers = [receipt["response_id"] for receipt in receipts]
    assert all(isinstance(identifier, str) and identifier for identifier in identifiers)
    assert len(set(identifiers)) == count
    assert [receipt["usage"]["total_tokens"] for receipt in receipts] == [10] * count
    # Distinct cached-token counts establish physical completion order independently of IDs.
    assert [
        receipt["usage"]["input_tokens_details"]["cached_tokens"] for receipt in receipts
    ] == list(range(1, count + 1))


@asynccontextmanager
async def peer(responses: ResponsesFixture | None = None):
    fixture = DecisionsPeer(responses=responses if responses is not None else ResponsesFixture())
    app = web.Application()
    app.router.add_post("/v1/decisions", fixture.handle)
    app.router.add_post("/v1/responses", fixture.responses.handle)
    async with TestServer(app) as server:
        fixture.url = str(server.make_url("/v1"))
        yield fixture


class Verdict(BaseModel):
    approved: bool


class Nested(BaseModel):
    category: Literal["accept", "review"]
    enabled: bool


class Assessment(BaseModel):
    nested: Nested
    probability: float = Field(ge=0, le=1)
    rating: float = Field(ge=1, le=5)
    retries: int = Field(ge=1, le=3)
    kind: Literal["triage"]


def assessment_answers(body: dict[str, Any]) -> list[dict[str, Any]]:
    answers = []
    for question in body["questions"]:
        name = question["name"]
        instructions = question["instructions"]
        if question["type"] == "predicate":
            answers.append({"type": "predicate", "name": name, "probability": 0.75})
        elif question["type"] == "score":
            answers.append(
                {
                    "type": "score",
                    "name": name,
                    "score": 2,
                    "confidence": 1,
                    "probabilities": [
                        {"label": level["label"], "value": i, "probability": int(i == 2)}
                        for i, level in enumerate(question["levels"])
                    ],
                }
            )
        else:
            expected = '"review"' if "/category" in instructions else "2"
            selected = next(c["value"] for c in question["choices"] if c["description"] == expected)
            answers.append(choice(question, selected))
    return answers


@pytest.mark.parametrize("model", [None, "decision-test", "custom-luna-alias"])
@pytest.mark.parametrize("with_request_id", [False, True])
async def test_nested_typed_output_and_physical_completion_usage(with_request_id, model):
    async with timeout(20), peer() as server, server.provider() as provider:
        server.expected_model = model or "gpt-6-luna"
        server.request_id_header = "provider-request-header" if with_request_id else None
        server.resolved_model = "decision-resolved-version"
        server.replies.append(assessment_answers)
        agent = Agent(
            instructions="Assess.",
            model=model,
            api="decisions",
            provider=provider,
            output_type=Assessment,
        )
        async with agent.session() as session:
            turn = await session.submit("Review this request.")
            result = await turn.get_result()
            assert result.final_output == Assessment(
                nested=Nested(category="review", enabled=True),
                probability=0.75,
                rating=3,
                retries=2,
                kind="triage",
            )
            assert result.thread_usage is not None
            assert result.thread_usage.total_token_usage.total_tokens == 10
            recorded = [item async for item in session.turns()]
            assert_decision_receipts(recorded[0]["responses"], 1)
            if with_request_id:
                assert recorded[0]["responses"][0]["response_id"] != server.request_id_header
            observations = [event async for event in turn.events()]
            dispatches = [
                event for event in observations if isinstance(event, ModelRequestDispatching)
            ]
            assert len(dispatches) == 1
            assert dispatches[0].wire == "decisions"
            assert dispatches[0].transport == "http_json"
        assert not server.responses.requests


async def test_tool_route_arguments_effect_and_continued_final_decision():
    calls = []

    @function_tool
    def toggle(enabled: bool) -> str:
        """TOOL_VERIFIED_RULE: enabled means the customer has verified their identity."""
        calls.append(enabled)
        return "TOOL_RESULT_SEEN_BY_NEXT_DECISION"

    async with timeout(20), peer() as server, server.provider() as provider:
        server.expected_model = "gpt-6-luna"
        server.replies.extend([route(1), predicates(), route(0), predicates()])
        agent = Agent(
            instructions="Toggle then return approval.",
            tools=[toggle],
            output_type=Verdict,
            api="decisions",
            provider=provider,
        )
        async with agent.session() as session:
            result = await (await session.submit("Enable it.")).get_result()
            assert result.final_output == Verdict(approved=True)
            assert calls == [True]
            field_request = server.requests[1]
            assert all(question["type"] == "predicate" for question in field_request["questions"])
            assert "TOOL_VERIFIED_RULE" in json.dumps(field_request)
            assert "customer has verified their identity" in json.dumps(field_request)
            assert "TOOL_RESULT_SEEN_BY_NEXT_DECISION" in json.dumps(server.requests[2]["input"])
            assert result.thread_usage is not None
            assert result.thread_usage.total_token_usage.total_tokens == 40
            recorded = [item async for item in session.turns()]
            assert_decision_receipts(recorded[0]["responses"], 4)
        assert not server.responses.requests


@pytest.mark.parametrize("fallback_model", ["responses-test", "gpt-6-luna"])
@pytest.mark.parametrize("reason", ["unsupported_selected", "low_probability"])
async def test_explicit_fallback_preserves_original_tools_and_all_usage(reason, fallback_model):
    calls = []

    @function_tool
    def toggle(enabled: bool) -> str:
        calls.append(enabled)
        return "toggled"

    async with timeout(20), peer() as server, server.provider() as provider:
        server.replies.append(route(0, 0.6 if reason == "low_probability" else 1.0))
        output_type = Verdict if reason == "low_probability" else str
        final_text = '{"approved":true}' if reason == "low_probability" else "fallback accepted"
        server.responses.respond(assistant_output(final_text))
        agent = Agent(
            instructions="Decide.",
            tools=[toggle],
            output_type=output_type,
            model="decision-test",
            api="decisions",
            fallback_model=fallback_model,
            min_route_probability=0.8,
            provider=provider,
        )
        async with agent.session() as session:
            result = await (await session.submit("Classify this request.")).get_result()
            assert result.final_output == (
                Verdict(approved=True) if reason == "low_probability" else final_text
            )
            assert calls == []
            assert len(server.responses.requests) == 1
            fallback = server.responses.requests[0]
            assert fallback["model"] == fallback_model
            assert "toggle" in json.dumps(fallback["tools"])
            assert "enabled" in json.dumps(fallback["tools"])
            assert "Classify this request." in json.dumps(fallback["input"])
            assert result.thread_usage is not None
            assert result.thread_usage.total_token_usage.total_tokens == 24
            recorded = [item async for item in session.turns()]
            receipts = recorded[0]["responses"]
            assert len(receipts) == 2
            assert_decision_receipts(receipts[:1], 1)
            assert receipts[1]["response_id"] == "resp_1"
            assert receipts[0]["response_id"] != receipts[1]["response_id"]
            assert receipts[1]["usage"]["total_tokens"] == 14
        assert len(server.requests) == 1


async def test_buffered_provider_keeps_decisions_routing_and_explicit_json_fallback():
    @function_tool
    def unused(enabled: bool) -> str:
        pytest.fail("The final-answer route must not invoke a tool")

    responses = JSONPeer(expected_model="gpt-6-luna")
    async with timeout(20), peer(responses) as server:
        server.expected_model = "gpt-6-luna"
        server.replies.extend([route(0, 0.6), route(0, 0.6)])
        async with NativeProvider(
            base_url=server.url,
            api_key="synthetic-decisions-key",
            response_mode="buffered",
        ) as provider:
            agent = Agent(
                instructions="Decide.",
                api="decisions",
                tools=[unused],
                output_type=Verdict,
                min_route_probability=0.8,
                provider=provider,
            )
            with pytest.raises(NativeError, match="requires an explicit fallback"):
                await agent.run("Classify without fallback.")
            assert len(server.requests) == 1
            assert not responses.requests

            agent = replace(agent, fallback_model="gpt-6-luna")
            responses.json_replies.append(
                json_response('{"approved":true}', "resp_buffered_fallback", "msg_fallback")
            )
            async with agent.session() as session:
                result = await (await session.submit("Classify with fallback.")).get_result()
                assert result.final_output == Verdict(approved=True)
                assert result.thread_usage is not None
                assert result.thread_usage.total_token_usage.total_tokens == 24
                recorded = [item async for item in session.turns()]
                receipts = recorded[0]["responses"]
                assert len(receipts) == 2
                assert receipts[0]["usage"]["total_tokens"] == 10
                assert receipts[1]["response_id"] == "resp_buffered_fallback"
                assert receipts[1]["usage"]["total_tokens"] == 14
            assert len(server.requests) == 2
            assert len(responses.requests) == 1
            assert responses.requests[0]["model"] == "gpt-6-luna"
            assert responses.requests[0]["stream"] is False
            assert responses.finished_bodies == 1


@pytest.mark.parametrize("failure", ["refusal", "invalid_probability"])
async def test_fill_failure_never_invokes_tool_or_fallback_and_keeps_usage(failure):
    calls = []

    @function_tool
    def toggle(enabled: bool) -> str:
        calls.append(enabled)
        return "never"

    def fail_fill(body):
        name = body["questions"][0]["name"]
        return (
            [{"type": "refusal", "name": name}]
            if failure == "refusal"
            else [{"type": "predicate", "name": name, "probability": 2}]
        )

    async with timeout(20), peer() as server, server.provider() as provider:
        server.replies.extend([route(1), fail_fill])
        agent = Agent(
            instructions="Toggle.",
            tools=[toggle],
            output_type=Verdict,
            model="decision-test",
            api="decisions",
            fallback_model="responses-test",
            provider=provider,
        )
        async with agent.session() as session:
            with pytest.raises(NativeError):
                await (await session.submit("Enable it.")).get_result()
            recorded = [item async for item in session.turns()]
            receipts = recorded[0]["responses"]
            assert_decision_receipts(receipts, 2)
            assert sum(r["usage"]["total_tokens"] for r in receipts) == 20
        assert calls == []
        assert not server.responses.requests
        assert len(server.requests) == 2


async def test_cancellation_during_fill_keeps_route_usage_without_late_tool():
    calls = []
    fill_entered, release = asyncio.Event(), asyncio.Event()

    @function_tool
    def toggle(enabled: bool) -> str:
        calls.append(enabled)
        return "never"

    async def block_fill(index):
        if index == 2:
            fill_entered.set()
            await release.wait()

    async with timeout(20), peer() as server, server.provider() as provider:
        server.replies.extend([route(1), predicates()])
        server.before_reply = block_fill
        agent = Agent(
            instructions="Toggle.",
            tools=[toggle],
            output_type=Verdict,
            model="decision-test",
            api="decisions",
            fallback_model="responses-test",
            provider=provider,
        )
        try:
            async with agent.session() as session:
                turn = await session.submit("Enable it.")
                await fill_entered.wait()
                await turn.cancel()
                with pytest.raises(TurnStoppedError):
                    await turn.get_result()
                release.set()
                recorded = [item async for item in session.turns()]
                receipts = recorded[0]["responses"]
                assert_decision_receipts(receipts, 1)
                assert receipts[0]["usage"]["total_tokens"] == 10
        finally:
            release.set()
        assert calls == []
        assert not server.responses.requests


async def test_all_unsupported_without_fallback_fails_before_http():
    async with timeout(20), peer() as server, server.provider() as provider:
        agent = Agent(
            instructions="Answer freely.", model="decision-test", api="decisions", provider=provider
        )
        with pytest.raises(NativeError):
            await agent.run("Write an open-ended paragraph.")
        assert not server.requests
        assert not server.responses.requests


async def test_prepared_request_input_reaches_decisions_compiler():
    seen = []
    transformed_models = []

    @function_tool
    def unused(enabled: bool) -> str:
        pytest.fail("The final-answer route must not invoke a tool")

    def transform(request: ModelRequestContext):
        assert request.model == "decision-test"
        assert request.purpose == "turn"
        transformed_models.append(request.model)
        return [
            *request.items,
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "TRANSFORMED_CONTEXT_MARKER"}],
            },
        ]

    def prepare(turn, request: PreparedRequest, *, preparation_index, recovery_attempt):
        seen.append((preparation_index, recovery_attempt))
        request["instructions"] += " PREPARED_INSTRUCTIONS_MARKER"
        request["input"].append(
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "PREPARED_INPUT_MARKER"}],
            }
        )
        return request

    async with timeout(20), peer() as server, server.provider() as provider:
        server.replies.extend([route(0), predicates()])
        agent = Agent(
            instructions="Classify.",
            output_type=Verdict,
            tools=[unused],
            model="decision-test",
            api="decisions",
            fallback_model="responses-test",
            provider=provider,
            transform_context=transform,
        )
        assert (
            await agent.run("Original input.", prepare_request=prepare)
        ).final_output == Verdict(approved=True)
        assert len(server.requests) == 2
        for request in server.requests:
            payload = json.dumps(request["input"])
            assert "TRANSFORMED_CONTEXT_MARKER" in payload
            assert "PREPARED_INSTRUCTIONS_MARKER" in payload
            assert "PREPARED_INPUT_MARKER" in payload
            assert "Original input." in payload
        assert seen == [(0, 0)]
        assert transformed_models == ["decision-test"]
        assert not server.responses.requests


async def test_provider_failure_does_not_use_semantic_fallback():
    async with timeout(20), peer() as server, server.provider() as provider:
        server.http_status = 400
        agent = Agent(
            instructions="Classify.",
            output_type=Verdict,
            model="decision-test",
            api="decisions",
            fallback_model="responses-test",
            provider=provider,
        )
        with pytest.raises(NativeError):
            await agent.run("Synthetic request.")
        assert len(server.requests) == 1
        assert not server.responses.requests


async def test_coordinator_delegates_to_string_model_specialist_then_resumes():
    selected = []

    def choose_technical(body):
        question = body["questions"][0]
        selected_choice = next(
            item["value"] for item in question["choices"] if item["description"] == '"technical"'
        )
        return [choice(question, selected_choice)]

    async with (
        timeout(20),
        peer() as server,
        server.provider(supports_websockets=False) as provider,
    ):
        specialist = Agent(
            instructions="Diagnose technical support requests.",
            model="responses-specialist",
            provider=provider,
        )

        @function_tool
        async def consult_specialist(specialty: Literal["billing", "technical"]) -> str:
            selected.append(specialty)
            assert specialty == "technical"
            return (await specialist.run("Production checkout stopped working.")).final_output

        server.replies.extend([route(1), choose_technical, route(0), predicates()])
        server.responses.respond(assistant_output("SPECIALIST_RESULT_CHECKOUT_REQUIRES_ACTION"))
        coordinator = Agent(
            instructions="Consult the matching specialist, then decide whether to approve action.",
            tools=[consult_specialist],
            model="decision-test",
            api="decisions",
            provider=provider,
            output_type=Verdict,
        )
        async with coordinator.session() as session:
            result = await (
                await session.submit("Production checkout stopped working.")
            ).get_result()
            assert result.final_output == Verdict(approved=True)
            assert selected == ["technical"]
            assert len(server.responses.requests) == 1
            assert server.responses.requests[0]["model"] == "responses-specialist"
            assert "Production checkout stopped working." in json.dumps(
                server.responses.requests[0]["input"]
            )
            assert "SPECIALIST_RESULT_CHECKOUT_REQUIRES_ACTION" in json.dumps(
                server.requests[2]["input"]
            )
            assert result.thread_usage is not None
            assert result.thread_usage.total_token_usage.total_tokens == 40
            recorded = [item async for item in session.turns()]
            assert_decision_receipts(recorded[0]["responses"], 4)


async def test_azure_shaped_provider_compacts_to_plain_fallback_summary_then_decides():
    fixture = DecisionsPeer()
    fixture.replies.extend([predicates(), predicates()])
    compaction_requests = []
    summary = "PLAIN_COMPACTION_SUMMARY_KEEP_APPROVAL_CONTEXT"

    async def compact(request):
        body = await request.json()
        compaction_requests.append(body)
        if len(compaction_requests) != 1:
            return web.json_response(
                {"error": {"message": "Unexpected fallback request"}}, status=400
            )
        if any(item.get("type") == "compaction_trigger" for item in body["input"]):
            # Azure-compatible remote compaction returns opaque encrypted context.
            # Decisions must select local compaction instead, so it can read the summary.
            fixture.responses.respond(
                {
                    "type": "response.output_item.done",
                    "item": {"type": "compaction", "encrypted_content": "OPAQUE_COMPACTION"},
                }
            )
        else:
            fixture.responses.respond(assistant_output(summary))
        return await fixture.responses.handle(request)

    app = web.Application()
    app.router.add_post("/azure-api.test/v1/decisions", fixture.handle)
    app.router.add_post("/azure-api.test/v1/responses", compact)
    async with timeout(25), TestServer(app) as server:
        fixture.url = str(server.make_url("/azure-api.test/v1"))
        async with fixture.provider() as provider:
            agent = Agent(
                instructions="Decide approval using the retained context.",
                model="decision-test",
                api="decisions",
                fallback_model="responses-test",
                provider=provider,
                output_type=Verdict,
            )
            async with agent.session() as session:
                first = await (await session.submit("Remember approval context.")).get_result()
                assert first.final_output == Verdict(approved=True)
                try:
                    await session.compact()
                except NativeError as error:
                    pytest.fail(
                        f"Model compaction failed: {error}; "
                        f"provider requests={compaction_requests!r}"
                    )
                snapshot = await session.model_context()
                assert summary in json.dumps(snapshot.items)
                assert not any(item.get("type") == "compaction" for item in snapshot.items)
                second = await (await session.submit("Decide again from the summary.")).get_result()
                assert second.final_output == Verdict(approved=True)
        assert len(compaction_requests) == 1
        assert compaction_requests[0]["model"] == "responses-test"
        assert not any(
            item.get("type") == "compaction_trigger" for item in compaction_requests[0]["input"]
        )
        assert len(fixture.requests) == 2
        assert summary in json.dumps(fixture.requests[1]["input"])


async def test_required_single_tool_field_request_retains_tool_semantic_rule():
    calls = []

    @function_tool
    def record(enabled: bool) -> str:
        """TOOL_REQUIRED_RULE: enabled means the shipment has passed customs inspection."""
        calls.append(enabled)
        return "recorded"

    def stop_after_tool(turn, *, step_index):
        return StopTurn("The one required call has completed") if step_index == 1 else None

    async with timeout(20), peer() as server, server.provider() as provider:
        server.replies.append(predicates())
        agent = Agent(
            instructions="Record the status.",
            tools=[record],
            model="decision-test",
            api="decisions",
            model_settings={"tool_choice": "required"},
            provider=provider,
        )
        with pytest.raises(TurnStoppedError):
            await agent.run("Record it.", before_step=stop_after_tool)
        assert calls == [True]
        assert len(server.requests) == 1
        field_request = server.requests[0]
        assert all(question["type"] == "predicate" for question in field_request["questions"])
        assert "TOOL_REQUIRED_RULE" in json.dumps(field_request)
        assert "shipment has passed customs inspection" in json.dumps(field_request)
        assert not server.responses.requests


async def test_opening_captures_decisions_policy_before_native_acquisition(monkeypatch):
    import importlib

    @function_tool
    def unreachable(enabled: bool) -> str:
        pytest.fail("Final output route must not execute a tool")

    native = importlib.import_module("agents._native")
    create_engine = native.create_engine
    creating = asyncio.Event()
    release = asyncio.Event()

    async def gated_create_engine(*args, **kwargs):
        creating.set()
        await release.wait()
        return await create_engine(*args, **kwargs)

    monkeypatch.setattr(native, "create_engine", gated_create_engine)
    async with timeout(20), peer() as server, server.provider() as provider:
        server.replies.extend([route(0, 0.6), route(0, 0.6), predicates()])
        server.responses.respond(assistant_output('{"approved":true}'))
        agent = Agent(
            instructions="Decide.",
            api="decisions",
            model="decision-test",
            output_type=Verdict,
            tools=[unreachable],
            fallback_model="responses-test",
            min_route_probability=0.8,
            provider=provider,
        )
        opening = asyncio.create_task(agent.run("First request."))
        try:
            await creating.wait()
            # A new declaration changes future openings without changing the
            # policy already captured by the suspended opening.
            agent = replace(agent, fallback_model=None, min_route_probability=0.2)
            release.set()
            assert (await opening).final_output == Verdict(approved=True)
            assert len(server.responses.requests) == 1
            assert server.responses.requests[0]["model"] == "responses-test"
            assert (await agent.run("Later request.")).final_output == Verdict(approved=True)
            assert len(server.responses.requests) == 1
        finally:
            release.set()
            await asyncio.gather(opening, return_exceptions=True)
