"""Typed Decisions, callback ownership and separate usage, without service calls."""

import asyncio
import json
from contextlib import nullcontext
from copy import copy
from typing import Annotated, Any, Literal

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic import BaseModel, Field, create_model

from agents import (
    Agent,
    AgentHooks,
    DecisionError,
    DecisionRequest,
    DecisionResponse,
    OpenAIDecisions,
    RunBudget,
    RunConfig,
    Runner,
    function_tool,
)
from agents.testing import ScriptedModel, assistant_message, function_call
from agents.tool_context import ToolContext
from agents.usage import Usage


class Provider:
    def __init__(self, value):
        self.value = value
        self.calls = []

    async def get_decision(self, request: DecisionRequest) -> DecisionResponse:
        self.calls.append(request)
        return DecisionResponse(value=self.value, usage=Usage(input_tokens=99, total_tokens=99))


class Queue(BaseModel):
    team: Literal["account", "orders"]
    urgent: bool


class Ticket(BaseModel):
    queue: Queue
    probability: Annotated[float, Field(ge=0, le=1)]
    priority: Annotated[int, Field(ge=0, le=3)]
    rating: Annotated[float, Field(ge=-1, le=1)]


async def test_real_client_structured_record_and_borrowed_lifetime():
    if not hasattr(AsyncOpenAI, "decisions"):
        pytest.skip("This client has no Decisions resource.")
    sent = []

    def respond(req):
        data = json.loads(req.content)
        sent.append(data)
        answers = []
        for q in data["questions"]:
            answer = {"name": q["name"], "type": q["type"]}
            if q["type"] == "predicate":
                answer["probability"] = 0.75
            elif q["type"] == "choice":
                values = [c["value"] for c in q["choices"]]
                answer.update(
                    choice=values[1],
                    confidence=1,
                    probabilities=[
                        {"value": v, "probability": float(i == 1)} for i, v in enumerate(values)
                    ],
                )
            else:
                answer.update(
                    score=1.5,
                    confidence=0.5,
                    probabilities=[
                        {"label": level["label"], "value": i, "probability": 0.5 if i else 0}
                        for i, level in enumerate(q["levels"])
                    ],
                )
            answers.append(answer)
        return httpx2.Response(200, json={"answers": answers})

    async with AsyncOpenAI(
        api_key="synthetic",
        base_url="https://fixture.invalid/v1",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond), trust_env=False),
    ) as client:
        async with OpenAIDecisions(client=client) as provider:
            agent = Agent(name="Queue", decisions=provider)
            ticket = await agent.decide("Classify", input="order", output_type=Ticket)
            assert ticket == Ticket(
                queue=Queue(team="orders", urgent=True), probability=0.75, priority=1, rating=0.5
            )
            assert await agent.decide("Urgent?", input="order") is True
            assert await agent.decide("Team?", input="order", choices=["only"]) == "only"
            assert len(sent) == 2 and all(d["input"] == "order" for d in sent)
        assert not client.is_closed()
        with pytest.raises(RuntimeError, match="closed"):
            await agent.decide("Urgent?", input="")


async def test_private_validation_preserves_types_and_rejects_unsupported_before_dispatch():
    provider = Provider(1)
    a = Agent(name="check", decisions=provider)
    assert a.clone().decisions is provider
    fields: dict[str, Any] = {f"k{i}": (bool, ...) for i in range(401)}
    for typ in (
        str,
        float,
        list[bool],
        dict[str, bool],
        int,
        create_model("TooMany", **fields),
    ):
        with pytest.raises(ValueError):
            await a.decide("Classify", input="", output_type=typ)
    for choices in ([], [1, 1.0], [True, True], [float("nan")], list(range(256))):
        with pytest.raises(ValueError):
            await a.decide("Classify", input="", choices=choices)
    assert not provider.calls
    with pytest.raises(DecisionError) as error:
        await a.decide("Allow?", input="private")
    assert error.value.usage is not None
    assert error.value.reason == "invalid_response" and error.value.usage.total_tokens == 99
    assert await a.decide("Which?", input="", choices=[True, 1]) is provider.value
    assert await a.decide("Allow?", input="", provider=Provider(False)) is False
    with pytest.raises(DecisionError):
        await a.decide(
            "Classify",
            input="",
            output_type=Queue,
            provider=Provider({"team": "orders", "urgent": "true"}),
        )

    class Changed:
        async def get_decision(self, request):
            request.output_schema["enum"].append("other")
            return DecisionResponse(value="other")

    with pytest.raises(DecisionError):
        await a.decide("Which?", input="", choices=["a", "b"], provider=Changed())


async def finish(agent, stream, **kwargs):
    if stream:
        result = Runner.run_streamed(agent, "history", **kwargs)
        async for _ in result.stream_events():
            pass
        return result
    return await Runner.run(agent, "history", **kwargs)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("own", [False, True])
