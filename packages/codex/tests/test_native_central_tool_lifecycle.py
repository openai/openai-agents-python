"""Awaited central observers follow native invocation and accepted output ownership."""

from __future__ import annotations

import json

import pytest

from agents import (
    Agent,
    Deny,
    GuardrailResult,
    GuardrailTripwire,
    HttpMCP,
    InputGuardrail,
    ToolContext,
    ToolLifecycleContext,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider
from tests.test_native_manual_tools import next_request
from tests.test_native_mcp_tools import Peer, mcp_call


async def test_central_fanout_covers_actual_function_mcp_and_configured_child(responses):
    application = object()
    peer = Peer(names=("search",))
    observations, timeline, effects = [], [], []
    expected_agents = {}

    def record(scope, event: ToolLifecycleContext):
        assert event.context is application
        assert event.agent is expected_agents[event.agent.name]
        assert event.call_id and event.turn_id and event.session_id
        assert isinstance(json.loads(event.arguments), dict)
        if event.kind == "end":
            assert event.output["type"] == "function_call_output"
            assert event.output["call_id"] == event.call_id
            assert event.succeeded is True
        observations.append((scope, event))
        timeline.append((scope, event.tool.name, event.kind))

    async def run_observer(event):
        record("run", event)

    async def agent_observer(event):
        record("agent", event)

    @function_tool
    def direct(context: ToolContext, key: str) -> str:
        assert context.context is application
        effects.append("direct")
        timeline.append(("effect", "direct", "body"))
        return "direct " + key

    @function_tool
    def leaf(context: ToolContext, key: str) -> str:
        assert context.context is application
        effects.append("leaf")
        timeline.append(("effect", "leaf", "body"))
        return "leaf " + key

    for output in (
        function_call("direct", {"key": "read"}, "central-direct"),
        mcp_call("search", "mcp__records", "central-mcp"),
        function_call("nested", {"input": "work"}, "central-child"),
        function_call("leaf", {"key": "read"}, "central-leaf"),
        assistant_output("child complete"),
        assistant_output("root complete"),
    ):
        responses.respond(output)
    async with timeout(20), peer.serve() as url:
        child = Agent(
            name="Worker",
            instructions="Run the function.",
            provider=provider(responses),
            tools=[leaf],
            on_tool_lifecycle=agent_observer,
        )
        records = HttpMCP(name="records", url=url, tools=["search"], approval_mode="preapproved")
        root = Agent(
            name="Coordinator",
            instructions="Use each tool.",
            provider=provider(responses),
            tools=[direct, records, child.as_tool("nested")],
            on_tool_lifecycle=agent_observer,
        )
        expected_agents.update(Coordinator=root, Worker=child)
        async with root.session(context=application, on_tool_lifecycle=run_observer) as session:
            assert (
                await (await session.submit("Read.")).get_result()
            ).final_output == "root complete"
            history = json.dumps((await session.history_snapshot()).items)
    assert effects == ["direct", "leaf"]
    assert len(peer.calls) == 1 and len(responses.requests) == 6
    for scope in ("run", "agent"):
        assert {
            (event.tool.name, event.kind) for owner, event in observations if owner == scope
        } == {
            (name, phase)
            for name in ("direct", "search", "nested", "leaf")
            for phase in ("start", "end")
        }
        assert sum(owner == scope for owner, _ in observations) == 8
    for name in ("direct", "leaf"):
        body = timeline.index(("effect", name, "body"))
        for scope in ("run", "agent"):
            assert timeline.index((scope, name, "start")) < body
            assert timeline.index((scope, name, "end")) > body
    assert timeline.index(("run", "nested", "start")) < timeline.index(("run", "leaf", "start"))
    assert timeline.index(("run", "leaf", "end")) < timeline.index(("run", "nested", "end"))
    for _, event in observations:
        if event.output is not None:
            event.output["output"] = "observer mutation"
    assert "observer mutation" not in history
    assert "direct read" in json.dumps(responses.requests[1])


class ObserverFailure(RuntimeError):
    pass


@pytest.mark.parametrize("phase", ["start", "end"])
async def test_central_observer_failure_keeps_actual_effect_and_committed_history(responses, phase):
    effects, seen = [], []

    @function_tool
    def read() -> str:
        effects.append("read")
        return "committed-value"

    async def observe(event):
        seen.append(event.kind)
        if event.kind == phase:
            raise ObserverFailure("synthetic observer failure")

    responses.respond(function_call("read", {}, "observer-error"))
    agent = Agent(instructions="Use read.", provider=provider(responses), tools=[read])
    async with timeout(20), agent.session(on_tool_lifecycle=observe) as session:
        turn = await session.submit("Read.")
        with pytest.raises(ObserverFailure, match="synthetic observer failure"):
            await turn.get_result()
        assert len(responses.requests) == 1
        assert effects == ([] if phase == "start" else ["read"])
        if phase == "end":
            history = json.dumps((await session.history_snapshot()).items)
            assert "committed-value" in history
            saved = await session.save_state()
    assert seen == (["start"] if phase == "start" else ["start", "end"])
    if phase == "end":
        responses.respond(assistant_output("restored"))
        assert (await agent.run("Continue.", state=saved)).final_output == "restored"
        assert effects == ["read"]
        assert "committed-value" in json.dumps(responses.requests[-1])


@pytest.mark.parametrize("allowed", [False, True])
async def test_central_observer_follows_existing_function_approval(responses, allowed):
    observations, effects = [], []

    @function_tool(needs_approval=True)
    def read() -> str:
        effects.append("read")
        return "read"

    async def observe(event):
        observations.append(event.kind)

    responses.respond(function_call("read", {}, "approved-read"))
    responses.respond(assistant_output("done"))
    agent = Agent(instructions="Use read.", tools=[read], provider=provider(responses))
    async with timeout(20), agent.session(on_tool_lifecycle=observe) as session:
        turn = await session.submit("Read.")
        request = await next_request(session.interactions(), read)
        assert observations == [] and effects == []
        if allowed:
            await request.approve()
        else:
            await request.reject("denied")
        assert (await turn.get_result()).final_output == "done"
    assert effects == (["read"] if allowed else [])
    assert observations == (["start", "end"] if allowed else [])


async def test_central_observer_does_not_bypass_before_policy(responses):
    observations, effects = [], []

    def before(_context):
        return Deny("denied before dispatch")

    @function_tool(before=before)
    def read() -> str:
        effects.append("read")
        return "read"

    async def observe(event):
        observations.append(event.kind)

    responses.respond(function_call("read", {}, "denied-read"))
    responses.respond(assistant_output("done"))
    result = await Agent(
        instructions="Use read.",
        tools=[read],
        provider=provider(responses),
        on_tool_lifecycle=observe,
    ).run("Read.")
    assert result.final_output == "done"
    assert effects == [] and observations == []


async def test_central_run_observer_does_not_bypass_input_guardrail(responses):
    observations, effects = [], []

    async def guard(_context, _agent, _input, _session):
        return GuardrailResult(True, "blocked")

    @function_tool
    def read() -> str:
        effects.append("read")
        return "read"

    async def observe(event):
        observations.append(event.kind)

    agent = Agent(
        instructions="Use read.",
        tools=[read],
        provider=provider(responses),
        input_guardrails=[InputGuardrail("block", guard, run_in_parallel=False)],
    )
    with pytest.raises(GuardrailTripwire):
        await agent.run("Read.", on_tool_lifecycle=observe)
    assert effects == [] and observations == [] and responses.requests == []
