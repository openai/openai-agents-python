"""Local peer and Runner observations with synthetic responses."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from dataclasses import FrozenInstanceError

import httpx2
import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import APIStatusError, AsyncOpenAI

from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrail,
    InputGuardrailTripwireTriggered,
    ModelRetrySettings,
    ModelSettings,
    RequestCapture,
    RunConfig,
    RunHooks,
    Runner,
    UserError,
    function_tool,
    output_guardrail,
)
from agents.memory import OpenAIResponsesCompactionSession
from agents.models import openai_provider
from agents.models.openai_provider import OpenAIProvider
from agents.request_capture import TransportCounts
from agents.testing import ScriptedModel
from agents.tracing.span_data import ResponseSpanData
from tests.memory.test_openai_responses_compaction_session import SimpleListSession
from tests.model_test_helpers import get_response_obj
from tests.models.test_codex_provider import FakeThread, config, threads as threads
from tests.test_responses import get_function_tool_call, get_handoff_tool_call, get_text_message
from tests.testing_processor import fetch_ordered_spans

pytestmark = [pytest.mark.asyncio, pytest.mark.allow_call_model_methods]


class Peer:
    def __init__(self):
        self.statuses = []
        self.compact_user_agents = []
        self.requests = []
        self.received = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.payload = get_response_obj([get_text_message("done")]).model_dump(mode="json")

    async def handle(self, request):
        if request.method == "GET":
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            async for _ in socket:
                self.requests.append("frame")
                if self.statuses:
                    await socket.send_json({"type": "error", "error": {"message": "failed"}})
                    continue
                await socket.send_json(
                    {"type": "response.created", "sequence_number": 0, "response": self.payload}
                )
                await socket.send_json(
                    {"type": "response.completed", "sequence_number": 1, "response": self.payload}
                )
            return socket

        body = await request.json()
        self.requests.append(request.path)
        self.received.set()
        await self.release.wait()
        status = self.statuses.pop(0) if self.statuses else 200
        if status == "disconnect":
            request.transport.close()
            return web.Response()
        if status == 307:
            raise web.HTTPTemporaryRedirect("/redirected")
        if status != 200:
            return web.json_response(
                {"error": {"message": "unavailable"}},
                status=status,
                headers={"retry-after-ms": "1"},
            )
        if request.path.endswith("/compact"):
            self.compact_user_agents.append(request.headers["User-Agent"])
            return web.json_response(
                {"output": [{"type": "compaction", "encrypted_content": "fixture"}]}
            )
        if request.path.endswith("/chat/completions"):
            return web.json_response(
                {
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "done"},
                            "finish_reason": "stop",
                        }
                    ]
                }
            )
        if body.get("stream"):
            event = {"type": "response.completed", "response": self.payload, "sequence_number": 0}
            return web.Response(
                text=f"data: {json.dumps(event)}\n\n", content_type="text/event-stream"
            )
        return web.json_response(self.payload)


@pytest_asyncio.fixture(loop_scope="function")
async def peer(monkeypatch):
    peer = Peer()
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", peer.handle)
    monkeypatch.setattr(openai_provider, "_http_client", None)
    async with TestServer(app) as server:
        peer.url = str(server.make_url("/"))
        peer.provider = OpenAIProvider(base_url=peer.url, api_key="fixture")
        try:
            yield peer
        finally:
            peer.release.set()
            await peer.provider.aclose()
            await openai_provider.shared_http_client().aclose()


async def run(agent, stream=False, **kwargs):
    if not stream:
        return await Runner.run(agent, "private input", **kwargs)
    result = Runner.run_streamed(agent, "private input", **kwargs)
    async for _ in result.stream_events():
        pass
    return result


@pytest.mark.parametrize("stream", [False, True])
async def test_client_and_runner_retries_and_redirect(peer, stream):
    client = peer.provider._get_client()
    client.max_retries = 1
    peer.statuses = [503, 503, 307]
    model = peer.provider.get_model("fixture")
    settings = ModelSettings(
        timeout=5,
        retry=ModelRetrySettings(
            max_retries=1, backoff={"initial_delay": 0}, policy=lambda _: True
        ),
    )
    with RequestCapture() as capture:
        result = await run(
            Agent(name="root", model=model, model_settings=settings),
            stream,
            run_config=RunConfig(tracing={"operation_detail": "metadata"}),
        )
    assert result.final_output == "done"
    (span,) = [
        span.span_data
        for span in fetch_ordered_spans()
        if isinstance(span.span_data, ResponseSpanData)
    ]
    assert len(span.operation.retry_timestamps) == 2
    assert len(peer.requests) == 4
    totals = capture.totals
    (row,) = totals.routes
    assert totals.coverage == row.coverage == "complete"
    assert totals.model_submissions == row.submissions == 1
    assert row.observed == TransportCounts(4, 4, 4)
    assert "private" not in repr(totals) and peer.url not in repr(totals)
    with pytest.raises(FrozenInstanceError):
        row.submissions = 0


@pytest.mark.parametrize("fresh", [None, "first", "last"])
async def test_challenge_reuses_or_replaces_request(peer, fresh):
    class Challenge(httpx2.Auth):
        def auth_flow(self, request):
            other = httpx2.Request(
                request.method, request.url, headers=request.headers, content=request.content
            )
            reply = yield other if fresh == "first" else request
            if reply.status_code == 401:
                yield other if fresh == "last" else request

    provider = OpenAIProvider(api_key="fixture", base_url=peer.url, use_responses=False)
    openai_provider.shared_http_client().auth = Challenge()
    peer.statuses = [401]
    with RequestCapture() as capture:
        await run(Agent(name="root", model=provider.get_model("fixture")))
    assert len(peer.requests) == 2
    (row,) = capture.totals.routes
    assert row.submissions == 1
    assert row.coverage == ("complete" if fresh is None else "partial")
    n = 2 if fresh is None else 1
    assert row.observed == TransportCounts(n, n, n)


@pytest.mark.parametrize("stream", [False, True])
async def test_redirect_cannot_hide_an_unmarked_challenge(peer, stream):
    class Challenge(httpx2.Auth):
        def auth_flow(self, request):
            yield request
            yield httpx2.Request(request.method, request.url, content=request.content)
            yield request

    agent = Agent(name="root", model=peer.provider.get_model("fixture"))
    openai_provider.shared_http_client().auth = Challenge()
    peer.statuses = [307, 401, 401]
    with RequestCapture() as capture:
        result = await run(agent, stream)
    assert result.final_output == "done"
    assert len(peer.requests) == 4
    (row,) = capture.totals.routes
    assert row.submissions == 1
    assert row.coverage == capture.totals.coverage == "partial"
    assert row.observed == TransportCounts(3, 3, 3)


async def test_unmarked_challenge_before_cancellation_keeps_gap(peer):
    class Challenge(httpx2.Auth):
        def auth_flow(self, request):
            yield httpx2.Request(request.method, request.url, content=request.content)
            yield request

    async def cancel_second(request):
        if peer.requests:
            raise asyncio.CancelledError()

    agent = Agent(name="root", model=peer.provider.get_model("fixture"))
    pool = openai_provider.shared_http_client()
    pool.auth = Challenge()
    pool.event_hooks["request"].append(cancel_second)
    peer.statuses = [401]
    with pytest.raises(asyncio.CancelledError), RequestCapture() as capture:
        await run(agent)
    assert len(peer.requests) == 1
    assert capture.totals.coverage == "unknown" and capture.totals.observed is None


async def test_websocket_reply_once_per_request(peer):
    provider = OpenAIProvider(api_key="fixture", base_url=peer.url, use_responses_websocket=True)
    model = provider.get_model("fixture")
    capture = RequestCapture()
    try:
        for stream in [False, True]:
            with capture:
                result = await run(Agent(name="root", model=model), stream)
            assert result.final_output == "done"
            (row,) = capture.totals.routes
            assert row.route == "websocket"
            assert row.observed == TransportCounts(1, 1, 1)
        assert peer.requests == ["frame", "frame"]
    finally:
        await provider.aclose()


@pytest.mark.parametrize("before", [False, True])
async def test_cancellation_before_and_after_header_write(peer, before):
    model = peer.provider.get_model("fixture")
    peer.release.clear()

    async def hold(request):
        peer.received.set()
        await peer.release.wait()

    if before:
        openai_provider.shared_http_client().event_hooks["request"].append(hold)
    capture = RequestCapture()
    with pytest.raises(asyncio.CancelledError), capture:
        task = asyncio.create_task(run(Agent(name="root", model=model)))
        await asyncio.wait_for(peer.received.wait(), 5)
        task.cancel()
        await task
    n = 0 if before else 1
    assert len(peer.requests) == n
    assert capture.totals.routes[0].observed == TransportCounts(n, n, 0)


@pytest.mark.parametrize("enabled", [False, True])
async def test_borrowed_http_and_automatic_compaction(peer, monkeypatch, enabled):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture")
    monkeypatch.setenv("OPENAI_BASE_URL", peer.url)
    session = OpenAIResponsesCompactionSession(
        "test", SimpleListSession(), should_trigger_compaction=lambda _: True
    )
    peer.statuses = [200, 503]
    async with AsyncOpenAI(api_key="fixture", base_url=peer.url) as borrowed:
        model = OpenAIProvider(openai_client=borrowed).get_model("fixture")
        try:
            capture = RequestCapture()
            with capture if enabled else nullcontext():
                await run(Agent(name="root", model=model), session=session)
            assert peer.compact_user_agents == [borrowed.user_agent]
        finally:
            await session.client.close()
    totals = capture.totals
    assert len(peer.requests) == 3
    if not enabled:
        assert not totals.routes
        return
    assert totals.coverage == "partial" and totals.compactions == 1
    compact, model_row = totals.routes
    assert compact.observed == TransportCounts(2, 2, 2)
    assert (model_row.route, model_row.coverage, model_row.observed) == (
        "borrowed_http",
        "unknown",
        None,
    )


@pytest.mark.parametrize("stream", [False, True])
async def test_attached_children_only_and_correction(peer, stream):
    child = Agent(name="child", model=peer.provider.get_model("fixture"))

    @function_tool
    async def independent():
        return (await run(child, stream)).final_output

    tool = child.as_tool(
        tool_name="ask", tool_description="Ask", on_stream=(lambda _: None) if stream else None
    )
    model = ScriptedModel(
        [
            [get_function_tool_call("missing", "{}", "a")],
            [get_function_tool_call("ask", '{"input":"check"}')],
            [get_function_tool_call("independent", "{}", "c")],
            [get_text_message("done")],
        ]
    )
    config = RunConfig(tool_not_found_behavior="return_error_to_model")
    with RequestCapture() as capture:
        await run(
            Agent(name="root", model=model, tools=[tool, independent]), stream, run_config=config
        )
    assert len(peer.requests) == 2
    attached, root = capture.totals.routes
    assert (root.owner, root.submissions, root.observed) == ("root", 4, None)
    assert (attached.owner, attached.submissions, attached.observed) == (
        "child",
        1,
        TransportCounts(1, 1, 1),
    )
    assert capture.totals.coverage == "partial"


async def test_redirect_chains_existing_trace_once_and_disabled_capture_is_inert(peer):
    agent = Agent(name="root", model=peer.provider.get_model("fixture"))
    writes = 0

    async def original(name, info):
        nonlocal writes
        if name == "http11.send_request_headers.complete":
            writes += 1

    async def install(request):
        request.extensions.setdefault("trace", original)

    pool = openai_provider.shared_http_client()
    pool.event_hooks["request"].insert(0, install)
    peer.statuses = [307]
    with RequestCapture() as capture:
        await run(agent)
    assert writes == len(peer.requests) == 2
    assert capture.totals.routes[0].observed == TransportCounts(2, 2, 2)

    async def verify(request):
        assert request.extensions["trace"] is original

    pool.event_hooks["request"].append(verify)
    await run(agent)
    assert writes == 3
    assert capture.totals.routes[0].observed == TransportCounts(2, 2, 2)


async def test_missing_transport_evidence_is_unknown(peer):
    async def remove_trace(request):
        request.extensions.pop("trace", None)

    model = peer.provider.get_model("fixture")
    openai_provider.shared_http_client().event_hooks["request"].append(remove_trace)
    with RequestCapture() as capture:
        await run(Agent(name="root", model=model))
    (row,) = capture.totals.routes
    assert len(peer.requests) == 1
    assert row.coverage == "unknown" and row.observed is None


async def test_concurrent_and_unfinished_stream_capture(peer):
    agent = Agent(name="root", model=peer.provider.get_model("fixture"))
    peer.release.clear()
    capture = RequestCapture()
    with capture:
        result = Runner.run_streamed(agent, "start")
        await asyncio.wait_for(peer.received.wait(), 5)
        with pytest.raises(RuntimeError, match="active"), capture:
            pass
    old = capture.totals
    assert old.coverage == "partial"
    peer.release.set()

    async def measured(capture, stream):
        with capture:
            await run(agent, stream)
        (row,) = capture.totals.routes
        assert row.submissions == 1
        assert row.coverage == "complete" and row.observed == TransportCounts(1, 1, 1)

    await asyncio.gather(measured(capture, False), measured(RequestCapture(), True))
    async for _ in result.stream_events():
        pass
    assert old.routes[0].observed == TransportCounts(1, 1, 0)
    assert len(peer.requests) == 3


async def test_native_transport_unavailable(tmp_path, threads):
    threads[0].append(FakeThread())
    with RequestCapture() as capture:
        await run(Agent(name="root"), run_config=config(tmp_path))
    (row,) = capture.totals.routes
    assert (row.route, row.submissions, row.coverage, row.observed) == (
        "native",
        1,
        "unknown",
        None,
    )


async def test_unstarted_websocket_send_has_no_attempt(peer, monkeypatch):
    provider = OpenAIProvider(api_key="fixture", base_url=peer.url, use_responses_websocket=True)
    model = provider.get_model("fixture")
    wait = model._await_websocket_with_timeout

    async def cancel_before_send(awaitable, timeout_seconds, phase):
        if phase == "send":
            awaitable.close()
            raise asyncio.CancelledError
        return await wait(awaitable, timeout_seconds, phase)

    monkeypatch.setattr(model, "_await_websocket_with_timeout", cancel_before_send)
    try:
        with pytest.raises(asyncio.CancelledError), RequestCapture() as capture:
            await run(Agent(name="root", model=model))
        assert capture.totals.model_submissions == 1
        assert capture.totals.observed == TransportCounts(0, 0, 0)
        assert not peer.requests
    finally:
        await provider.aclose()


async def test_handoff_remains_in_root(peer):
    target = Agent(name="target", model=peer.provider.get_model("fixture"))
    model = ScriptedModel([[get_handoff_tool_call(target)]])
    with RequestCapture() as capture:
        await run(Agent(name="root", model=model, handoffs=[target]))
    totals = capture.totals
    assert totals.model_submissions == 2 and totals.coverage == "partial"
    assert all(row.owner == "root" for row in totals.routes)
    assert totals.observed == TransportCounts(1, 1, 1)


async def test_approval_resume_starts_fresh():
    @function_tool(needs_approval=True)
    def pause():
        return "yes"

    model = ScriptedModel([[get_function_tool_call("pause", "{}")], [get_text_message("ok")]])
    agent = Agent(name="root", model=model, tools=[pause])
    with RequestCapture() as capture:
        first = await run(agent)
    initial = capture.totals
    state = first.to_state()
    state.approve(first.interruptions[0])
    with capture:
        final = await Runner.run(agent, state)
    assert final.final_output == "ok"
    assert initial.model_submissions == capture.totals.model_submissions == 1
    assert capture.totals.observed is None


@pytest.mark.parametrize("websocket", [False, True])
async def test_failure_keeps_reply_evidence(peer, websocket):
    from agents.models.openai_responses import ResponsesWebSocketError

    peer.statuses = [403]
    provider = OpenAIProvider(
        api_key="fixture", base_url=peer.url, use_responses_websocket=websocket
    )
    capture = RequestCapture()
    try:
        error = ResponsesWebSocketError if websocket else APIStatusError
        with pytest.raises(error), capture:
            await run(Agent(name="root", model=provider.get_model("fixture")))
        assert len(peer.requests) == 1
        assert capture.totals.observed == TransportCounts(1, 1, 1)
        assert capture.totals.coverage == "complete"
    finally:
        await provider.aclose()


@pytest.mark.parametrize("retry", [False, True])
async def test_missing_trace_on_failed_hop(peer, retry):
    model = peer.provider.get_model("fixture")

    async def remove_first_trace(request):
        if not peer.requests:
            request.extensions.pop("trace", None)

    openai_provider.shared_http_client().event_hooks["request"].append(remove_first_trace)
    with RequestCapture() as capture:
        agent = Agent(name="root", model=model)
        if retry:
            peer.statuses = ["disconnect"]
            await run(agent)
        else:
            peer.release.clear()
            task = asyncio.create_task(run(agent))
            await asyncio.wait_for(peer.received.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert len(peer.requests) == (2 if retry else 1)
    assert capture.totals.model_submissions == 1
    assert capture.totals.coverage == ("partial" if retry else "unknown")
    assert capture.totals.observed == (TransportCounts(1, 1, 1) if retry else None)


async def test_hook_request_on_same_pool_is_excluded(peer):
    agent = Agent(name="root", model=peer.provider.get_model("fixture"))
    pool = openai_provider.shared_http_client()

    async def telemetry(request):
        if request.url.path == "/responses":
            await pool.post(peer.url + "telemetry", json={})

    pool.event_hooks["request"].insert(0, telemetry)
    with RequestCapture() as capture:
        await run(agent)
    assert peer.requests == ["/telemetry", "/responses"]
    assert capture.totals.observed == TransportCounts(1, 1, 1)
    assert capture.totals.coverage == "complete"


@pytest.mark.parametrize("stream", [False, True])
async def test_failed_setup_does_not_hide_next_invocation(peer, stream):
    agent = Agent(name="root", model=peer.provider.get_model("fixture"))
    with RequestCapture() as capture:
        with pytest.raises(UserError):
            await run(agent, stream, session=SimpleListSession(), conversation_id="conflict")
        await run(agent)
    assert peer.requests == ["/responses"]
    assert capture.totals.model_submissions == 1
    assert capture.totals.observed == TransportCounts(1, 1, 1)
    assert capture.totals.coverage == "complete"


async def test_immediate_stream_cancel_finishes_capture(peer):
    agent = Agent(name="root", model=peer.provider.get_model("fixture"))
    with RequestCapture() as capture:
        result = Runner.run_streamed(agent, "start")
        result.cancel()
        async for _ in result.stream_events():
            pass
    assert not peer.requests
    assert capture.totals.routes == ()
    assert capture.totals.coverage == "complete"


async def test_failed_sync_setup_does_not_hide_next_invocation(peer):
    agent = Agent(name="root", model=peer.provider.get_model("fixture"))
    with RequestCapture() as capture:
        with pytest.raises(RuntimeError, match="already running"):
            Runner.run_sync(agent, "start")
        await run(agent)
    assert peer.requests == ["/responses"]
    assert capture.totals.model_submissions == 1
    assert capture.totals.observed == TransportCounts(1, 1, 1)
    assert capture.totals.coverage == "complete"


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("timeout", [None, 5])
async def test_stream_guardrail_during_preparation_skips_submission(peer, enabled, timeout):
    release = asyncio.Event()

    async def deny(ctx, agent, input):
        await asyncio.wait_for(release.wait(), timeout=5)
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    class Hooks(RunHooks):
        async def on_llm_start(self, *args):
            release.set()
            assert result._input_guardrails_task is not None
            await result._input_guardrails_task

    agent = Agent(
        name="root",
        model=peer.provider.get_model("fixture"),
        model_settings=ModelSettings(timeout=timeout),
        input_guardrails=[InputGuardrail(guardrail_function=deny)],
    )
    capture = RequestCapture()
    with capture if enabled else nullcontext():
        result = Runner.run_streamed(agent, "input", hooks=Hooks())
        with pytest.raises(InputGuardrailTripwireTriggered):
            async for _ in result.stream_events():
                pass
    assert release.is_set()
    assert not peer.requests
    assert capture.totals.model_submissions == 0
    if enabled:
        assert capture.totals.observed == TransportCounts(0, 0, 0)


async def test_direct_calls_share_capture_and_nested_capture_is_isolated(peer):
    agent = Agent(name="root", model=peer.provider.get_model("fixture"))
    with RequestCapture() as outer:
        await run(agent)
        with RequestCapture() as inner:
            await run(agent, True)
        await asyncio.gather(run(agent), run(agent, True))
    assert len(peer.requests) == 4
    for capture, count in ((outer, 3), (inner, 1)):
        assert capture.totals.model_submissions == count
        assert capture.totals.observed == TransportCounts(count, count, count)
        assert capture.totals.coverage == "complete"


async def test_unfinished_run_stays_partial_when_another_finishes(peer):
    agent = Agent(name="root", model=peer.provider.get_model("fixture"))
    peer.release.clear()
    with RequestCapture() as capture:
        pending = Runner.run_streamed(agent, "start")
        await asyncio.wait_for(peer.received.wait(), 5)
        with pytest.raises(UserError):
            await run(agent, session=SimpleListSession(), conversation_id="conflict")
    peer.release.set()
    async for _ in pending.stream_events():
        pass
    assert capture.totals.model_submissions == 1
    assert capture.totals.coverage == "partial"
    assert capture.totals.observed == TransportCounts(1, 1, 0)


async def test_direct_calls_retain_transport_gaps(peer):
    async def remove_trace(request):
        request.extensions.pop("trace", None)

    agent = Agent(name="root", model=peer.provider.get_model("fixture"))
    hooks = openai_provider.shared_http_client().event_hooks["request"]
    with RequestCapture() as capture:
        hooks.append(remove_trace)
        await run(agent)
        hooks.remove(remove_trace)
        await run(agent)
    assert len(peer.requests) == capture.totals.model_submissions == 2
    assert capture.totals.observed == TransportCounts(1, 1, 1)
    assert capture.totals.coverage == "partial"


async def test_sync_direct_runs():
    def invoke():
        agent = Agent(name="root", model=ScriptedModel([[get_text_message("done")]] * 2))
        with RequestCapture() as capture:
            Runner.run_sync(agent, "one")
            Runner.run_sync(agent, "two")
        assert capture.totals.model_submissions == 2
        assert capture.totals.coverage == "unknown"

    await asyncio.to_thread(invoke)


async def test_unfinished_opaque_run_excludes_independent_guardrail_calls():
    entered, release = asyncio.Event(), asyncio.Event()
    judge = Agent(name="judge", model=ScriptedModel([[get_text_message("ok")]]))

    @output_guardrail
    async def check(ctx, agent, output):
        await run(judge)
        entered.set()
        await release.wait()
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    agent = Agent(
        name="root", model=ScriptedModel([[get_text_message("done")]]), output_guardrails=[check]
    )
    with RequestCapture() as capture:
        task = asyncio.create_task(run(agent))
        try:
            await asyncio.wait_for(entered.wait(), 5)
        finally:
            release.set()
    await task
    assert capture.totals.coverage == "partial"
    assert capture.totals.model_submissions == 1
    assert capture.totals.observed is None