async def test_exact_tool_and_proposal_owners_after_handoff(stream, own, monkeypatch):
    import agents.tracing

    spans = []

    def span(name, *, data):
        spans.append(data)
        return nullcontext()

    monkeypatch.setattr(agents.tracing, "custom_span", span)
    root, child = Provider(True), Provider(True)
    saved = []

    @function_tool(failure_error_function=None)
    async def verify(ctx: ToolContext[None]) -> bool:
        saved.append(ctx)
        target.decisions = Provider(False)
        with pytest.raises(RuntimeError, match="live"):
            await copy(ctx).decide("Wrong?", input="")
        return await ctx.decide("Check?", input="tool")

    async def policy(run, proposal):
        assert not hasattr(run, "decide")
        with pytest.raises(RuntimeError, match="live"):
            await saved[0].decide("Again?", input="")
        saved.append(proposal)
        assert await proposal.decide("Ready?", input=proposal.output)
        return "finish"

    target = Agent(
        name="next",
        decisions=child if own else None,
        tools=[verify],
        hooks=AgentHooks(final_output_policy=policy),
        model=ScriptedModel(
            [[function_call("verify", {}, call_id="verify")], [assistant_message("done")]]
        ),
    )
    first = Agent(
        name="first",
        decisions=root,
        handoffs=[target],
        model=ScriptedModel([[function_call("transfer_to_next", {}, call_id="transfer_to_next")]]),
    )
    result = await finish(
        first,
        stream,
        run_config=RunConfig(tracing_disabled=own, budget=RunBudget(max_model_calls=3)),
    )
    assert result.final_output == "done" and result.context_wrapper.usage.total_tokens == 0
    assert [call.input for call in (child if own else root).calls] == ["tool", "done"]
    assert not (root if own else child).calls
    assert len(spans) == (0 if own else 2)
    assert all(s["usage"]["total_tokens"] == 99 and "input" not in s for s in spans)
    with pytest.raises(RuntimeError, match="live"):
        await saved[-1].decide("Again?", input="")


@pytest.mark.parametrize("stream", [False, True])
async def test_agent_as_tool_has_its_own_grant(stream):
    root = Provider(True)

    async def policy(run, proposal):
        with pytest.raises(ValueError, match="Configure"):
            await proposal.decide("Use parent?", input="")
        return "finish"

    child = Agent(
        name="child",
        model=ScriptedModel([[assistant_message("reply")]]),
        hooks=AgentHooks(final_output_policy=policy),
    )
    parent = Agent(
        name="parent",
        decisions=root,
        tools=[child.as_tool(tool_name="ask", tool_description="Ask", failure_error_function=None)],
        model=ScriptedModel(
            [[function_call("ask", {"input": "query"}, call_id="ask")], [assistant_message("done")]]
        ),
    )
    assert (
        await finish(parent, stream, run_config=RunConfig(tracing_disabled=True))
    ).final_output == "done"
    assert not root.calls


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("route", ["direct", "tool", "policy", "handoff"])
async def test_cancelled_tool_joins_decision_cleanup(stream, route):
    from agents.decisions import decision_handoff

    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Blocking:
        async def get_decision(self, request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
            return DecisionResponse(value=True)

    @function_tool
    async def check(ctx: ToolContext[None]) -> bool:
        return await ctx.decide("OK?", input="")

    agent = Agent(
        name="check",
        decisions=Blocking(),
        model=ScriptedModel([[function_call("check", {}, call_id="check")]]),
        tools=[check],
    )
    if route == "policy":

        async def policy(run, proposal):
            await proposal.decide("Ready?", input="")
            return "finish"

        agent.hooks = AgentHooks(final_output_policy=policy)
        agent.model = ScriptedModel([[assistant_message("done")]])
    if route == "handoff":
        selector = decision_handoff(
            agent, question="Which?", targets={"a": Agent(name="a"), "b": Agent(name="b")}
        )
        agent.handoffs = [selector]
        agent.model = ScriptedModel(
            [[function_call(selector.tool_name, {"input": "question"}, call_id="select")]]
        )
    if route != "direct":
        agent = Agent(
            name="parent",
            tools=[agent.as_tool(tool_name="ask", tool_description="Ask")],
            model=ScriptedModel([[function_call("ask", {"input": "question"}, call_id="ask")]]),
        )
    config = RunConfig(tracing_disabled=True)
    if stream:
        result = Runner.run_streamed(agent, "input", run_config=config)

        async def drain():
            async for _ in result.stream_events():
                pass

        task = asyncio.create_task(drain())
        cancel = result.cancel
    else:
        task = asyncio.create_task(Runner.run(agent, "input", run_config=config))
        cancel = task.cancel
    try:
        await asyncio.wait_for(entered.wait(), 5)
        cancel()
        await asyncio.wait_for(cleaning.wait(), 5)
        done, _ = await asyncio.wait((task,), timeout=0.35)
        assert not done
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)


