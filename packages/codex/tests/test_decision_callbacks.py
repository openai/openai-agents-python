"""Decisions through real native callbacks, accounting, and HTTP adaptation."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Literal

import httpx
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI
from pydantic import BaseModel

from agents import (
    Agent,
    ContinueTurn,
    DecisionError,
    DecisionResponse,
    NativeError,
    OpenAIDecisions,
    OutputValidationError,
    RunBudget,
    ToolContext,
    TurnStoppedError,
    function_tool,
)
from agents.observation import UsageUpdated
from agents.usage import TokenUsage
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider as generative_provider
from tests.test_native_child_agents import agent_for, child_usage, handles, trust
from tests.test_native_completion_callback import CompletionChildPeer
from tests.test_native_executor_tools import declaration

USAGE = TokenUsage(7, 0, 0, 3, 0, 10)
WIRE_USAGE = {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}


class RecordingProvider:
    def __init__(self, name="selected", *, usage=USAGE):
        self.name, self.usage, self.requests = name, usage, []

    async def get_decision(self, request):
        self.requests.append(request)
        value = request.input == "Final." if request.question == "Complete?" else True
        return DecisionResponse(
            value={"response": value},
            usage=self.usage,
            request_id=f"req-{self.name}-{len(self.requests)}",
            model="fixture-decision",
        )


@pytest_asyncio.fixture(loop_scope="function")
async def decision_peer():
    peer = SimpleNamespace(requests=[], mode="normal")

    async def handle(request):
        body = await request.json()
        peer.requests.append((request.path, body))
        if request.path == "/responses":
            data = {
                "status": "incomplete" if peer.mode == "incomplete" else "completed",
                "model": "fixture-fallback",
                "usage": WIRE_USAGE,
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": '{"text":"An explanation."}'}],
                    }
                ],
            }
        else:
            answers = []
            for question in body["questions"]:
                answer = {"name": question["name"], "type": question["type"]}
                if peer.mode == "refusal":
                    answer["type"] = "refusal"
                elif question["type"] == "predicate":
                    answer["probability"] = 2 if peer.mode == "invalid" else 0.75
                else:
                    values = [choice["value"] for choice in question["choices"]]
                    answer.update(
                        choice=values[-1],
                        confidence=1,
                        probabilities=[
                            {"value": value, "probability": int(index == len(values) - 1)}
                            for index, value in enumerate(values)
                        ],
                    )
                answers.append(answer)
            data = {"answers": answers, "model": "fixture-decision", "usage": WIRE_USAGE}
        return web.json_response(data, headers={"x-request-id": "req-http-decision"})

    app = web.Application()
    app.router.add_post("/decisions", handle)
    app.router.add_post("/responses", handle)
    async with TestServer(app) as server:
        async with AsyncOpenAI(
            api_key="synthetic-decision-key",
            base_url=str(server.make_url("/")),
            max_retries=0,
            http_client=httpx.AsyncClient(trust_env=False),
        ) as client:
            peer.client = client
            yield peer


async def test_builtin_uses_shared_compiler_and_real_http(decision_peer):
    class Ticket(BaseModel):
        category: Literal["billing", "technical"]
        urgent: bool

    decisions = OpenAIDecisions(client=decision_peer.client)
    result = await decisions.decide("Classify.", input="Evidence", output_type=Ticket)
    assert result == Ticket(category="technical", urgent=True)
    assert decision_peer.requests[0][0] == "/decisions"
    assert decision_peer.requests[0][1]["input"] == "Evidence"
    assert len(decision_peer.requests[0][1]["questions"]) == 2
    await decisions.aclose()
    assert not decision_peer.client.is_closed()


@pytest.mark.parametrize(
    "choices,expected",
    [
        ([False, "unknown"], "unknown"),
        (["unknown", False], False),
        ([True, "unknown"], "unknown"),
        (["unknown", True], True),
    ],
)
async def test_builtin_boolean_choices_use_decisions_without_fallback(
    decision_peer, choices, expected
):
    async with OpenAIDecisions(client=decision_peer.client) as decisions:
        result = await decisions.decide(
            "Choose the exact value.", input="Evidence", choices=choices
        )
    assert result == expected
    assert type(result) is type(expected)
    assert len(decision_peer.requests) == 1
    path, body = decision_peer.requests[0]
    assert path == "/decisions"
    assert len(body["questions"]) == 1
    assert len(body["questions"][0]["choices"]) == 2


@pytest.mark.parametrize("mode,reason", [("refusal", "refusal"), ("invalid", "invalid_response")])
async def test_builtin_errors_retain_completed_metadata(decision_peer, mode, reason):
    decision_peer.mode = mode
    decisions = OpenAIDecisions(client=decision_peer.client, fallback_model="fixture-fallback")
    with pytest.raises(DecisionError) as error:
        await decisions.decide("Check.", input="Evidence")
    assert error.value.reason == reason
    assert error.value.usage == USAGE
    assert error.value.request_id == "req-http-decision"
    assert [path for path, _ in decision_peer.requests] == ["/decisions"]


async def test_fallback_is_selected_before_inference_and_rejects_incomplete(decision_peer):
    class Explanation(BaseModel):
        text: str

    decisions = OpenAIDecisions(client=decision_peer.client)
    with pytest.raises(ValueError, match="Unsupported Decisions output schema"):
        await decisions.decide("Explain.", input="Evidence", output_type=Explanation)
    assert not decision_peer.requests
    fallback = OpenAIDecisions(client=decision_peer.client, fallback_model="fixture-fallback")
    assert await fallback.decide(
        "Explain.", input="Evidence", output_type=Explanation
    ) == Explanation(text="An explanation.")
    decision_peer.mode = "incomplete"
    with pytest.raises(DecisionError) as error:
        await fallback.decide("Explain.", input="Evidence", output_type=Explanation)
    assert error.value.reason == "invalid_response"
    assert error.value.usage == USAGE
    assert [path for path, _ in decision_peer.requests] == ["/responses", "/responses"]


async def test_binding_reaches_step_tool_and_completion_without_changing_context_size(responses):
    original, selected, override = [
        RecordingProvider(name) for name in ("original", "selected", "override")
    ]
    completions = []

    @function_tool
    async def check(context: ToolContext[None]) -> str:
        assert await context.decide("Tool check", input="Evidence", provider=override)
        return "checked"

    async def before_step(turn, *, step_index):
        assert await turn.decide("private-decision-question", input="Step evidence")

    async def complete(context):
        completions.append(context)
        if not await context.decide("Complete?", input=context.text or ""):
            return ContinueTurn("Provide the final answer.")
        return None

    responses.respond(function_call("check", {}, "check-1"))
    responses.respond(assistant_output("Candidate."))
    responses.respond(assistant_output("Final."))
    agent = Agent(
        instructions="Answer.",
        tools=[check],
        provider=generative_provider(responses),
        decisions=original,
        before_complete=complete,
    )
    async with timeout(20), agent.session(decisions=selected) as session:
        turn = await session.submit(
            "Start.", before_step=before_step, budget=RunBudget(max_requests=3)
        )
        result = await turn.get_result()
        saved = await session.save_state()
        history = [entry async for entry in session.turns()]
        with pytest.raises(RuntimeError, match="live callback"):
            await completions[0].decide("Stale", input="Evidence")
    assert result.final_output == "Final."
    assert result.thread_usage is not None
    assert result.thread_usage.total_token_usage.total_tokens == 102
    assert result.thread_usage.last_token_usage.total_tokens == 14
    assert history[-1]["usage"]["total_tokens"] == 102
    assert [context.continuation_count for context in completions] == [0, 1]
    assert {context.turn_id for context in completions} == {turn.turn_id}
    assert len(selected.requests) == 5 and len(override.requests) == 1
    assert not original.requests
    assert "private-decision-question" not in saved.to_json()
    assert len(responses.requests) == 3


@pytest.mark.parametrize(
    "usage,code", [(USAGE, "run_budget_exceeded"), (None, "run_budget_usage_unavailable")]
)
async def test_completed_decision_is_accounted_before_next_sampling_admission(
    responses, usage, code
):
    decisions = RecordingProvider(usage=usage)

    async def complete(context):
        await context.decide("Check", input=context.text or "")
        return ContinueTurn("One more step.")

    responses.respond(assistant_output("Candidate."))
    responses.respond(assistant_output("Must not sample."))
    agent = Agent(
        instructions="Answer.",
        provider=generative_provider(responses),
        decisions=decisions,
        before_complete=complete,
    )
    async with timeout(20):
        with pytest.raises(NativeError) as error:
            await agent.run("Start.", budget=RunBudget(max_tokens=20))
    assert error.value.code == code
    assert len(responses.requests) == len(decisions.requests) == 1


@pytest.mark.parametrize("refuse", [False, True])
async def test_invalid_completed_decision_retains_usage_before_raising(responses, refuse):
    class InvalidProvider:
        async def get_decision(self, request):
            if refuse:
                raise DecisionError("refusal", usage=USAGE)
            return DecisionResponse(value={"response": "invalid boolean"}, usage=USAGE)

    async def complete(context):
        await context.decide("Check", input=context.text or "")

    responses.respond(assistant_output("Candidate."))
    agent = Agent(
        instructions="Answer.",
        provider=generative_provider(responses),
        decisions=InvalidProvider(),
        before_complete=complete,
    )
    async with timeout(20), agent.session() as session:
        turn = await session.submit("Start.")
        with pytest.raises(DecisionError if refuse else OutputValidationError):
            await turn.get_result()
        history = [entry async for entry in session.turns()]
    assert history[-1]["usage"]["total_tokens"] == 24
    assert len(responses.requests) == 1


@pytest.mark.parametrize("close_session", [False, True])
async def test_cancel_joins_provider_cleanup_and_retains_late_completed_usage(
    responses, close_session
):
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    callbacks = 0

    class SlowProvider:
        async def get_decision(self, request):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()
            return DecisionResponse(value={"response": True}, usage=USAGE)

    async def complete(context):
        nonlocal callbacks
        callbacks += 1
        if callbacks == 1:
            await context.decide("Check", input=context.text or "")
            raise AssertionError("Cancelled callback continued after decide")

    responses.respond(assistant_output("Candidate."))
    responses.respond(assistant_output("Successor."))
    agent = Agent(
        instructions="Answer.",
        provider=generative_provider(responses),
        decisions=SlowProvider(),
        before_complete=complete,
    )
    async with timeout(20), agent.session() as session:
        turn = await session.submit("Start.")
        events = turn.events()
        await entered.wait()
        stopping = asyncio.create_task(session.aclose() if close_session else turn.cancel())
        try:
            await cleaning.wait()
            assert not stopping.done()
        finally:
            release.set()
        await stopping
        with pytest.raises(TurnStoppedError):
            await turn.get_result()
        usage = [event.usage async for event in events if isinstance(event, UsageUpdated)]
        assert usage[-1].total_token_usage.total_tokens == 24
        if close_session:
            return
        successor = await (await session.submit("Next.")).get_result()
        history = [entry async for entry in session.turns()]
    assert successor.final_output == "Successor."
    assert successor.thread_usage.total_token_usage.total_tokens == 38
    assert [entry["usage"]["total_tokens"] for entry in history] == [24, 14]


async def test_child_decision_keeps_usage_after_root_finishes(
    tmp_path, monkeypatch, native_executor_binary
):
    monkeypatch.setenv("CODEX_EXEC_SERVER_TEST_BINARY", str(native_executor_binary))
    entered, release = asyncio.Event(), asyncio.Event()
    decisions = RecordingProvider()

    async def complete(context):
        if context.subagent_id is not None:
            entered.set()
            await release.wait()
            assert await context.decide("Child check", input=context.text or "")

    peer = CompletionChildPeer(("default",))
    child_usage(peer, {"/root": (7, 3, 0, 0), "/root/alpha": (5, 2, 0, 0)})
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    async with (
        timeout(30),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        peer.url = str(server.make_url("/v1"))
        agent = replace(agent_for(peer, executor), decisions=decisions, before_complete=complete)
        async with agent.session(environment=declaration(executor)) as session:
            root = await session.submit("Use the child.", budget=RunBudget(max_tokens=21))
            try:
                await entered.wait()
                root_result = await root.get_result()
                assert root_result.thread_usage.total_token_usage.total_tokens == 20
                (child,) = await handles(session, 1)
            finally:
                release.set()
            child_result = await child.get_result()
            assert child_result.final_output == "Child candidate."
            assert child_result.thread_usage.total_token_usage.total_tokens == 17
            assert child_result.thread_usage.last_token_usage.total_tokens == 7
            assert (await root.get_result()).thread_usage == root_result.thread_usage
    assert len(decisions.requests) == 1
