"""Issuing-profile metadata survives handoff and cannot outlive its native owner."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
from contextlib import AsyncExitStack

import pytest

from agents import Agent, HttpMCP, TurnStoppedError
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_agent import provider
from tests.test_native_code_mode import code_call
from tests.test_native_mcp_tools import Peer, mcp_call


@pytest.fixture
async def profile_peer():
    # Profile preparation may synchronously wait for MCP initialization. Keep the
    # remote peer independent of the caller's event loop, as a real server is.
    peer = Peer(names=("search",))
    ready = concurrent.futures.Future()

    async def serve():
        closed = asyncio.Event()
        async with peer.serve() as url:
            ready.set_result((url, asyncio.get_running_loop(), closed))
            await closed.wait()

    def worker():
        try:
            asyncio.run(serve())
        except BaseException as error:
            if not ready.done():
                ready.set_exception(error)
            else:
                errors.append(error)

    errors = []
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    url, loop, closed = await asyncio.to_thread(ready.result, 5)
    try:
        yield peer, url
    finally:
        loop.call_soon_threadsafe(closed.set)
        await asyncio.to_thread(thread.join, 5)
        assert not thread.is_alive()
        assert not errors


@pytest.fixture
def selection_records(monkeypatch):
    from agents.session import Session

    recorded = []
    original = Session._select_step_body

    async def observe(self, state, message):
        source = self._agent_for_message(message)
        recorded.append(
            {
                "profile_id": message.get("profile_id"),
                "profile_epoch": message.get("profile_epoch"),
                "generation": message.get("generation"),
                "candidates": message.get("candidates"),
                "defaults": message.get("defaults"),
                "source_model": source.model,
                "source_programmatic": source.programmatic_tool_calling,
                "operation_refs": None
                if state.operation_refs is None
                else [
                    {"name": ref.name, "namespace": ref.namespace} for ref in state.operation_refs
                ],
                "desired_mcp_servers": [server.name for server in self._mcp_tools],
                "default_tools_omitted": self._default_tools is None,
            }
        )
        await original(self, state, message)

    monkeypatch.setattr(Session, "_select_step_body", observe)
    return recorded


@pytest.mark.parametrize("in_code", [False, True], ids=["direct", "code"])
async def test_profile_only_metadata_uses_exact_issuing_callback(
    responses: ResponsesFixture, profile_peer, selection_records, in_code
):
    peer, url = profile_peer
    application = {"tenant": "controlled"}
    seen = []
    secret = "private-profile-b-metadata-7301"

    async def metadata(ctx):
        seen.append(ctx)
        assert ctx.context is application
        assert ctx.submission is turn
        assert ctx.call.tool.name == "search"
        assert ctx.call.route == ("code" if in_code else "direct")
        return {"app/profile": secret}

    target = Agent(
        name="B",
        instructions="B owns the only MCP declaration.",
        programmatic_tool_calling=in_code,
        tools=[HttpMCP(name="local", url=url, tools=("search",), request_metadata=metadata)],
    )
    source = Agent(
        name="A",
        instructions="Route to B.",
        handoffs=[target],
        programmatic_tool_calling=in_code,
        provider=provider(responses),
        model="gpt-5.5",
    )
    responses.respond(function_call("transfer_to_b", {}, "handoff-b"))
    responses.respond(
        code_call('text(await tools.mcp__local__search({key:"owned"}));', "profile-call")
        if in_code
        else mcp_call("search", "mcp__local", "profile-call")
    )
    responses.respond(assistant_output("profile accepted"))
    async with (
        timeout(30),
        source.session(context=application) as session,
    ):
        turn = await session.submit("Route and search")
        assert (await turn.get_result()).final_output == "profile accepted"
        assert await session.get_active_agent() is target
        if len(seen) != 1:
            print(json.dumps({"step_selections": selection_records}, default=str))
            print(
                json.dumps(
                    {
                        "response_requests": responses.requests,
                        "peer_requests": peer.requests,
                        "step_selections": selection_records,
                    },
                    default=str,
                )
            )
        assert len(seen) == 1
        assert len(peer.calls) == 1
        assert peer.calls[0]["_meta"]["app/profile"] == secret
        assert secret not in json.dumps(responses.requests)
        assert secret not in (await session.save_state()).to_json()
    assert len(responses.requests) == 3


@pytest.mark.parametrize("ending", ["cancel", "close"])
async def test_profile_metadata_retirement_joins_and_never_retargets(
    responses: ResponsesFixture, profile_peer, ending
):
    peer, url = profile_peer
    entered, cancelled, release, returned = (asyncio.Event() for _ in range(4))
    observed = []

    async def old_metadata(ctx):
        observed.append("B")
        entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            return {"app/profile": "retired-B"}
        finally:
            returned.set()

    def fresh_metadata(ctx):
        observed.append("C")
        return {"app/profile": "fresh-C"}

    target_b = Agent(
        name="B",
        instructions="B",
        tools=[HttpMCP(name="local", url=url, tools=("search",), request_metadata=old_metadata)],
    )
    target_c = Agent(
        name="C",
        instructions="C",
        tools=[HttpMCP(name="local", url=url, tools=("search",), request_metadata=fresh_metadata)],
    )
    source = Agent(
        name="A", instructions="A", handoffs=[target_b, target_c], provider=provider(responses)
    )
    responses.respond(function_call("transfer_to_b", {}, "handoff-b"))
    responses.respond(mcp_call("search", "mcp__local", "reused-call"))
    async with timeout(35), AsyncExitStack() as stack:
        session = await stack.enter_async_context(source.session())
        turn = await session.submit("Route to B and search")
        stopping = None
        try:
            await entered.wait()
            with pytest.raises(RuntimeError):
                await session.update(agent=target_c)
            stopping = asyncio.create_task(turn.cancel() if ending == "cancel" else stack.aclose())
            await cancelled.wait()
            assert not stopping.done()
            assert peer.calls == []
        finally:
            release.set()
            if stopping is not None:
                await stopping
        assert returned.is_set()
        assert peer.calls == []
        with pytest.raises(TurnStoppedError):
            await turn.get_result()
        if ending == "cancel":
            await session.update(agent=target_c)
            fresh_session = session
        else:
            fresh_session = await stack.enter_async_context(source.session())
            await fresh_session.update(agent=target_c)
        responses.respond(mcp_call("search", "mcp__local", "reused-call"))
        responses.respond(assistant_output("fresh accepted"))
        assert (
            await (await fresh_session.submit("Search with fresh C")).get_result()
        ).final_output == "fresh accepted"
        assert await fresh_session.get_active_agent() is target_c
    assert observed == ["B", "C"]
    assert len(peer.calls) == 1
    assert peer.calls[0]["_meta"]["app/profile"] == "fresh-C"
    assert "retired-B" not in json.dumps(peer.calls)


async def test_restored_profile_uses_fresh_metadata_callback_and_current_selection(
    responses: ResponsesFixture, profile_peer, selection_records
):
    peer, url = profile_peer
    seen = []

    def graph(label, application):
        def metadata(ctx):
            assert ctx.context is application
            seen.append((label, ctx.submission))
            return {"app/profile": label}

        target = Agent(
            name="B",
            instructions="B owns MCP",
            tools=[HttpMCP(name="local", url=url, tools=("search",), request_metadata=metadata)],
        )
        source = Agent(
            name="A",
            instructions="A",
            handoffs=[target],
            provider=provider(responses),
            model="gpt-5.5",
        )
        return source, target

    old_context = {"tenant": "old"}
    source, target = graph("private-old-profile-9813", old_context)
    responses.respond(function_call("transfer_to_b", {}, "handoff-b"))
    responses.respond(mcp_call("search", "mcp__local", "old-call"))
    responses.respond(assistant_output("old accepted"))
    async with timeout(30):
        async with source.session(context=old_context) as session:
            original = await session.submit("Route to B and search")
            assert (await original.get_result()).final_output == "old accepted"
            assert await session.get_active_agent() is target
            saved = await session.save_state()
        current_context = {"tenant": "current"}
        fresh_source, fresh_target = graph("private-fresh-profile-9813", current_context)
        responses.respond(mcp_call("search", "mcp__local", "fresh-call"))
        responses.respond(assistant_output("fresh accepted"))
        async with fresh_source.session(
            context=current_context, state=SessionState.from_json(saved.to_json())
        ) as restored:
            assert await restored.get_active_agent() is fresh_target
            current = await restored.submit("Search with restored B")
            assert (await current.get_result()).final_output == "fresh accepted"
            after = await restored.save_state()
    if len(seen) != 2:
        print(json.dumps({"step_selections": selection_records}, default=str))
        print(
            json.dumps(
                {
                    "response_requests": responses.requests,
                    "peer_requests": peer.requests,
                    "step_selections": selection_records,
                },
                default=str,
            )
        )
    assert seen == [("private-old-profile-9813", original), ("private-fresh-profile-9813", current)]
    assert len(peer.calls) == 2
    assert [call["_meta"]["app/profile"] for call in peer.calls] == [
        "private-old-profile-9813",
        "private-fresh-profile-9813",
    ]
    for private in ("private-old-profile-9813", "private-fresh-profile-9813"):
        assert private not in saved.to_json() + after.to_json() + json.dumps(responses.requests)
    assert len(responses.requests) == 5