@pytest.mark.parametrize("stream", [False, True])
async def test_concurrent_runs_and_detached_callback_requests(stream):
    class Pending:
        def __init__(self):
            self.entered = asyncio.Event()
            self.drained = asyncio.Event()

        async def get_decision(self, request):
            self.entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.drained.set()
            return DecisionResponse(value=True)

    async def single(value):
        pending = Pending()
        detached = []

        async def check(run, proposal):
            detached.append(
                asyncio.create_task(proposal.decide("Later?", input="", provider=pending))
            )
            await pending.entered.wait()
            assert await proposal.decide("OK?", input="") is value
            return "finish"

        agent = Agent(
            name="same",
            decisions=Provider(value),
            hooks=AgentHooks(final_output_policy=check),
            model=ScriptedModel([[assistant_message("done")]]),
        )
        await finish(agent, stream, run_config=RunConfig(tracing_disabled=True))
        assert pending.drained.is_set()
        with pytest.raises(asyncio.CancelledError):
            await detached[0]

    await asyncio.wait_for(asyncio.gather(single(True), single(False)), 5)


@pytest.mark.parametrize("kind", ["score", "choice"])
@pytest.mark.parametrize("total", [0.4, 1.000001, 1.4])
async def test_service_distribution_must_sum_to_one(kind, total):
    if not hasattr(AsyncOpenAI, "decisions"):
        pytest.skip("This client has no Decisions resource.")

    def respond(req):
        q = json.loads(req.content)["questions"][0]
        answer = {"type": kind, "name": q["name"], "confidence": 0.5}
        if kind == "choice":
            answer.update(
                choice="a",
                probabilities=[{"value": v, "probability": total / 2} for v in ("a", "b")],
            )
        else:
            answer.update(
                score=0.5,
                probabilities=[
                    {"value": i, "label": level["label"], "probability": total / 2}
                    for i, level in enumerate(q["levels"])
                ],
            )
        return httpx2.Response(200, json={"answers": [answer]})

    async with AsyncOpenAI(
        api_key="synthetic",
        base_url="https://fixture.invalid/v1",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond), trust_env=False),
    ) as client:
        agent = Agent(name="Check", decisions=OpenAIDecisions(client=client))
        typ = Literal["a", "b"] if kind == "choice" else Annotated[float, Field(ge=1, le=2)]
        value = agent.decide("Which?", input="", output_type=typ)
        if total == 1.000001:
            assert await value == ("a" if kind == "choice" else 1.5)
        else:
            with pytest.raises(DecisionError) as error:
                await value
            assert error.value.reason == "invalid_response"


async def test_decision_span_reports_the_validated_outcome_once(monkeypatch):
    import agents.tracing

    spans = []

    def capture(name, *, data):
        assert name == "decision.usage"
        spans.append(data)
        return nullcontext()

    monkeypatch.setattr(agents.tracing, "custom_span", capture)

    class Refuses:
        async def get_decision(self, request):
            raise DecisionError("refusal", usage=Usage(input_tokens=1, total_tokens=1))

    a = Agent(name="checker", decisions=Provider(True))
    with agents.tracing.trace("check"):
        assert await a.decide("Secret question?", input="evidence") is True
        with pytest.raises(DecisionError):
            await a.decide("Secret question?", input="evidence", provider=Provider("secret value"))
        with pytest.raises(DecisionError):
            await a.decide("Secret question?", input="evidence", provider=Refuses())
    assert [s["outcome"] for s in spans] == ["completed", "invalid_response", "refusal"]
    assert [s["usage"]["total_tokens"] for s in spans] == [99, 99, 1]
    assert "secret" not in repr(spans).lower() and "evidence" not in repr(spans)


async def test_decide_is_async_and_gather_handles_invalid_calls():
    import inspect

    from agents import ProposedFinalOutput

    assert all(
        inspect.iscoroutinefunction(cls.decide) for cls in (Agent, ToolContext, ProposedFinalOutput)
    )
    a = Agent(name="check", decisions=Provider(True))
    ok, invalid = await asyncio.gather(
        a.decide("Pass?", input=""), a.decide("", input=""), return_exceptions=True
    )
    assert ok is True and isinstance(invalid, ValueError)


@pytest.mark.parametrize("stream", [False, True])
async def test_request_filter_uses_inherited_provider_on_fresh_owner(stream):
    provider = Provider(True)
    model = ScriptedModel([[assistant_message("done")]])
    target = Agent(name="next", model=model)
    first = Agent(
        name="first",
        decisions=provider,
        handoffs=[target],
        model=ScriptedModel([[function_call("transfer_to_next", {}, call_id="transfer")]]),
    )
    saved = []

    async def prepare(data):
        if data.agent is first:
            saved.append(data.decide("Later?", input=""))
        else:
            with pytest.raises(RuntimeError, match="live"):
                await saved.pop()
            if await data.decide("Include instructions?", input="new user"):
                data.model_data.instructions = "Explain the next step."
            saved.append(data.decide("After exit?", input=""))
        return data.model_data

    await finish(
        first, stream, run_config=RunConfig(tracing_disabled=True, call_model_input_filter=prepare)
    )
    assert model.calls[0].system_instructions == "Explain the next step."
    with pytest.raises(RuntimeError, match="live"):
        await saved.pop()
    assert [call.input for call in provider.calls] == ["new user"]
