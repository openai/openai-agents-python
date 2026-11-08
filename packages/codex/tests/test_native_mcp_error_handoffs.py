"""Real issued handles keep captured MCP values out of public error handoffs."""

from __future__ import annotations

import asyncio
import gc
from contextlib import aclosing
from dataclasses import replace

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    ApprovalRequest,
    FormRequest,
    FunctionTool,
    HostedVerificationMCP,
    HttpMCP,
    UrlRequest,
    VerificationRequest,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_agent import provider
from tests.test_native_child_agents import agent_for, child_peer, handles, trust
from tests.test_native_executor_tools import declaration
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_mcp_configuration_values import _SECRET, assert_private_frames
from tests.test_native_mcp_tools import Peer, mcp_call
from tests.test_native_scoped_policies import AskingPeer, selected
from tests.test_native_user_verification import Enrollment, VerificationPeer, select


def private(error: BaseException) -> None:
    assert_private_frames(error, allow_chain=True, application_files=(__file__,))


async def test_observation_reader_rejects_concurrent_read_without_retaining_session(
    responses: ResponsesFixture,
) -> None:
    server = HttpMCP(
        name="credentials", url="http://127.0.0.1:1/mcp", tools=[], headers={"X-Token": _SECRET}
    )
    async with timeout(15), provider(responses) as client:
        async with Agent(instructions="test", tools=[server]).session(provider=client) as session:
            stream = session.events()
            first = asyncio.create_task(anext(stream))
            try:
                await asyncio.sleep(0)
                assert not first.done()
                with pytest.raises(RuntimeError, match="pending reader") as caught:
                    await anext(stream)
                private(caught.value)
            finally:
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)
                await stream.aclose()
    assert not responses.requests


async def test_live_subagent_rejects_busy_start_and_wrong_turn_without_retaining_session(
    tmp_path, monkeypatch
) -> None:
    async with (
        timeout(35),
        child_peer(("default",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
        Peer().serve() as mcp_url,
    ):
        trust(executor, tmp_path, monkeypatch)
        app = agent_for(peer, executor)
        captured = HttpMCP(name="credentials", url=mcp_url, tools=[], headers={"X-Token": _SECRET})
        app = replace(app, tools=[*app.tools, captured])
        try:
            async with app.session(environment=declaration(executor)) as session:
                root = await session.submit("Start one child")
                [turn] = await handles(session, 1)
                [child] = [item async for item in session.subagents()]
                try:
                    with pytest.raises((RuntimeError, ValueError)) as caught:
                        await child.start("A busy child must reject this input")
                    private(caught.value)
                    with pytest.raises((RuntimeError, ValueError)) as caught:
                        await child.try_cancel(root)  # type: ignore[arg-type]
                    private(caught.value)
                finally:
                    peer.release["alpha"].set()
                assert (await turn.get_result()).final_output == "alpha complete"
                assert (await root.get_result()).final_output == "root complete"
        finally:
            peer.release["alpha"].set()
            await app.provider.aclose()


@pytest.mark.parametrize("kind", ["form", "url", "approval"])
async def test_expired_real_mcp_request_handoffs_do_not_retain_session(
    responses: ResponsesFixture, kind: str
) -> None:
    peer = (
        Peer(names=("find-record",), annotations=None)
        if kind == "approval"
        else AskingPeer(unsupported=kind == "url")
    )
    async with timeout(20), peer.serve() as url, provider(responses) as client:
        if kind == "approval":
            responses.respond(mcp_call("find_record", "mcp__records", "approval"))
            tools = ["find-record"]
        else:
            responses.respond(selected(kind, "ordinary"))
            tools = ["search"]
        responses.respond(assistant_output("done"))
        server = HttpMCP(
            name="records",
            url=url,
            tools=tools,
            headers={"X-Token": _SECRET},
            approval_mode="native" if kind == "approval" else "preapproved",
        )
        async with Agent(instructions="test", tools=[server]).session(provider=client) as session:
            await session.submit("Ask for a real request")
            async with aclosing(session.interactions()) as cursor:
                request = await anext(cursor)
            assert isinstance(
                request, {"form": FormRequest, "url": UrlRequest, "approval": ApprovalRequest}[kind]
            )
        before = len(responses.requests)
        if kind == "form":
            operations = [lambda: request.respond({}), lambda: request.decline()]
        elif kind == "url":
            operations = [lambda: request.decline(), lambda: request.cancel_url_completion()]
        else:
            operations = [lambda: request.respond("deny")]
        for operation in operations:
            with pytest.raises(
                (RuntimeError, ValueError), match="expired|closed|answered"
            ) as caught:
                await operation()
            private(caught.value)
        assert len(responses.requests) == before
        assert server.headers == {"X-Token": _SECRET}


async def test_manual_action_response_handoff_does_not_retain_mcp_session(
    responses: ResponsesFixture,
) -> None:
    class Answer(BaseModel):
        value: int

    tool = FunctionTool(
        name="lookup", description="Lookup", input_type=LookupArgs, output_type=Answer
    )
    responses.respond(function_call("lookup", {"key": "a"}, "manual"))
    async with timeout(20), Peer().serve() as url, provider(responses) as client:
        server = HttpMCP(name="credentials", url=url, tools=[], headers={"X-Token": _SECRET})
        async with Agent(instructions="test", tools=[tool, server]).session(
            provider=client
        ) as session:
            await session.submit("lookup")
            async with aclosing(session.interactions()) as cursor:
                request = await next_request(cursor, tool)
            with pytest.raises(ValueError, match="failed validation") as caught:
                await request.respond({"value": "invalid"})
            private(caught.value)
        before = len(responses.requests)
        with pytest.raises((RuntimeError, ValueError), match="expired|closed|answered") as caught:
            await request.respond({"value": 1})
        private(caught.value)
        assert len(responses.requests) == before


async def test_verification_expired_owner_still_protects_captured_registration(
    responses: ResponsesFixture, tmp_path
) -> None:
    peer = VerificationPeer(Enrollment(tmp_path))
    async with timeout(25), peer.serve() as url, provider(responses) as client:
        registration = HostedVerificationMCP(
            url=url, tools=["search"], approval_mode="preapproved", headers={"X-Token": _SECRET}
        )
        responses.respond(select("verification"))
        async with Agent(instructions="test", tools=[registration]).session(
            provider=client
        ) as session:
            turn = await session.submit("Request verification")
            async with aclosing(session.interactions()) as cursor:
                request = await anext(cursor)
            assert isinstance(request, VerificationRequest)
            assert request._registration is registration
        owner = request._session
        del turn, session
    del client
    await asyncio.sleep(0)
    gc.collect()
    assert owner() is None, "The real closed Session must be collected before fallback is qualified"
    with pytest.raises((RuntimeError, ValueError), match="expired|closed|answered") as caught:
        await request.decline()
    private(caught.value)
    assert registration.headers == {"X-Token": _SECRET}
