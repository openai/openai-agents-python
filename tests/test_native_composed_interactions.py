"""Native questions and cleanup belong to each explicitly opened independent Session."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import Any

import pytest
from pydantic import BaseModel

from agents import (
    ActionReply,
    ActionRequested,
    Agent,
    ApprovalRequest,
    ElicitationRequest,
    FunctionTool,
    HttpMCP,
    Interaction,
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
from tests.test_native_agent import provider
from tests.test_native_mcp_tools import Peer
from tests.test_native_scoped_policies import AskingPeer, selected


@dataclass
class Application:
    tenant: str


class ChildInput(BaseModel):
    input: str


class PolicyAgent(Agent):
    """Observe public Sessions to prove their independent lifetime."""

    @asynccontextmanager
    async def session(self, **kwargs: Any):
        async with super().session(**kwargs) as session:
            self.sessions.append(session)
            yield session


@pytest.mark.parametrize("kind", ["approval", "form"])
@pytest.mark.parametrize("accept", [True, False], ids=["accept", "decline"])
async def test_independent_root_question_routing_and_borrowed_provider(
    responses: ResponsesFixture, kind: str, accept: bool
) -> None:
    peer = Peer(names=("search",), annotations=None) if kind == "approval" else AskingPeer()
    shared = provider(responses)
    received = []
    app = Application("current-tenant")
    async with timeout(20), peer.serve() as url, shared:
        server = HttpMCP(
            name="records",
            url=url,
            tools=["search"],
            approval_mode="auto" if kind == "approval" else "approve",
        )
        child = PolicyAgent(instructions="Search in the child's own root.", tools=[server])
        child.sessions = []

        async def answer(
            outer: ToolContext[Application], question: Interaction
        ) -> ActionReply[Any]:
            [live] = child.sessions
            assert outer.context is app and outer.call_id == "ask"
            assert outer.submission is parent_turn
            assert question.session_id == live.id != parent.id
            assert question.subagent_id is None
            assert live.active_turn is not None
            assert (question.turn_id, question.submission_id) == (
                live.active_turn.turn_id,
                live.active_turn.submission_id,
            )
            received.append(question)
            with pytest.raises(RuntimeError, match="open or opening"):
                await shared.aclose()
            if isinstance(question, ApprovalRequest):
                assert kind == "approval" and not peer.calls
                return question.reply("allow_once" if accept else "deny")
            assert isinstance(question, ElicitationRequest) and isinstance(peer, AskingPeer)
            assert kind == "form" and not peer.answers
            return question.reply({"answer": outer.context.tenant} if accept else None)

        @function_tool(name="ask", description="Ask.")
        async def ask(outer: ToolContext[Application], input: str) -> str:
            result = await child.run(input, provider=shared, on_interaction=partial(answer, outer))
            return result.final_output

        responses.respond(function_call("ask", {"input": "search"}, "ask"))
        responses.respond(selected("inner", "key"))
        responses.respond(assistant_output("child done"))
        responses.respond(assistant_output("parent done"))
        async with Agent[Application](instructions="Ask.", tools=[ask]).session(
            context=app, provider=shared
        ) as parent:
            parent_turn = await parent.submit("search")
            assert (await parent_turn.get_result()).final_output == "parent done"
            assert len(received) == 1
            assert tool_output(responses.requests[-1], "ask") == "child done"
            assert "inner" not in json.dumps(responses.requests[-1])
            if isinstance(peer, AskingPeer):
                assert len(peer.answers) == 1
                result = peer.answers[0]["result"]
                assert result["action"] == ("accept" if accept else "decline")
                if accept:
                    assert result["content"] == {"answer": app.tenant}
            else:
                assert len(peer.calls) == int(accept)
            responses.respond(assistant_output("successor"))
            assert (
                await (await parent.submit("after child")).get_result()
            ).final_output == "successor"
            expired = received[0]
            with pytest.raises((RuntimeError, ValueError), match="closed|expired|answered"):
                if isinstance(expired, ApprovalRequest):
                    await expired.respond("allow_once")
                else:
                    assert isinstance(expired, ElicitationRequest)
                    await expired.respond({"answer": "old"})
        responses.respond(assistant_output("borrowed provider still usable"))
        assert (
            await Agent(instructions="Other session.").run("ready", provider=shared)
        ).final_output == "borrowed provider still usable"
    assert not responses.replies


async def test_simultaneous_same_id_forms_cannot_answer_parent_or_sibling_requests(
    responses: ResponsesFixture,
) -> None:
    left_peer, right_peer = AskingPeer(), AskingPeer()
    both, parent_ready, checked, release = (asyncio.Event() for _ in range(4))
    children = {}
    received: dict[str, ElicitationRequest] = {}
    crossed = []
    refused: list[RuntimeError] = []
    parent_request: ActionRequested[Any, Any] | None = None
    manual = FunctionTool(
        name="parent_ack", description="Parent only.", input_type=ChildInput, output_type=str
    )
    shared = provider(responses)
    async with timeout(25), left_peer.serve() as left, right_peer.serve() as right, shared:

        async def answer(
            outer: ToolContext[Application], question: Interaction
        ) -> ActionReply[Any]:
            assert isinstance(question, ElicitationRequest)
            name = outer.call_id
            [current] = children[name].sessions
            assert question.session_id == current.id and question.subagent_id is None
            assert outer.submission is turn and outer.context.tenant == "parent-tenant"
            received[name] = question
            if len(received) == 2:
                both.set()
            await both.wait()
            await parent_ready.wait()
            other = received["right" if name == "left" else "left"]
            assert other.session_id != question.session_id
            with pytest.raises(RuntimeError, match="cannot respond directly") as sibling:
                await other.respond({"answer": "wrong child"})
            refused.append(sibling.value)
            assert parent_request is not None
            with pytest.raises(RuntimeError, match="cannot respond directly") as parent_reply:
                await parent_request.respond("wrong parent")
            refused.append(parent_reply.value)
            crossed.append(name)
            if len(crossed) == 2:
                checked.set()
            await release.wait()
            return question.reply({"answer": name})

        for name, url in (("left", left), ("right", right)):
            child = PolicyAgent(
                instructions="Ask locally.",
                tools=[HttpMCP(name="records", url=url, tools=["search"], approval_mode="approve")],
            )
            child.sessions = []
            children[name] = child

        @function_tool(name="left")
        async def ask_left(outer: ToolContext[Application], input: str) -> str:
            result = await children["left"].run(
                input, provider=shared, on_interaction=partial(answer, outer)
            )
            return result.final_output

        @function_tool(name="right")
        async def ask_right(outer: ToolContext[Application], input: str) -> str:
            result = await children["right"].run(
                input, provider=shared, on_interaction=partial(answer, outer)
            )
            return result.final_output

        responses.respond(
            function_call("left", {"input": "one"}, "left"),
            function_call("right", {"input": "two"}, "right"),
            function_call("parent_ack", {"input": "parent"}, "ack"),
        )
        # Both servers ask form ID873 and both native roots use the same call ID.
        # Only owning Session authority can distinguish these requests.
        responses.respond(selected("same-inner", "key"))
        responses.respond(selected("same-inner", "key"))
        responses.respond(assistant_output("child"))
        responses.respond(assistant_output("child"))
        responses.respond(assistant_output("parent"))
        parent_agent = Agent[Application](
            instructions="Ask both.", tools=[ask_left, ask_right, manual]
        )
        async with parent_agent.session(
            context=Application("parent-tenant"), provider=shared
        ) as parent:
            cursor = parent.interactions()
            turn = await parent.submit("both")
            try:
                candidate = await anext(cursor)
                parent_request = manual.match(candidate)
                assert parent_request is not None and parent_request.session_id == parent.id
                parent_ready.set()
                await checked.wait()
                assert set(crossed) == {"left", "right"}
                assert not left_peer.answers and not right_peer.answers
                [pending] = await parent.pending_interactions()
                fresh_parent = manual.match(pending)
                assert fresh_parent is not None
                assert (
                    fresh_parent.session_id,
                    fresh_parent.submission_id,
                    fresh_parent.turn_id,
                    fresh_parent.call_id,
                ) == (
                    parent_request.session_id,
                    parent_request.submission_id,
                    parent_request.turn_id,
                    parent_request.call_id,
                )
                await parent_request.respond("current parent")
                release.set()
                assert (await turn.get_result()).final_output == "parent"
                assert left_peer.answers[0]["result"] == {
                    "action": "accept",
                    "content": {"answer": "left"},
                }
                assert right_peer.answers[0]["result"] == {
                    "action": "accept",
                    "content": {"answer": "right"},
                }
                assert len(left_peer.answers) == len(right_peer.answers) == 1
                assert len(refused) == 4
                assert all(type(exc) is RuntimeError for exc in refused), refused
            finally:
                parent_ready.set()
                release.set()
                await cursor.aclose()


async def test_cancelled_child_form_waits_for_handler_cleanup_and_rejects_late_answer(
    responses: ResponsesFixture,
) -> None:
    peer = AskingPeer()
    entered, closing, release, cleaned = (asyncio.Event() for _ in range(4))
    received = []
    shared = provider(responses)
    async with timeout(25), peer.serve() as url, shared:
        child = PolicyAgent(
            instructions="Ask.",
            tools=[HttpMCP(name="records", url=url, tools=["search"], approval_mode="approve")],
        )
        child.sessions = []

        async def answer(
            outer: ToolContext[Application], question: Interaction
        ) -> ActionReply[Any]:
            assert isinstance(question, ElicitationRequest)
            received.append(question)
            entered.set()
            try:
                await asyncio.Future()
                raise AssertionError("Cancelled handler resumed")
            finally:
                closing.set()
                await release.wait()
                cleaned.set()

        @function_tool(name="ask")
        async def ask(outer: ToolContext[Application], input: str) -> str:
            result = await child.run(input, provider=shared, on_interaction=partial(answer, outer))
            return result.final_output

        responses.respond(function_call("ask", {"input": "search"}, "outer"))
        responses.respond(selected("cancelled-inner", "key"))
        parent_agent = Agent[Application](instructions="Ask.", tools=[ask])
        async with parent_agent.session(context=Application("tenant"), provider=shared) as parent:
            turn = await parent.submit("ask")
            await entered.wait()
            stopping = asyncio.create_task(turn.cancel())
            try:
                await closing.wait()
                assert not stopping.done() and not cleaned.is_set()
                with pytest.raises(RuntimeError, match="open or opening"):
                    await shared.aclose()
                responses.respond(assistant_output("unrelated sibling"))
                assert (
                    await Agent(instructions="Sibling.").run("continue", provider=shared)
                ).final_output == "unrelated sibling"
            finally:
                release.set()
            await stopping
            assert cleaned.is_set()
            assert all(reply.get("result", {}).get("action") != "accept" for reply in peer.answers)
            responses.respond(assistant_output("parent continues"))
            assert (
                await (await parent.submit("new turn")).get_result()
            ).final_output == "parent continues"
            [old] = received
            with pytest.raises((RuntimeError, ValueError), match="expired|closed|answered"):
                await old.respond({"answer": "late"})
            [old_session] = child.sessions
            with pytest.raises(RuntimeError, match="closed"):
                await old_session.submit("new work on closed child")


async def test_nested_manual_interaction_preserves_enclosing_session_guards(
    responses: ResponsesFixture,
) -> None:
    manual = FunctionTool(
        name="lookup", description="Read a key.", input_type=ChildInput, output_type=str
    )
    leaf = PolicyAgent(instructions="Read the key.", tools=[manual])
    leaf.sessions = []
    observed = []
    shared = provider(responses)

    async def handle(outer: ToolContext[None], event: Interaction) -> ActionReply[str]:
        request = manual.match(event)
        assert request is not None
        ancestors = [session, *middle.sessions]
        assert outer.submission is ancestors[-1].active_turn
        for live in [*ancestors, *leaf.sessions]:
            turn = live.active_turn
            assert turn is not None
            async with timeout(2):
                with pytest.raises(RuntimeError, match="wait on its own turn"):
                    await asyncio.create_task(turn.get_result())
                with pytest.raises(RuntimeError, match="interrupt its own Session"):
                    await live.cancel_all()
            observed.append(live.id)
        assert (await earlier.get_result()).final_output == "earlier result"
        return request.reply("approved")

    @function_tool
    async def ask_leaf(outer: ToolContext[None], input: str) -> str:
        result = await leaf.run(input, provider=shared, on_interaction=partial(handle, outer))
        return result.final_output

    middle = PolicyAgent(instructions="Ask the leaf.", tools=[ask_leaf])
    middle.sessions = []

    @function_tool
    async def ask_middle(input: str) -> str:
        result = await middle.run(input, provider=shared)
        return result.final_output

    responses.respond(assistant_output("earlier result"))
    responses.respond(function_call("ask_middle", {"input": "delegate"}, "outer"))
    responses.respond(function_call("ask_leaf", {"input": "read"}, "leaf"))
    responses.respond(function_call("lookup", {"input": "sample"}, "lookup"))
    responses.respond(assistant_output("leaf answer"))
    responses.respond(assistant_output("middle answer"))
    responses.respond(assistant_output("parent answer"))

    parent = Agent(instructions="Ask.", tools=[ask_middle], provider=shared)
    async with timeout(25), shared, parent.session() as session:
        earlier = await session.submit("previous")
        assert (await earlier.get_result()).final_output == "earlier result"
        turn = await session.submit("read")
        assert (await turn.get_result()).final_output == "parent answer"
        assert observed == [session.id, middle.sessions[0].id, leaf.sessions[0].id]
        assert len(set(observed)) == 3
        assert tool_output(responses.requests[-1], "outer") == "middle answer"
    assert not responses.replies
