"""Original Responses objects use the live native turn observation stream."""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import BaseModel

from agents import Agent, FunctionTool, InteractionRequired, NativeError, NativeProvider
from agents.observation import OutputTextDelta, RawProviderEvent
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    text_response,
)
from tests.test_native_child_agents import agent_for, child_peer, handles, metadata, trust
from tests.test_native_executor_tools import declaration
from tests.test_native_provider_websockets import peer as websocket_peer

PRIVATE_MARKER = "SYNTHETIC_RAW_PROVIDER_EXTENSION_c8e34b"


@pytest.mark.requires_native_linux_sandbox
async def test_raw_child_frames_follow_the_admitted_child_turn(
    tmp_path, monkeypatch, native_executor_binary
):
    monkeypatch.setenv("CODEX_EXEC_SERVER_TEST_BINARY", str(native_executor_binary))
    opaque = {"type": "response.sdk_fixture.child", "private": PRIVATE_MARKER}
    following = {"type": "response.sdk_fixture.following_child", "value": 0}
    async with (
        timeout(30),
        child_peer(("default",)) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        original_response = peer.response
        child_requests = 0

        def respond():
            nonlocal child_requests
            meta = metadata(peer.requests[-1], peer.request_headers[-1])
            if meta["agent_name"] == "/root":
                return original_response()
            child_requests += 1
            return [
                opaque if child_requests == 1 else following,
                assistant_output(f"child {child_requests}"),
            ]

        monkeypatch.setattr(peer, "response", respond)
        trust(executor, tmp_path, monkeypatch)
        async with agent_for(peer, executor).session(
            environment=declaration(executor),
            include_raw_provider_events=True,
            observation_retention="all",
        ) as session:
            root = await session.submit("Delegate.")
            assert (await root.get_result()).final_output == "root complete"
            (turn,) = await handles(session, 1)
            assert (await turn.get_result()).final_output == "child 1"
            raw = [
                event
                async for event in turn.events(include_execution_details=True)
                if isinstance(event, RawProviderEvent)
            ]
            assert opaque in [event.payload for event in raw]
            assert all(
                event.turn_id == turn.turn_id and event.submission_id == turn.submission_id
                for event in raw
            )
            assert not [
                event async for event in turn.events() if isinstance(event, RawProviderEvent)
            ]
            assert opaque not in [
                event.payload
                async for event in root.events(include_execution_details=True)
                if isinstance(event, RawProviderEvent)
            ]
            (child,) = [child async for child in session.subagents()]
            next_turn = await child.start("Continue.")
            assert (await next_turn.get_result()).final_output == "child 2"
            next_raw = [
                event.payload
                async for event in next_turn.events(include_execution_details=True)
                if isinstance(event, RawProviderEvent)
            ]
            assert following in next_raw
            assert opaque not in next_raw
            assert PRIVATE_MARKER not in (await session.save_state()).to_json()


@pytest.mark.parametrize("transport", ["sse", "websocket"])
async def test_raw_frames_keep_provider_order_and_stay_in_the_observation_stream(
    transport, monkeypatch
):
    from _agents_codex import observation

    first = {
        "type": "response.sdk_fixture.opaque",
        "extension": {"private": PRIVATE_MARKER, "parts": [None, False, 0]},
    }
    malformed = {
        "type": "response.output_text.delta",
        "item_id": "msg-raw",
        "delta": 42,
    }
    events = text_response("msg-raw", "hello")
    second = {"type": "response.sdk_fixture.second", "unmodeled": {"n": 0}}
    async with timeout(30), websocket_peer() as peer:
        peer.respond(first, malformed, *events)
        peer.respond(second, assistant_output("again"))
        peer.respond(first, assistant_output("quiet"))
        agent = Agent(
            instructions="Answer.",
            provider=peer.provider(websocket=transport == "websocket"),
        )
        async with agent.session(
            include_raw_provider_events=True, observation_retention="all"
        ) as session:
            turn = await session.submit("first")
            assert (await turn.get_result()).final_output == "hello"
            detailed = [event async for event in turn.events(include_execution_details=True)]
            raw = [event for event in detailed if isinstance(event, RawProviderEvent)]
            assert [event.payload["type"] for event in raw] == [
                "response.created",
                first["type"],
                malformed["type"],
                *(event["type"] for event in events),
                "response.completed",
            ]
            assert raw[1].payload == first
            assert raw[2].payload == malformed
            assert all(
                event.source == "responses"
                and event.transport == transport
                and event.turn_id == turn.turn_id
                and event.submission_id == turn.submission_id
                for event in raw
            )
            text = next(event for event in detailed if isinstance(event, OutputTextDelta))
            assert raw[4].payload["delta"] == text.delta == "hello"
            assert raw[4].sequence < text.sequence
            assert not [
                event async for event in turn.events() if isinstance(event, RawProviderEvent)
            ]
            raw[1].payload["extension"] = None
            replay = [
                event
                async for event in turn.events(include_execution_details=True)
                if isinstance(event, RawProviderEvent)
            ]
            assert replay[1].payload == first

            following = await session.submit("second")
            assert (await following.get_result()).final_output == "again"
            following_raw = [
                event.payload
                async for event in following.events(include_execution_details=True)
                if isinstance(event, RawProviderEvent)
            ]
            assert following_raw[1] == second
            assert first not in following_raw

            original_deepcopy = observation.deepcopy
            copied_later_payloads = []

            def track_copy(value):
                if isinstance(value, dict) and value.get("type") == second["type"]:
                    copied_later_payloads.append(value)
                return original_deepcopy(value)

            with monkeypatch.context() as patch:
                patch.setattr(observation, "deepcopy", track_copy)
                earlier_raw = [
                    event.payload
                    async for event in turn.events(include_execution_details=True)
                    if isinstance(event, RawProviderEvent)
                ]
            assert earlier_raw[1] == first
            assert not copied_later_payloads
            assert PRIVATE_MARKER not in (await session.save_state()).to_json()
            assert PRIVATE_MARKER not in json.dumps([row async for row in session.turns()])
        async with agent.session() as quiet_session:
            quiet = await quiet_session.submit("third")
            assert (await quiet.get_result()).final_output == "quiet"
            assert not [
                event
                async for event in quiet.events(include_execution_details=True)
                if isinstance(event, RawProviderEvent)
            ]
        if transport == "websocket":
            assert peer.warmups > 0
            assert not peer.http_requests


async def test_raw_buffered_response_keeps_the_original_object():
    body = {
        "id": "resp-raw-buffered",
        "status": "completed",
        "output": [assistant_output("done")["item"]],
        "provider_extension": {"opaque": PRIVATE_MARKER},
    }

    async def respond(request: web.Request) -> web.Response:
        assert (await request.json())["stream"] is False
        return web.json_response(body)

    app = web.Application()
    app.router.add_post("/v1/responses", respond)
    async with timeout(30), TestServer(app) as server:
        provider = NativeProvider(
            base_url=str(server.make_url("/v1")),
            api_key="synthetic-raw-key",
            response_mode="buffered",
        )
        async with (
            provider,
            Agent(instructions="Answer.", model="fixture", provider=provider).session(
                include_raw_provider_events=True
            ) as session,
        ):
            turn = await session.submit("answer")
            assert (await turn.get_result()).final_output == "done"
            raw = [
                event
                async for event in turn.events(include_execution_details=True)
                if isinstance(event, RawProviderEvent)
            ]
            assert [(event.transport, event.payload) for event in raw] == [("json", body)]
            assert PRIVATE_MARKER not in (await session.save_state()).to_json()


async def test_raw_failed_response_is_observable_before_the_turn_error(responses):
    frame = {
        "type": "response.failed",
        "response": {
            "id": "resp-invalid",
            "status": "failed",
            "error": {"code": "invalid_prompt", "message": "synthetic invalid prompt"},
        },
        "provider_extension": PRIVATE_MARKER,
    }
    responses.respond(frame)
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-raw-key")
    async with (
        timeout(30),
        provider,
        Agent(instructions="Answer.", provider=provider).session(
            include_raw_provider_events=True
        ) as session,
    ):
        turn = await session.submit("invalid")
        with pytest.raises(NativeError, match="synthetic invalid prompt"):
            await turn.get_result()
        raw = [
            event.payload
            async for event in turn.events(include_execution_details=True)
            if isinstance(event, RawProviderEvent)
        ]
        assert raw[-1] == frame
        assert PRIVATE_MARKER not in (await session.save_state()).to_json()


async def test_raw_capture_arms_before_a_restored_manual_turn_continues(responses):
    class LookupArgs(BaseModel):
        key: str

    lookup = FunctionTool(
        name="lookup", description="Look up a record.", input_type=LookupArgs, output_type=str
    )
    responses.respond(function_call("lookup", {"key": "a"}, "call-raw"))
    provider = NativeProvider(base_url=responses.url, api_key="synthetic-raw-key")
    agent = Agent(instructions="Use lookup once.", provider=provider, tools=[lookup])

    def answer(event):
        action = lookup.match(event)
        assert action is not None
        return action.reply("accepted")

    frame = {"type": "response.sdk_fixture.restored", "provider_extension": PRIVATE_MARKER}
    async with timeout(30), provider:
        with pytest.raises(InteractionRequired) as pending:
            await agent.run("Look up a.")
        state = pending.value.state
        assert state is not None and state.native_continuation is not None
        responses.respond(frame, assistant_output("done"))
        async with agent.session(
            state=state, on_interaction=answer, include_raw_provider_events=True
        ) as session:
            turn = session.restored_turn
            assert turn is not None
            assert (await turn.get_result()).final_output == "done"
            raw = [
                event
                async for event in turn.events(include_execution_details=True)
                if isinstance(event, RawProviderEvent)
            ]
            assert raw[1].payload == frame
            assert all(event.turn_id == turn.turn_id for event in raw)
