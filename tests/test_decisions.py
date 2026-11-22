"""Decisions at the explicit-input, client, callback and Runner boundaries."""

import asyncio
import json
from typing import Any
from unittest.mock import Mock

import httpx2
import pytest
from openai import AsyncOpenAI, InternalServerError
from pydantic import BaseModel, Field, field_validator

from agents import (
    Agent,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    Runner,
    RunState,
    SQLiteSession,
    Usage,
    handoff,
)
from agents.decisions import (
    DecisionError,
    DecisionRequest,
    DecisionResult,
    OpenAIDecisions,
    decide,
    decision_handoff,
    decision_scope,
)
from agents.decorators import tool
from agents.exceptions import UserError
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call

CONFIG = RunConfig(tracing_disabled=True)


class Provider:
    """Script the decision boundary independently of the primary model."""

    def __init__(self, value: Any):
        self.value = value
        self.requests: list[DecisionRequest] = []

    async def get_decision(self, request: DecisionRequest) -> DecisionResult[Any]:
        self.requests.append(request)
        return DecisionResult(self.value, "request", "fixture", Usage(requests=1, total_tokens=7))


class Count(BaseModel):
    count: int = Field(json_schema_extra={"minimum": 0})

    @field_validator("count")
    @classmethod
    def even(cls, value: int) -> int:
        if value % 2:
            raise ValueError("private parser details")
        return value


async def test_explicit_input_and_choices_are_captured_before_await():
    provider = Provider(False)
    evidence = [{"values": [1]}]
    choices = [0, False, None]
    pending = decide("Which?", provider=provider, input=evidence, choices=choices)
    evidence[0]["values"].clear()
    choices.clear()
    result = await pending
    assert result.value is False
    assert provider.requests[0].input == [{"values": [1]}]
    assert provider.requests[0].output_schema == {"enum": [0, False, None]}
    assert result.usage is not None
    assert (result.request_id, result.model, result.usage.total_tokens) == ("request", "fixture", 7)
    assert (await decide("Even?", provider=Provider(True), input="2")).value is True
    typed = await decide(
        "How many?", provider=Provider({"count": 2}), input="pair", output_type=Count
    )
    assert typed.value.count == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"question": " "},
        {"input": None},
        {"input": ""},
        {"input": []},
        {"input": [{"n": float("inf")}]},
        {"input": [{1: True}]},
        {"choices": []},
        {"choices": [1, 1.0]},
        {"choices": [float("nan")]},
        {"choices": ["x"], "output_type": Count},
    ],
)
async def test_invalid_requests_do_not_reach_provider(changes):
    provider = Provider(True)
    with pytest.raises(ValueError):
        await decide(**{"question": "Which?", "provider": provider, "input": "item", **changes})
    assert not provider.requests


@pytest.mark.parametrize("value", [-2, 3, "private", float("nan")])
async def test_schema_and_parser_failures_keep_only_metadata(value):
    class MutableProvider(Provider):
        async def get_decision(self, request):
            request.output_schema.clear()
            return await super().get_decision(request)

    with pytest.raises(DecisionError) as caught:
        await decide(
            "Count?", provider=MutableProvider({"count": value}), input="pair", output_type=Count
        )
    error = caught.value
    assert error.reason == "invalid_response" and error.__context__ is None
    assert error.usage is not None
    assert (error.model, error.request_id, error.usage.total_tokens) == ("fixture", "request", 7)
    assert "private" not in repr(error)


class BlockingProvider(Provider):
    """Control suspension and simulate a provider returning success after cancellation."""

    def __init__(self):
        super().__init__("blue")
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    async def get_decision(self, request):
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0)
        self.finished.set()
        return await super().get_decision(request)


async def test_scope_requires_exact_live_owner_and_joins_revoked_work():
    provider = BlockingProvider()
    owner = {}
    async with decision_scope(owner):
        with pytest.raises(RuntimeError, match="owner"):
            await decide("Which?", provider=provider, input="item", owner={})
        stale = decide("Which?", provider=provider, input="item", owner=owner)
        task = asyncio.create_task(
            decide("Which?", provider=provider, input="item", choices=["blue"], owner=owner)
        )
        await provider.started.wait()
    assert provider.finished.is_set()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with decision_scope(owner):
        with pytest.raises(RuntimeError, match="owner"):
            await stale
    assert len(provider.requests) == 1


