"""Request projections run through Core without rewriting accepted history."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import agents
from agents import Agent, ModelRequestContext, PromptedOutput, TurnStoppedError, function_tool
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_child_agents import ChildPeer, agent_for, handles, metadata, trust
from tests.test_native_executor_tools import declaration
from tests.test_native_provider_websockets import peer as websocket_peer


def reference(text: str) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }


def test_public_declaration_retains_callback_in_variants() -> None:
    assert agents.ModelRequestContext is ModelRequestContext
    assert "ModelRequestContext" in agents.__all__

    def callback(request):
        return None

    agent = Agent(instructions="Answer.", transform_context=callback)
    assert agent.with_output_type(PromptedOutput(int)).transform_context is callback
    assert replace(agent, name="Other").transform_context is callback
    with pytest.raises(TypeError, match="transform_context"):
        Agent(instructions="Answer.", transform_context="callback")


async def test_fresh_context_each_step_preserves_history_and_tools(
    responses: ResponsesFixture,
) -> None:
    application = {"reference": "reference-before-tool"}
    requests = []

    @function_tool
    def refresh() -> str:
        application["reference"] = "reference-after-tool"
        return "tool receipt"

    async def transform(request: ModelRequestContext[dict[str, str]]):
        assert request.context is application
        assert request.purpose == "turn" and request.subagent_id is None
        requests.append(request)
        return [
            *[item for item in request.items if "request-only-secret" not in json.dumps(item)],
            reference(request.context["reference"]),
        ]

    responses.respond(function_call("refresh", {}, "refresh-call"))
    responses.respond(assistant_output("answer"))
    async with (
        timeout(20),
        Agent(
            instructions="Use the reference.",
            tools=[refresh],
            provider=provider(responses),
            transform_context=transform,
        ).session(context=application) as session,
    ):
        turn = await session.submit("request-only-secret")
        assert (await turn.get_result()).final_output == "answer"
        saved = (await session.save_state()).to_json()
        assert "request-only-secret" in saved
        assert "reference-before-tool" not in saved and "reference-after-tool" not in saved
        assert all(request.session_id == session.id for request in requests)
        assert all(request.turn_id == turn.turn_id for request in requests)
    assert len(requests) == len(responses.requests) == 2
    first, second = map(json.dumps, responses.requests)
    assert "request-only-secret" not in first + second
    assert "reference-before-tool" in first and "reference-before-tool" not in second
    assert "reference-after-tool" in second
    assert tool_output(responses.requests[1], "refresh-call") == "tool receipt"
    assert requests[0].request_id != requests[1].request_id
    assert "reference-before-tool" not in repr(requests[0])


async def test_none_keeps_original_input_even_after_mutating_callback_copy(responses):
    def transform(request):
        for item in request.items:
            if item.get("role") == "user":
                item["content"] = [{"type": "input_text", "text": "discarded edit"}]
        return None

    responses.respond(assistant_output("answer"))
    agent = Agent(instructions="Answer.", provider=provider(responses), transform_context=transform)
    assert (await agent.run("original input")).final_output == "answer"
    assert "original input" in json.dumps(responses.requests[0])
    assert "discarded edit" not in json.dumps(responses.requests[0])


async def test_run_sync_applies_the_same_callback_and_application_context(responses):
    def transform(request: ModelRequestContext[str]):
        return [*request.items, reference(request.context)]

    responses.respond(assistant_output("answer"))
    agent = Agent(instructions="Answer.", provider=provider(responses), transform_context=transform)
    async with timeout(20):
        result = await asyncio.to_thread(agent.run_sync, "input", context="sync reference")
    assert result.final_output == "answer"
    assert "sync reference" in json.dumps(responses.requests[0])


@pytest.mark.parametrize("bad", ["not a sequence of items", [{"type": "message", "content": 3}]])
async def test_invalid_callback_output_never_reaches_provider(responses, bad):
    agent = Agent(
        instructions="Answer.",
        provider=provider(responses),
        transform_context=lambda request: bad,
    )
    async with timeout(15):
        with pytest.raises((TypeError, RuntimeError), match="context"):
            await agent.run("input")
    assert not responses.requests


async def test_callback_exception_stops_request_and_preserves_original_error(responses):
    problem = LookupError("reference unavailable")

    async def transform(request):
        raise problem

    agent = Agent(instructions="Answer.", provider=provider(responses), transform_context=transform)
    async with timeout(15):
        with pytest.raises(LookupError) as caught:
            await agent.run("input")
    assert caught.value is problem
    assert not responses.requests


async def test_cancel_joins_sync_callback_and_rejects_late_projection(responses):
    entered = Event()
    release = Event()
    exited = Event()
    calls = 0

    def transform(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            try:
                assert release.wait(15)
                return [*request.items, reference("late projection")]
            finally:
                exited.set()
        return [*request.items, reference("current projection")]

    agent = Agent(instructions="Answer.", provider=provider(responses), transform_context=transform)
    async with timeout(20), agent.session() as session:
        turn = await session.submit("cancel this")
        assert await asyncio.to_thread(entered.wait, 10)
        stopped = asyncio.create_task(turn.cancel())
        try:
            await asyncio.sleep(0)
            assert not stopped.done() and not responses.requests
        finally:
            release.set()
        await stopped
        assert exited.is_set()
        with pytest.raises(TurnStoppedError):
            await turn.get_result()
        assert not responses.requests
        responses.respond(assistant_output("survivor"))
        next_turn = await session.submit("new request")
        assert (await next_turn.get_result()).final_output == "survivor"
    request = json.dumps(responses.requests[0])
    assert "current projection" in request and "late projection" not in request


async def test_cancel_waits_for_async_cleanup_and_discards_suppressed_cancellation(responses):
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()

    async def transform(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()
            return [reference("late projection")]

    agent = Agent(instructions="Answer.", provider=provider(responses), transform_context=transform)
    async with timeout(20), agent.session() as session:
        turn = await session.submit("cancel this")
        await entered.wait()
        stopped = asyncio.create_task(turn.cancel())
        try:
            await cleaning.wait()
            assert not stopped.done() and not responses.requests
        finally:
            release.set()
        await stopped
        with pytest.raises(TurnStoppedError):
            await turn.get_result()
    assert not responses.requests


async def test_output_repair_and_compaction_use_current_application_policy(responses):
    purposes = []
    application = {"reference": "current-reference"}

    async def transform(request):
        assert request.context is application
        purposes.append(request.purpose)
        return [*request.items, reference(request.context["reference"])]

    agent = Agent(
        instructions="Answer with a number.",
        model="gpt-5.5",
        provider=provider(responses),
        output_type=PromptedOutput(int, max_retries=1),
        transform_context=transform,
    )
    responses.respond(assistant_output("invalid"))
    responses.respond(assistant_output("7"))
    responses.respond(assistant_output("Remember the number seven."))
    async with timeout(25), agent.session(context=application) as session:
        turn = await session.submit("A number")
        assert (await turn.get_result()).final_output == 7
        await session.compact()
    assert purposes == ["turn", "output_repair", "compaction"]
    assert all("current-reference" in json.dumps(request) for request in responses.requests)


async def test_restore_and_supplied_compaction_rebind_current_callback(responses):
    calls = []

    def first(request):
        calls.append("first")
        return [*request.items, reference("first reference")]

    def second(request):
        calls.append("second")
        return [*request.items, reference("second reference")]

    agent = Agent(instructions="Answer.", provider=provider(responses), transform_context=first)
    for answer in ("one", "two", "three", "four"):
        responses.respond(assistant_output(answer))
    async with timeout(25):
        async with agent.session() as session:
            assert (await (await session.submit("one")).get_result()).final_output == "one"
            saved = await session.save_state()
        async with replace(agent, transform_context=second).session(state=saved) as restored:
            assert (await (await restored.submit("two")).get_result()).final_output == "two"
            await restored.compact(summary="Remember the first two requests.")
            assert (await (await restored.submit("three")).get_result()).final_output == "three"
        async with replace(agent, transform_context=None).session(state=saved) as plain:
            assert (await (await plain.submit("four")).get_result()).final_output == "four"
    assert calls == ["first", "second", "second"]
    assert "first reference" not in saved.to_json()
    assert all("second reference" in json.dumps(request) for request in responses.requests[1:3])
    assert "reference" not in json.dumps(responses.requests[3])


@pytest.mark.parametrize("upgrade_status", [None, 426])
async def test_websocket_and_http_fallback_transform_once_without_unfiltered_warmup(upgrade_status):
    calls = []

    def transform(request):
        calls.append(request.request_id)
        return [reference("projected request")]

    async with timeout(20), websocket_peer(upgrade_status=upgrade_status) as peer:
        peer.respond(assistant_output("answer"))
        agent = Agent(instructions="Answer.", provider=peer.provider(), transform_context=transform)
        assert (await agent.run("original request")).final_output == "answer"
        assert len(calls) == len(peer.requests) == 1
        assert peer.warmups == 0
        assert "projected request" in json.dumps(peer.requests)
        assert "original request" not in json.dumps(peer.requests)
        assert len(peer.http_requests) == (1 if upgrade_status else 0)


async def test_http_retry_reuses_one_projection():
    attempts = []
    calls = []
    fixture = ResponsesFixture()
    fixture.respond(assistant_output("answer"))

    async def handle(request):
        attempts.append(await request.json())
        if len(attempts) == 1:
            return web.json_response(
                {"error": {"message": "temporarily unavailable"}},
                status=503,
                headers={"Retry-After": "0"},
            )
        return await fixture.handle(request)

    def transform(request):
        calls.append(request.request_id)
        return [reference("projected request")]

    app = web.Application()
    app.router.add_post("/v1/responses", handle)
    async with timeout(20), TestServer(app) as server:
        fixture.url = str(server.make_url("/v1"))
        agent = Agent(
            instructions="Answer.", provider=provider(fixture), transform_context=transform
        )
        assert (await agent.run("original request")).final_output == "answer"
    assert len(attempts) == 2 and len(calls) == 1
    assert attempts[0]["input"] == attempts[1]["input"]
    assert reference("projected request") in attempts[0]["input"]
    assert "original request" not in json.dumps(attempts)


class ContextChildPeer(ChildPeer):
    def response(self):
        request = self.requests[-1]
        identity = metadata(request, self.request_headers[-1])
        if identity["agent_name"] == "/root":
            return super().response()
        return [assistant_output("child complete")]


async def test_native_child_inherits_policy_with_distinct_request_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls = []
    application = {"reference": "shared reference"}

    async def transform(request):
        assert request.context is application
        calls.append(request)
        return [*request.items, reference(request.context["reference"])]

    peer = ContextChildPeer(("default",))
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    async with (
        timeout(30),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        peer.url = str(server.make_url("/v1"))
        agent = replace(agent_for(peer, executor), transform_context=transform)
        async with agent.session(context=application, environment=declaration(executor)) as session:
            root = await session.submit("Delegate the question.")
            assert (await root.get_result()).final_output == "root complete"
            (child,) = await handles(session, 1)
            assert (await child.get_result()).final_output == "child complete"
            child_requests = [request for request in calls if request.subagent_id is not None]
            assert len(child_requests) == 1
            assert child_requests[0].session_id == session.id
            assert child_requests[0].turn_id == child.turn_id
            assert child_requests[0].subagent_id == child.child_id
    assert len(calls) == len(peer.requests) >= 3
    assert all("shared reference" in json.dumps(request) for request in peer.requests)
