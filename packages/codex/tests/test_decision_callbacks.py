"""Decisions through real native callbacks, accounting, and HTTP adaptation."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
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
    OpenAIDecisions,
    OutputValidationError,
    PromptedOutput,
    RunBudget,
    ToolContext,
    ToolRef,
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
            value=value,
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
    result = await Agent(instructions="Assess the supplied evidence.", decisions=decisions).decide(
        "Classify.", input="Evidence", output_type=Ticket
    )
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
async def test_builtin_boolean_choices_use_decisions(decision_peer, choices, expected):
    async with OpenAIDecisions(client=decision_peer.client) as decisions:
        result = await Agent(
            instructions="Assess the supplied evidence.", decisions=decisions
        ).decide("Choose the exact value.", input="Evidence", choices=choices)
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
    decisions = OpenAIDecisions(client=decision_peer.client)
    with pytest.raises(DecisionError) as error:
        await Agent(instructions="Assess the supplied evidence.", decisions=decisions).decide(
            "Check.", input="Evidence"
        )
    assert error.value.reason == reason
    assert error.value.usage == USAGE
    assert error.value.request_id == "req-http-decision"
    assert [path for path, _ in decision_peer.requests] == ["/decisions"]


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
    async with timeout(20), replace(agent, decisions=selected).session() as session:
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
    assert result.thread_usage.total_token_usage.total_tokens == 42
    assert result.thread_usage.last_token_usage.total_tokens == 14
    assert history[-1]["usage"]["total_tokens"] == 42
    assert [context.continuation_count for context in completions] == [0, 1]
    assert {context.turn_id for context in completions} == {turn.turn_id}
    assert len(selected.requests) == 5 and len(override.requests) == 1
    assert not original.requests
    assert "private-decision-question" not in saved.to_json()
    assert len(responses.requests) == 3


async def test_terminal_tool_completion_keeps_decisions_owner_without_text_repair(responses):
    class Final(BaseModel):
        value: int

    original, selected = [RecordingProvider(name) for name in ("original", "selected")]
    context = {"tenant": "fixture"}
    completions = []

    @function_tool(finish_turn=True)
    def finish() -> dict[str, int]:
        return {"value": 7}

    async def complete(candidate):
        assert candidate.context is context
        assert candidate.text is None
        assert candidate.tool_call is not None
        assert candidate.tool_call.tool == ToolRef("finish")
        assert candidate.final_output == Final(value=7)
        if completions:
            # An earlier candidate cannot borrow this candidate's fresh scope.
            with pytest.raises(RuntimeError, match="live callback"):
                await completions[0].decide("Stale", input="evidence")
        completions.append(candidate)
        assert await candidate.decide("Approve tool result?", input="value=7")
        if candidate.continuation_count == 0:
            return ContinueTurn("Call finish again.")
        return None

    responses.respond(function_call("finish", {}, "first-finish"))
    responses.respond(function_call("finish", {}, "accepted-finish"))
    agent = Agent(
        instructions="Use finish.",
        tools=[finish],
        provider=generative_provider(responses),
        decisions=original,
        output_type=PromptedOutput(Final, max_retries=2),
        before_complete=complete,
    )
    async with timeout(20), replace(agent, decisions=selected).session(context=context) as session:
        turn = await session.submit("Finish.")
        result = await turn.get_result()
        history = [entry async for entry in session.turns()]
        with pytest.raises(RuntimeError, match="live callback"):
            await completions[-1].decide("Stale", input="evidence")
    assert result.final_output == Final(value=7)
    assert result.tool_call is not None and result.tool_call.call_id == "accepted-finish"
    assert result.repair_attempts == ()
    assert result.thread_usage is not None
    assert result.thread_usage.total_token_usage.total_tokens == 28
    assert result.thread_usage.last_token_usage.total_tokens == 14
    assert history[-1]["usage"]["total_tokens"] == 28
    decision_responses = [
        response
        for response in history[-1]["responses"]
        if response.get("upstream_request_id") in {"req-selected-1", "req-selected-2"}
    ]
    assert decision_responses == []
    assert [candidate.continuation_count for candidate in completions] == [0, 1]
    assert {candidate.turn_id for candidate in completions} == {turn.turn_id}
    assert len(selected.requests) == 2 and not original.requests
    assert [request.input for request in selected.requests] == ["value=7", "value=7"]
    assert len(responses.requests) == 2


@pytest.mark.parametrize("refuse", [False, True])
async def test_invalid_completed_decision_keeps_model_usage_separate(responses, refuse):
    class InvalidProvider:
        async def get_decision(self, request):
            if refuse:
                raise DecisionError("refusal", usage=USAGE)
            return DecisionResponse(value="invalid boolean", usage=USAGE)

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
    assert history[-1]["usage"]["total_tokens"] == 14
    assert len(responses.requests) == 1


@pytest.mark.parametrize("close_session", [False, True])
async def test_cancel_joins_provider_cleanup_without_charging_model_usage(responses, close_session):
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
            return DecisionResponse(value=True, usage=USAGE)

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
        assert usage[-1].total_token_usage.total_tokens == 14
        if close_session:
            return
        successor = await (await session.submit("Next.")).get_result()
        history = [entry async for entry in session.turns()]
    assert successor.final_output == "Successor."
    assert successor.thread_usage.total_token_usage.total_tokens == 28
    assert [entry["usage"]["total_tokens"] for entry in history] == [14, 14]


async def test_child_decision_stays_live_after_root_finishes(
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
            assert child_result.thread_usage.total_token_usage.total_tokens == 7
            assert child_result.thread_usage.last_token_usage.total_tokens == 7
            assert (await root.get_result()).thread_usage == root_result.thread_usage
    assert len(decisions.requests) == 1


async def test_terminal_decision_cancellation_joins_without_model_usage(responses):
    class Final(BaseModel):
        value: int

    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests, resumed, candidates = [], [], []

    class SlowProvider:
        async def get_decision(self, request):
            requests.append(request.input)
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()
            return DecisionResponse(
                value=True,
                usage=USAGE,
                request_id="req-terminal-late",
                model="fixture-decision",
            )

    @function_tool(finish_turn=True)
    def finish() -> dict[str, int]:
        return {"value": 7}

    async def complete(candidate):
        if candidate.tool_call is None:
            return
        candidates.append(candidate)
        assert candidate.text is None
        assert candidate.tool_call.tool == ToolRef("finish")
        assert candidate.tool_call.call_id == "cancelled-finish"
        assert candidate.final_output == Final(value=7)
        await candidate.decide("Approve tool result?", input="value=7")
        resumed.append(candidate.turn_id)

    responses.respond(function_call("finish", {}, "cancelled-finish"))
    responses.respond(assistant_output('{"value":8}'))
    agent = Agent(
        instructions="Use finish.",
        tools=[finish],
        provider=generative_provider(responses),
        decisions=SlowProvider(),
        output_type=PromptedOutput(Final, max_retries=1),
        before_complete=complete,
    )
    async with timeout(20), agent.session() as session:
        turn = await session.submit("Finish.")
        await entered.wait()
        state = session._active
        assert state is not None and state.handle is not None
        assert state.handle.turn_id == turn.turn_id
        stopping = asyncio.create_task(turn.cancel())
        try:
            await cleaning.wait()
            assert not stopping.done()
            # Observe real native retirement before releasing completed metadata.
            # Shield the owner's receipt from cancellation of this test waiter.
            terminal = await asyncio.shield(state.terminal)
            assert terminal["type"] == "turn_aborted"
            assert not stopping.done()
        finally:
            release.set()
        await stopping
        with pytest.raises(TurnStoppedError):
            await turn.get_result()
        history = [record async for record in session.turns()]
        assert history[-1]["usage"]["total_tokens"] == 14
        decision_responses = [
            response
            for response in history[-1]["responses"]
            if response.get("upstream_request_id") == "req-terminal-late"
        ]
        assert decision_responses == []
        assert candidates[0].turn_id == turn.turn_id and not resumed
        assert requests == ["value=7"]
        assert state.cleanup_error is None
        successor = await (await session.submit("Next.")).get_result()
        assert successor.final_output == Final(value=8)
        assert successor.repair_attempts == ()
        assert successor.thread_usage.total_token_usage.total_tokens == 28
        history = [record async for record in session.turns()]
        assert [record["usage"]["total_tokens"] for record in history] == [14, 14]
    assert requests == ["value=7"] and not resumed
    assert len(responses.requests) == 2


def test_bound_decisions_with_native_tracing() -> None:
    """Run the actual process-owned native tracer around completion Decisions."""
    env = {key: value for key, value in os.environ.items() if key != "OPENAI_API_KEY"}
    root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(root), env.get("PYTHONPATH"))))
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio; from tests.test_decision_callbacks import "
            "_bound_decisions_with_native_tracing; "
            "asyncio.run(asyncio.wait_for(_bound_decisions_with_native_tracing(), 30))",
        ],
        env=env,
        cwd=root,
        text=True,
        capture_output=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def _bound_decisions_with_native_tracing() -> None:
    from agents import telemetry
    from tests.native_fixtures import ResponsesFixture

    fixture = ResponsesFixture()
    app = web.Application()
    app.router.add_post("/responses", fixture.handle)
    decisions = RecordingProvider()
    events = []

    class Recorder:
        def on_trace_event(self, event):
            events.append({**event, "attributes": dict(event["attributes"])})

    async def complete(candidate):
        assert await candidate.decide("PRIVATE_DECISION_QUESTION", input="PRIVATE_DECISION_INPUT")

    await telemetry.configure(processors=[Recorder()])
    try:
        async with TestServer(app) as server:
            fixture.url = str(server.make_url(""))
            agent = Agent(
                instructions="Answer.",
                decisions=decisions,
                provider=generative_provider(fixture),
                before_complete=complete,
            )
            for disabled in (False, True):
                fixture.respond(assistant_output("Final."))
                async with agent.session(trace_options={"disabled": disabled}) as session:
                    result = await (await session.submit("Answer.")).get_result()
                    assert result.final_output == "Final."
                    assert result.thread_usage.total_token_usage.total_tokens == 14
        assert len(decisions.requests) == len(fixture.requests) == 2
        with telemetry.trace("metadata-only", options={"include_sensitive_data": False}):

            class Invalid:
                async def get_decision(self, request):
                    return DecisionResponse(value="yes", usage=USAGE, request_id="req-invalid")

            with pytest.raises(OutputValidationError):
                await Agent(instructions="Assess.", decisions=Invalid()).decide(
                    "PRIVATE_DECISION_QUESTION", input="PRIVATE_DECISION_INPUT"
                )

            class Refused:
                async def get_decision(self, request):
                    raise DecisionError("refusal", request_id="req-refused")

            with pytest.raises(DecisionError):
                await Agent(instructions="Assess.", decisions=Refused()).decide(
                    "PRIVATE_DECISION_QUESTION", input="PRIVATE_DECISION_INPUT"
                )
        await telemetry.force_flush()
        usage = [
            event
            for event in events
            if event["name"] == "decision.usage" and event["lifecycle"] == "start"
        ]
        assert len(usage) == 3  # Disabled tracing creates no recording sink.
        attributes = usage[0]["attributes"]
        assert attributes["sdk.metadata.total_tokens"] == "10"
        assert attributes["sdk.metadata.usage_known"] == "true"
        assert attributes["sdk.metadata.outcome"] == "completed"
        by_request = {
            event["attributes"].get("sdk.metadata.request_id"): event["attributes"]
            for event in usage
        }
        assert by_request["req-invalid"]["sdk.metadata.total_tokens"] == "10"
        assert by_request["req-invalid"]["sdk.metadata.outcome"] == "completed"
        assert by_request["req-refused"]["sdk.metadata.usage_known"] == "false"
        assert by_request["req-refused"]["sdk.metadata.outcome"] == "refusal"
        assert "sdk.metadata.total_tokens" not in by_request["req-refused"]
        parents = {event["span_id"]: event for event in events}
        assert parents[usage[0]["parent_span_id"]]["name"] == "decision"
        encoded = json.dumps(events)
        assert "PRIVATE_DECISION_QUESTION" not in encoded
        assert "PRIVATE_DECISION_INPUT" not in encoded
    finally:
        await telemetry.shutdown()


async def test_decision_usage_does_not_consume_model_budget(responses):
    decisions = RecordingProvider(usage=TokenUsage(1000, 0, 0, 1000, 0, 2000))

    async def check(turn, **kwargs):
        assert await turn.decide("Allowed?", input="Synthetic evidence")

    responses.respond(assistant_output("within model budget"))
    agent = Agent(
        instructions="Answer.", decisions=decisions, provider=generative_provider(responses)
    )
    async with timeout(20), agent.session() as session:
        result = await (
            await session.submit(
                "Answer.", before_step=check, budget=RunBudget(max_tokens=15, max_requests=1)
            )
        ).get_result()
        assert result.final_output == "within model budget"
        assert result.thread_usage.total_token_usage.total_tokens == 14
        history = [entry async for entry in session.turns()]
        assert history[-1]["usage"]["total_tokens"] == 14
        assert len(history[-1]["responses"]) == 1
    assert len(decisions.requests) == len(responses.requests) == 1


@pytest.mark.parametrize("inflight", [False, True], ids=["after-timeout", "inflight-result"])
async def test_expired_function_tool_rejects_decisions(responses, inflight):
    requests, cancelled, rejected = [], [], []

    class Decisions:
        async def get_decision(self, request):
            requests.append(request)
            if inflight:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append("provider")
            return DecisionResponse(value=True)

    @function_tool(callback_timeout=0.1, timeout_behavior="error_as_result")
    async def lookup(context: ToolContext[None]) -> str:
        """Recover after a tool callback times out."""
        if not inflight:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append("tool")
        with pytest.raises(asyncio.CancelledError if inflight else RuntimeError):
            await context.decide("Ready?", input="Synthetic evidence")
        rejected.append(True)
        return "late output"

    responses.respond(function_call("lookup", {}, "expired-tool"))
    responses.respond(assistant_output("recovered"))
    agent = Agent(
        instructions="Use lookup.",
        tools=[lookup],
        provider=generative_provider(responses),
        decisions=Decisions(),
    )
    async with timeout(10), agent.session() as session:
        result = await (await session.submit("Read.")).get_result()
        assert result.final_output == "recovered"
    assert cancelled == ["provider" if inflight else "tool"]
    assert rejected == [True]
    assert len(requests) == int(inflight)


async def test_live_yielded_tool_can_decide_after_native_root_completion(responses):
    from tests.test_native_code_mode import code_call

    entered, release = asyncio.Event(), asyncio.Event()
    decisions = RecordingProvider()
    contexts = []
    finished = []

    @function_tool
    async def check(context: ToolContext[None]) -> str:
        """Check while the yielded tool callback remains live."""
        contexts.append(context)
        entered.set()
        await release.wait()
        assert await context.decide("Ready?", input="Synthetic evidence")
        finished.append(True)
        return "checked"

    final = assistant_output("done")

    async def hold(event):
        if event == final:
            await entered.wait()

    responses.before_event = hold
    responses.respond(
        code_call(
            "const pending=tools.check({}); yield_control(); text(await pending);", "live-cell"
        )
    )
    responses.respond(final)
    agent = Agent(
        instructions="Check then finish.",
        tools=[check],
        decisions=decisions,
        programmatic_tool_calling=True,
        provider=generative_provider(responses),
    )
    async with timeout(25), agent.session() as session:
        turn = await session.submit("Check.")
        await entered.wait()
        state = session._active
        assert state is not None
        try:
            terminal = await asyncio.shield(state.terminal)
            assert terminal["type"] == "task_complete"
        finally:
            release.set()
        result = await turn.get_result()
        assert result.final_output == "done" and finished == [True]
        assert result.thread_usage.total_token_usage.total_tokens == 28
        with pytest.raises(RuntimeError, match="exact live callback"):
            await contexts[0].decide("Expired?", input="Evidence")
    assert len(decisions.requests) == 1
