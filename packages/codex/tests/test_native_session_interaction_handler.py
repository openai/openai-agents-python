"""Session-owned handlers serve retained work and genuinely Session-scoped URLs."""

import asyncio

import pytest
from pydantic import BaseModel

from agents import Agent, FunctionTool, InteractionExpiredError, UrlRequest
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_manual_tools import provider
from tests.test_native_root_url_lifetime import RootLifetimePeer
from tests.test_native_scoped_policies import selected
from tests.test_native_url_completion import app


class Lookup(BaseModel):
    key: str


async def test_handler_survives_turns_and_explicit_delivery_is_reusable(
    responses: ResponsesFixture,
) -> None:
    lookup = FunctionTool(name="lookup", description="", input_type=Lookup, output_type=str)
    received = []

    async def decide(request):
        received.append(request.call_id)
        return request.reply(request.arguments.key + "-accepted")

    responses.respond(function_call("lookup", {"key": "first"}, "first-call"))
    responses.respond(assistant_output("first result"))
    responses.respond(function_call("lookup", {"key": "second"}, "second-call"))
    responses.respond(assistant_output("second result"))
    agent = Agent(instructions="Use lookup", tools=[lookup], provider=provider(responses))
    async with timeout(20), agent.session(on_interaction=decide) as session:
        first = await session.submit("First")
        assert (await first.get_result()).final_output == "first result"
        assert not session._closed
        second = await session.submit("Second")
        assert (await second.get_result()).final_output == "second result"
    assert received == ["first-call", "second-call"]

    responses.respond(function_call("lookup", {"key": "explicit"}, "explicit-call"))
    responses.respond(assistant_output("explicit result"))
    async with timeout(20), agent.session() as session:
        turn = await session.submit("Explicit")
        request = await anext(session.interactions())
        reply = await decide(request)
        await session.respond(reply)
        with pytest.raises(InteractionExpiredError):
            await session.respond(reply)
        assert (await turn.get_result()).final_output == "explicit result"


async def test_session_handler_receives_url_without_forged_turn_identity(
    responses: ResponsesFixture,
) -> None:
    peer = RootLifetimePeer()
    received = []

    def decide(request):
        assert isinstance(request, UrlRequest)
        assert request.mode == "url"
        assert request.submission_id is None and request.turn_id is None
        received.append(request)
        return request.reply_url_accept()

    async with timeout(24), peer.serve() as url:
        responses.respond(selected("session-handler-url", "ordinary"))
        responses.respond(assistant_output("accepted URL"))
        async with app(responses, url).session(on_interaction=decide) as session:
            turn = await session.submit("Go")
            assert (await turn.get_result()).final_output == "accepted URL"
            assert len(received) == 1
            assert not session._closed
            peer.complete.set()
            await received[0].wait_url_completion(timeout=8)


async def test_session_scoped_handler_failure_reaches_session_and_active_work(
    responses: ResponsesFixture,
) -> None:
    peer = RootLifetimePeer()
    primary = ValueError("synthetic handler failure")

    def decide(request):
        assert request.submission_id is None
        raise primary

    async with timeout(24), peer.serve() as url:
        responses.respond(selected("session-handler-failure", "ordinary"))
        with pytest.raises(ValueError) as scope_error:
            async with app(responses, url).session(on_interaction=decide) as session:
                turn = await session.submit("Go")
                with pytest.raises(ValueError) as turn_error:
                    await turn.get_result()
                assert turn_error.value is primary
                with pytest.raises(ValueError) as session_error:
                    await session.pending_interactions()
                assert session_error.value is primary
        assert scope_error.value is primary


async def test_close_joins_started_handler(responses: ResponsesFixture) -> None:
    lookup = FunctionTool(name="lookup", description="", input_type=Lookup, output_type=str)
    entered, joined = asyncio.Event(), asyncio.Event()

    async def decide(request):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            joined.set()

    responses.respond(function_call("lookup", {"key": "blocked"}, "blocked-call"))
    agent = Agent(instructions="Use lookup", tools=[lookup], provider=provider(responses))
    async with timeout(20):
        async with agent.session(on_interaction=decide) as session:
            await session.submit("Wait")
            await entered.wait()
        assert joined.is_set()


async def test_turn_handler_failure_does_not_close_the_retained_session(
    responses: ResponsesFixture,
) -> None:
    lookup = FunctionTool(name="lookup", description="", input_type=Lookup, output_type=str)
    primary = ValueError("synthetic turn handler failure")

    async def decide(request):
        if request.arguments.key == "first":
            raise primary
        return request.reply("accepted")

    responses.respond(function_call("lookup", {"key": "first"}, "first-call"))
    responses.respond(function_call("lookup", {"key": "second"}, "second-call"))
    responses.respond(assistant_output("second result"))
    agent = Agent(instructions="Use lookup", tools=[lookup], provider=provider(responses))
    async with timeout(20), agent.session(on_interaction=decide) as session:
        first = await session.submit("First")
        with pytest.raises(ValueError) as turn_error:
            await first.get_result()
        assert turn_error.value is primary
        assert session._interaction_error is None and not session._closed
        second = await session.submit("Second")
        assert (await second.get_result()).final_output == "second result"
