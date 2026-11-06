"""Released one-shot state, returned replies and current restore bindings."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

import pytest
from pydantic import BaseModel

from agents import (
    ActionReply,
    Agent,
    ApprovalRequest,
    ElicitationRequest,
    FunctionTool,
    HttpMCP,
    Interaction,
    InteractionRequired,
    NativeProvider,
    PromptedOutput,
    Session,
    SessionState,
    ToolContext,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)


class Lookup(BaseModel):
    key: str


def manual() -> FunctionTool:
    return FunctionTool(name="lookup", description="Read", input_type=Lookup, output_type=str)


def provider(responses: ResponsesFixture) -> NativeProvider:
    return NativeProvider(base_url=responses.url, api_key="synthetic-reset-agent")


async def invoke(sync, agent, input, **options):
    if sync:
        return await asyncio.to_thread(agent.run_sync, input, **options)
    return await agent.run(input, **options)


@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize("bad", [None, 0, 1, "yes"])
async def test_bool_guard_before_any_open(sync, bad, responses):
    agent = Agent(instructions="")

    def unexpected(**kwargs):
        pytest.fail("invalid return_state acquired resources")

    agent.session = unexpected
    with pytest.raises(TypeError, match="return_state must be a bool"):
        await invoke(sync, agent, "read", return_state=bad)
    assert not responses.requests


@pytest.mark.parametrize("sync", [False, True])
async def test_idle_state_capture_and_restore(sync, responses):
    agent = Agent(instructions="Keep record A.", provider=provider(responses))
    responses.respond(assistant_output("remembered"))
    async with timeout(20):
        first = await invoke(sync, agent, "Remember record A", return_state=True)
        assert isinstance(first.state, SessionState)
        assert "Remember record A" not in repr(first)
        state = SessionState.from_json(first.state.to_json())
        with pytest.raises(TypeError, match="input is required"):
            await invoke(sync, agent, None, state=state)
        responses.respond(assistant_output("record A"))
        second = await invoke(sync, agent, "Which record?", state=state)
    assert second.state is None and second.final_output == "record A"
    rendered = json.dumps(responses.requests[-1]["input"])
    assert (
        "Remember record A" in rendered and "remembered" in rendered and "Which record?" in rendered
    )


@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize("handler", [False, True])
async def test_capture_failure_is_primary_and_closes_before_return(
    sync, handler, responses, monkeypatch
):
    closed = []
    agent = Agent(instructions="", provider=provider(responses))
    original = agent.session
    problem = RuntimeError("checkpoint refused")

    @asynccontextmanager
    async def session(**kwargs):
        async with original(**kwargs) as live:
            try:
                yield live
            finally:
                closed.append(live.id)

    async def refuse(self):
        raise problem

    monkeypatch.setattr(Session, "save_state", refuse)
    agent.session = session
    responses.respond(assistant_output("done"))
    with pytest.raises(RuntimeError, match="checkpoint refused") as caught:
        await invoke(
            sync,
            agent,
            "go",
            return_state=True,
            on_interaction=(lambda q: q.reply("unused")) if handler else None,
        )
    assert caught.value is problem and len(closed) == 1
    responses.respond(assistant_output("next"))
    assert (await invoke(sync, agent, "again")).final_output == "next"


@pytest.mark.parametrize("sync", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
async def test_pending_manual_uses_destination_context_and_saved_turn(sync, explicit, responses):
    effects = []
    lookup = manual()

    @function_tool
    def remember(ctx: ToolContext[dict[str, str]], key: str) -> str:
        effects.append((ctx.context["owner"], key))
        return ctx.context["owner"]

    agent = Agent(instructions="Read.", tools=[lookup, remember], provider=provider(responses))
    responses.respond(
        function_call("remember", {"key": "before"}, "original"),
        function_call("lookup", {"key": "A"}, "pending"),
    )
    async with timeout(20):
        async with agent.session(context={"owner": "source"}) as source:
            turn = await source.submit("Read", tools=agent.tools if explicit else None)
            request = (await turn.wait_for_request_or_result()).requests[0]
            assert lookup.match(request) is not None
            state = SessionState.from_json((await source.suspend()).to_json())
            with pytest.raises(InteractionRequired):
                await turn.get_result()
        assert effects == [("source", "before")]
        assert (state.operation_tools is not None) == explicit
        before = len(responses.requests)
        with pytest.raises(ValueError, match="input=None"):
            await invoke(sync, agent, "new input", state=state)
        with pytest.raises(InteractionRequired) as missing:
            await invoke(sync, agent, None, state=state)
        assert missing.value.state is state
        assert len(responses.requests) == before
        responses.respond(function_call("remember", {"key": "after"}, "current"))
        responses.respond(assistant_output("complete"))

        def answer(event: Interaction) -> ActionReply[str]:
            match = lookup.match(event)
            assert match is not None and match.call_id == "pending"
            assert match.arguments.key == "A"
            return match.reply("accepted")

        result = await invoke(
            sync,
            agent,
            None,
            state=state,
            context={"owner": "destination"},
            on_interaction=answer,
            return_state=True,
            provider=provider(responses),
        )
    assert result.turn.turn_id == turn.turn_id and result.final_output == "complete"
    assert isinstance(result.state, SessionState) and result.repair_attempts == ()
    assert effects == [("source", "before"), ("destination", "after")]
    assert tool_output(responses.requests[-1], "current") == "destination"


@pytest.mark.parametrize("explicit", [False, True])
async def test_missing_restored_context_refuses_before_environment_grants_or_effects(
    explicit, responses
):
    effects = []
    lookup = manual()

    @function_tool
    def remember(ctx: ToolContext[dict[str, str]], key: str) -> str:
        effects.append((ctx.context["owner"], key))
        return key

    agent = Agent(instructions="Read.", tools=[lookup, remember], provider=provider(responses))
    responses.respond(function_call("lookup", {"key": "A"}, "pending"))
    async with timeout(20):
        async with agent.session(context={"owner": "source"}) as source:
            turn = await source.submit("read", tools=agent.tools if explicit else None)
            assert lookup.match((await turn.wait_for_request_or_result()).requests[0]) is not None
            state = SessionState.from_json((await source.suspend()).to_json())
            with pytest.raises(InteractionRequired):
                await turn.get_result()

        @asynccontextmanager
        async def forbidden_environment():
            effects.append("entered environment")
            pytest.fail("missing context entered managed Environment")
            yield  # pragma: no cover

        with pytest.raises(TypeError, match="requires application context"):
            async with agent.session(state=state, environment=forbidden_environment()):
                pytest.fail("missing context published a restored grant")
    assert effects == [] and len(responses.requests) == 1


async def test_unselected_callback_context_stays_delayed_on_saved_subset(responses):
    lookup = manual()

    @function_tool
    def requires_tenant(ctx: ToolContext[dict[str, str]], key: str) -> str:
        pytest.fail("unselected callback ran")

    agent = Agent(instructions="", tools=[lookup, requires_tenant], provider=provider(responses))
    responses.respond(function_call("lookup", {"key": "A"}, "pending"))
    async with timeout(20):
        async with agent.session() as source:
            turn = await source.submit("read", tools=[lookup])
            assert lookup.match((await turn.wait_for_request_or_result()).requests[0]) is not None
            state = await source.suspend()
            with pytest.raises(InteractionRequired):
                await turn.get_result()
        responses.respond(assistant_output("complete"))
        result = await agent.run(
            None, state=state, on_interaction=lambda request: request.reply("ok")
        )
    assert result.final_output == "complete" and result.state is None


@pytest.mark.parametrize("sync", [False, True])
async def test_no_self_responding_handler_or_none_protocol(sync, responses):
    agent = Agent(instructions="", tools=[manual()], provider=provider(responses))
    responses.respond(function_call("lookup", {"key": "A"}, "pending"))

    async def invalid(request):
        await request.respond("cannot deliver")
        return request.reply("cannot deliver twice")

    with pytest.raises(RuntimeError, match="cannot respond directly"):
        await invoke(sync, agent, "read", on_interaction=invalid)
    assert len(responses.requests) == 1


@pytest.mark.parametrize("capture", [False, True])
async def test_prompted_manual_restore_and_repair_receipts_remain_separate(capture, responses):
    lookup = manual()
    agent = Agent(
        instructions="Read.",
        tools=[lookup],
        provider=provider(responses),
        output_type=PromptedOutput(Lookup, max_retries=1),
    )
    responses.respond(function_call("lookup", {"key": "A"}, "pending"))
    async with timeout(20):
        async with agent.session() as source:
            turn = await source.submit("read")
            assert lookup.match((await turn.wait_for_request_or_result()).requests[0]) is not None
            state = await source.suspend()
            with pytest.raises(InteractionRequired):
                await turn.get_result()
        responses.respond(assistant_output("invalid"))
        responses.respond(assistant_output('{"key":"repaired"}'))
        replies = []

        def reply(event):
            replies.append(event)
            return event.reply("value")

        result = await agent.run(None, state=state, on_interaction=reply, return_state=capture)
        assert result.final_output == Lookup(key="repaired")
        assert len(result.repair_attempts) == 1
        attempt = result.repair_attempts[0]
        assert attempt["parent_turn_id"] == result.turn.turn_id
        assert len(replies) == 1 and len(responses.requests) == 3
        if capture:
            assert isinstance(result.state, SessionState)
            saved = SessionState.from_json(result.state.to_json())
            async with agent.session(state=saved) as restored:
                assert restored.restored_turn is None
                receipts = [row async for row in restored.turns() if "parent_turn_id" in row]
                assert receipts == list(result.repair_attempts)
                items = [row async for row in restored.items() if row["turn_id"] == attempt["id"]]
                assert items and "repaired" in json.dumps(items)
                assert len(replies) == 1 and len(responses.requests) == 3
        else:
            assert result.state is None


@pytest.mark.parametrize("kind", ["approval", "form"])
@pytest.mark.parametrize("accept", [True, False])
async def test_returned_native_question_reply_in_nested_run(kind, accept, responses):
    # These fixtures use real local MCP and Responses transports.
    from tests.test_native_mcp_tools import Peer
    from tests.test_native_scoped_policies import AskingPeer, selected

    peer = Peer(names=("search",), annotations=None) if kind == "approval" else AskingPeer()
    shared = provider(responses)
    received = []
    async with timeout(20), peer.serve() as url, shared:
        server = HttpMCP(
            name="records",
            url=url,
            tools=["search"],
            approval_mode="auto" if kind == "approval" else "approve",
        )
        child = Agent(instructions="Search.", tools=[server])

        async def answer(question: Interaction) -> ActionReply[Any]:
            received.append(question)
            if isinstance(question, ApprovalRequest):
                assert not peer.calls
                return question.reply("allow_once" if accept else "deny")
            assert isinstance(question, ElicitationRequest) and isinstance(peer, AskingPeer)
            assert not peer.answers
            return question.reply({"answer": "tenant"} if accept else None)

        @function_tool
        async def ask(key: str) -> str:
            return (await child.run(key, provider=shared, on_interaction=answer)).final_output

        responses.respond(function_call("ask", {"key": "search"}, "ask"))
        responses.respond(selected("inner", "key"))
        responses.respond(assistant_output("child done"))
        responses.respond(assistant_output("parent done"))
        result = await Agent(instructions="Ask.", tools=[ask]).run("go", provider=shared)
        assert result.final_output == "parent done"
        assert len(received) == 1
        assert tool_output(responses.requests[-1], "ask") == "child done"
        if isinstance(peer, AskingPeer):
            assert peer.answers[0]["result"]["action"] == ("accept" if accept else "decline")
        else:
            assert len(peer.calls) == int(accept)