@pytest.mark.parametrize("case", ["choice", "predicate", "refusal", "invalid", "transport"])
async def test_generated_client_without_network(case):
    requests = []

    def respond(req):
        requests.append(json.loads(req.content))
        assert req.url.path == "/v1/decisions"
        if case == "transport":
            return httpx2.Response(500, json={})
        answer = {"name": "decision", "type": case, "choice": "blue", "probability": 0.7}
        if case == "invalid":
            answer.update(type="choice", choice="private")
        return httpx2.Response(
            200,
            headers={"x-request-id": "observed"},
            json={
                "answers": [answer],
                "model": "fixture",
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 1,
                    "total_tokens": 4,
                    "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(respond), trust_env=False)
    async with AsyncOpenAI(
        api_key="synthetic", base_url="https://fixture.invalid/v1", http_client=http
    ) as client:
        if not hasattr(client, "decisions"):
            pytest.skip("Requires the optional Decisions client")
        provider = OpenAIDecisions(client, model="fixture")
        with pytest.raises(ValueError, match="predicate"):
            await decide("How many?", provider=provider, input="item", output_type=Count)
        options: dict[str, Any] = {} if case == "predicate" else {"choices": ["red", "blue"]}
        pending = decide("Which?", provider=provider, input="item", **options)
        if case == "transport":
            with pytest.raises(InternalServerError):
                await pending
        else:
            if case in ("refusal", "invalid"):
                with pytest.raises(DecisionError) as caught:
                    await pending
                result = caught.value
                assert result.reason == ("refusal" if case == "refusal" else "invalid_response")
                assert result.__context__ is None and "private" not in repr(result)
            else:
                result = await pending
                assert result.value == (True if case == "predicate" else "blue")
            assert result.usage is not None
            assert (result.request_id, result.model, result.usage.total_tokens) == (
                "observed",
                "fixture",
                4,
            )
        assert len(requests) == 1 and requests[0]["input"] == "item"
        assert requests[0]["questions"][0]["type"] == (
            "predicate" if case == "predicate" else "choice"
        )
        assert not http.is_closed


def source_agent(arguments=None):
    return Agent(
        name="Source",
        model=ScriptedModel(
            [
                ModelStep(
                    output=[
                        function_call(
                            "select_specialist",
                            arguments or {"input": "evidence"},
                            call_id="transfer",
                        )
                    ],
                    response_id="parent",
                    request_id="original",
                )
            ]
        ),
    )


async def run(source, stream, value="history", **kwargs):
    if stream:
        result = Runner.run_streamed(source, value, run_config=CONFIG, **kwargs)
        async for _ in result.stream_events():
            pass
        return result
    return await Runner.run(source, value, run_config=CONFIG, **kwargs)


@pytest.mark.parametrize("count", [0, 1, 2, 255, 256])
async def test_openai_handoff_catalog_uses_permitted_choice_count(count):
    model = ScriptedModel([[assistant_message("stay")]])
    source = Agent(name="Source", model=model)
    targets = {str(i): Agent(name=str(i)) for i in range(max(2, count))}
    client = Mock(spec=AsyncOpenAI)
    route = decision_handoff(
        source,
        provider=OpenAIDecisions(client, model="fixture"),
        question="Which?",
        targets=targets,
        is_available=lambda ctx, label: int(label) < count,
    )
    source.handoffs.append(route)
    await run(source, False)
    assert model.calls[0].handoffs == ([route] if 2 <= count <= 255 else [])


@pytest.mark.parametrize("stream", [False, True])
async def test_openai_handoff_revoked_after_catalog_skips_provider(stream, monkeypatch):
    source = source_agent()
    targets = {"blue": Agent(name="Blue", model=ScriptedModel()), "red": Agent(name="Red")}

    class Hooks(RunHooks):
        async def on_llm_end(self, context, agent, response):
            del targets["red"]

    client = Mock(spec=AsyncOpenAI)
    provider = OpenAIDecisions(client, model="fixture")
    with pytest.raises(ValueError, match="2 to 255"):
        await decide("Which?", provider=provider, input="item", choices=["blue"])
    calls = Provider("blue")
    monkeypatch.setattr(provider, "get_decision", calls.get_decision)
    source.handoffs.append(
        decision_handoff(source, provider=provider, question="Which?", targets=targets)
    )
    with pytest.raises(ValueError, match="unavailable"):
        await run(source, stream, hooks=Hooks())
    assert not calls.requests
    assert isinstance(source.model, ScriptedModel)
    assert len(source.model.calls) == 1 and source.model.calls[0].handoffs


@pytest.mark.parametrize("stream", [False, True])
async def test_runner_preserves_context_call_identity_and_specialist_permissions(stream):
    context = {"allowed": "blue"}
    transfers, effects = [], []

    class Hooks(RunHooks):
        async def on_handoff(self, context, from_agent, to_agent):
            transfers.append((context.context, from_agent, to_agent))

    @tool(is_enabled=False)
    def hidden() -> str:
        raise AssertionError

    @tool(needs_approval=True)
    def gated() -> str:
        effects.append("approved")
        return "done"

    model = ScriptedModel([[function_call("gated", {}, call_id="restricted")]])
    blue = Agent(
        name="Blue", model=model, tools=[hidden, gated], tool_use_behavior="stop_on_first_tool"
    )
    red = Agent(name="Red", model=ScriptedModel())
    source = source_agent()
    provider = Provider("blue")
    targets = {"red": red, "blue": blue}
    route = decision_handoff(
        source,
        provider=provider,
        question="Which?",
        targets=targets,
        is_available=lambda ctx, label: ctx.context["allowed"] == label,
    )
    source.handoffs.append(route)
    result = await run(
        source, stream, context=context, hooks=Hooks(), auto_previous_response_id=True
    )
    assert result.last_agent is blue
    assert len(transfers) == 1
    ctx, parent, target = transfers[0]
    assert ctx is context and parent is source and target is blue
    assert result.raw_responses[0].request_id == "original"
    assert model.calls[0].previous_response_id == "parent"
    assert [
        i["call_id"] for i in model.calls[0].input if i.get("type") == "function_call_output"
    ] == ["transfer"]
    assert [t.name for t in model.calls[0].tools] == ["gated"]
    assert not effects and len(result.interruptions) == 1
    assert result.context_wrapper.usage.total_tokens == 7
    saved = result.to_state().to_string()
    with pytest.raises(UserError):
        await RunState.from_string(source, saved)
    with pytest.raises(UserError):
        await RunState.from_string(source, saved, context_override={"allowed": "red"})
    del targets["blue"]
    with pytest.raises(UserError):
        await RunState.from_string(source, saved, context_override=context)
    targets["blue"] = blue.clone()
    with pytest.raises(UserError):
        await RunState.from_string(source, saved, context_override=context)
    targets["blue"] = blue
    source.handoffs[:] = [handoff(blue, is_enabled=False)]
    with pytest.raises(UserError):
        await RunState.from_string(source, saved, context_override=context)
    source.handoffs[:] = [route]
    state = await RunState.from_string(source, saved, context_override=context)
    state.approve(state.get_interruptions()[0])
    resumed = await Runner.run(source, state, run_config=CONFIG)
    assert resumed.final_output == "done" and effects == ["approved"]
    assert len(model.calls) == len(provider.requests) == 1
    assert provider.requests[0].input == "evidence"
    assert provider.requests[0].output_schema == {"enum": ["blue"]}


async def test_restore_rebuilt_roster_binds_label_and_agent_without_reselection():
    provider = Provider("blue")
    effects = []

    @tool(needs_approval=True)
    def record() -> str:
        effects.append("record")
        return "done"

    def build(label):
        source = source_agent()
        target = Agent(
            name="Specialist",
            tools=[record],
            tool_use_behavior="stop_on_first_tool",
            model=ScriptedModel([[function_call("record", {}, call_id="write")]]),
        )
        source.handoffs.append(
            decision_handoff(source, provider=provider, question="Which?", targets={label: target})
        )
        return source, target

    source, _ = build("blue")
    saved = (await run(source, False)).to_state().to_json()
    with pytest.raises(UserError, match="agent identity"):
        await RunState.from_json(build("green")[0], saved)
    restored, target = build("blue")
    state = await RunState.from_json(restored, saved)
    state.approve(state.get_interruptions()[0])
    result = await Runner.run(restored, state, run_config=CONFIG)
    assert result.last_agent is target and result.final_output == "done"
    assert effects == ["record"] and len(provider.requests) == 1


@pytest.mark.parametrize(
    "case", ["replaced", "removed", "permission", "unknown", "timeout", "cancel"]
)
async def test_no_transfer_after_revocation_failure_or_cancellation(case):
    source = source_agent()
    model = ScriptedModel([[assistant_message("unexpected")]])
    targets = {"blue": Agent(name="Blue", model=model)}
    context = {"allowed": True}
    provider = BlockingProvider()
    source.handoffs.append(
        decision_handoff(
            source,
            provider=provider,
            question="Which?",
            targets=targets,
            is_available=lambda ctx, label: ctx.context["allowed"],
            timeout=0.01 if case == "timeout" else 30,
        )
    )
    task = asyncio.create_task(run(source, False, context=context))
    try:
        await asyncio.wait_for(provider.started.wait(), 3)
        error: type[BaseException] = ValueError
        if case == "replaced":
            targets["blue"] = targets["blue"].clone()
        elif case == "removed":
            source.handoffs.clear()
        elif case == "permission":
            context["allowed"] = False
        elif case == "unknown":
            provider.value = "unlisted_tool"
            error = DecisionError
        elif case == "timeout":
            error = asyncio.TimeoutError
        else:
            task.cancel()
            error = asyncio.CancelledError
        if error in (ValueError, DecisionError):
            provider.release.set()
        with pytest.raises(error):
            await task
        assert provider.finished.is_set() and len(provider.requests) == 1
        assert isinstance(source.model, ScriptedModel)
        assert not model.calls and len(source.model.calls) == 1
    finally:
        provider.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("arguments", [{"input": "a", "grant": True}, {"input": "é" * 4097}])
async def test_model_cannot_extend_routing_input(arguments):
    source = source_agent(arguments)
    provider = Provider("blue")
    source.handoffs.append(
        decision_handoff(source, provider=provider, question="Which?", targets={"blue": source})
    )
    with pytest.raises(ValueError, match="routing input"):
        await run(source, False)
    assert not provider.requests


async def test_immediate_stream_cancellation_drains_late_selection():
    source = source_agent()
    model = ScriptedModel()
    provider = BlockingProvider()
    source.handoffs.append(
        decision_handoff(
            source,
            provider=provider,
            question="Which?",
            targets={"blue": Agent(name="Blue", model=model)},
        )
    )
    result = Runner.run_streamed(source, "history", run_config=CONFIG)

    async def drain():
        async for _ in result.stream_events():
            pass

    consumer = asyncio.create_task(drain())
    try:
        await asyncio.wait_for(provider.started.wait(), 3)
    finally:
        result.cancel()
        await consumer
    assert provider.finished.is_set() and not model.calls
    assert result.last_agent is source


async def test_primary_can_answer_without_selection_and_clones_need_their_own_route():
    provider = Provider("blue")
    model = ScriptedModel([[assistant_message("direct")], [assistant_message("direct")]])
    source = Agent(name="Source", model=model)
    route = decision_handoff(
        source, provider=provider, question="Which?", targets={"blue": Agent(name="Blue")}
    )
    source.handoffs.append(route)
    for agent in (source, source.clone()):
        assert (await run(agent, False)).final_output == "direct"
    assert len(model.calls[0].handoffs) == 1 and not model.calls[1].handoffs
    assert not provider.requests


async def test_scope_exit_during_cancelled_request_joins_without_interrupting_cleanup():
    started = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    cleaned = asyncio.Event()

    class SlowCleanup:
        async def get_decision(self, request):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                cleaned.set()

    owner = object()
    async with decision_scope(owner):
        task = asyncio.create_task(
            decide("Ready?", provider=SlowCleanup(), input="item", owner=owner)
        )
        try:
            await started.wait()
            task.cancel()
            await cleaning.wait()
        finally:
            asyncio.get_running_loop().call_soon(release.set)
    assert cleaned.is_set()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_explicit_recovery_returns_to_caller_without_transferring():
    model = ScriptedModel(
        [
            [function_call("select_specialist", {"input": "evidence"}, call_id="transfer")],
            [assistant_message("direct")],
        ]
    )
    source = Agent(name="Source", model=model)
    provider = Provider("unknown")
    route = decision_handoff(
        source, provider=provider, question="Which?", targets={"blue": Agent(name="Blue")}
    )
    route.failure_error_function = lambda ctx, error: "Transfer unavailable"
    source.handoffs.append(route)
    result = await run(source, False)
    assert result.last_agent is source and result.final_output == "direct"
    assert result.context_wrapper.usage.total_tokens == 7 and len(provider.requests) == 1
    assert any(
        i.get("output") == "Transfer unavailable" and i.get("call_id") == "transfer"
        for i in model.calls[1].input
    )


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("approve_first", [False, True])
@pytest.mark.parametrize("failure", ["cancel", "timeout", "refusal", "invalid"])
async def test_selection_failure_keeps_completed_tool_receipt(stream, approve_first, failure):
    effects = []

    class Failing:
        async def get_decision(self, request):
            effects.append("select")
            if failure == "refusal":
                raise DecisionError("refusal")
            if failure == "invalid":
                return DecisionResult("unknown")
            if failure == "timeout":
                await asyncio.Event().wait()
            future = asyncio.get_running_loop().create_future()
            future.cancel("private details")
            return await future

    @tool(needs_approval=approve_first)
    def record() -> str:
        effects.append("record")
        return "recorded"

    source = Agent(
        name="Source",
        tools=[record],
        model=ScriptedModel(
            [
                [
                    function_call("record", {}, call_id="effect"),
                    function_call("select_specialist", {"input": "evidence"}, call_id="select"),
                ]
            ]
        ),
    )
    specialist = ScriptedModel()
    source.handoffs.append(
        decision_handoff(
            source,
            provider=Failing(),
            question="Which?",
            timeout=0.01 if failure == "timeout" else 30,
            targets={"blue": Agent(name="Blue", model=specialist)},
        )
    )
    session = SQLiteSession("receipts")
    try:
        value: Any = "history"
        if approve_first:
            paused = await run(source, stream, session=session)
            value = paused.to_state()
            value.approve(value.get_interruptions()[0])
        error = {"cancel": RuntimeError, "timeout": asyncio.TimeoutError}.get(
            failure, DecisionError
        )
        with pytest.raises(error) as caught:
            await run(source, stream, value, session=session)
        if failure == "cancel":
            assert str(caught.value) == "Decision provider cancelled its request"
        if approve_first:
            items = [
                entry["raw_item"]
                for entry in value.to_json()["generated_items"]
                if entry["type"] != "tool_approval_item"
            ]
        else:
            items = await session.get_items()
        receipts = [i for i in items if i.get("call_id") == "effect"]
        assert [i["type"] for i in receipts] == ["function_call", "function_call_output"]
        assert receipts[-1].get("output") == "recorded"
        assert effects == ["record", "select"] and not specialist.calls
    finally:
        session.close()


@pytest.mark.parametrize("stream", [False, True])
async def test_nested_specialist_uses_fresh_context_and_owns_approval(stream):
    effects = []
    provider = Provider("blue")

    @tool(needs_approval=True)
    def record(ctx: RunContextWrapper[dict[str, str]]) -> str:
        effects.append(ctx.context)
        return "recorded"

    source = source_agent()
    target = Agent(
        name="Blue",
        tools=[record],
        tool_use_behavior="stop_on_first_tool",
        model=ScriptedModel([[function_call("record", {}, call_id="effect")]]),
    )
    route = decision_handoff(
        source,
        provider=provider,
        question="Which?",
        targets={"blue": target},
        is_available=lambda ctx, label: ctx.context["allowed"] == label,
    )
    source.handoffs.append(route)
    manager = Agent(
        name="Manager",
        tools=[source.as_tool(tool_name="ask", tool_description="Ask")],
        model=ScriptedModel(
            [
                [function_call("ask", {"input": "work"}, call_id="parent")],
                [assistant_message("done")],
            ]
        ),
    )
    paused = await run(manager, stream, context={"allowed": "blue"})
    saved = paused.to_state().to_string()
    fresh = {"allowed": "blue", "revision": "current"}
    with pytest.raises(UserError, match="unavailable"):
        await RunState.from_string(manager, saved)
    with pytest.raises(UserError, match="unavailable"):
        await RunState.from_string(manager, saved, context_override={"allowed": "red"})
    source.handoffs[:] = [target]
    with pytest.raises(UserError):
        await RunState.from_string(manager, saved, context_override=fresh)
    source.handoffs[:] = [route]
    state = await RunState.from_string(manager, saved, context_override=fresh)
    assert not effects
    state.approve(state.get_interruptions()[0])
    done = await run(manager, not stream, state)
    assert done.final_output == "done"
    assert len(effects) == len(provider.requests) == 1 and effects[0] is fresh
