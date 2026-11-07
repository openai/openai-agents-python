"""Exercise explicit buffered Responses through real HTTP and the ordinary Core loop."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import BaseModel

from agents import (
    Agent,
    HttpMCP,
    NativeError,
    NativeProvider,
    OutputValidationError,
    TurnStoppedError,
    function_tool,
)
from agents.observation import (
    ModelRequestDispatching,
    ModelResponseLinked,
    OutputTextDelta,
    ToolFinished,
    ToolStarted,
    TurnFinished,
    TurnStarted,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, tool_output
from tests.test_native_agent_observation import collect, text_response
from tests.test_native_mcp_tools import Peer as MCPPeer, mcp_call
from tests.test_sdk_session_transport import HTTPPeer, disconnected


@dataclass
class JSONPeer(HTTPPeer):
    json_replies: deque[dict[str, Any] | bytes] = field(default_factory=deque)
    response_headers: dict[str, str] = field(default_factory=dict)
    body_gate: asyncio.Event | None = None
    body_entered: asyncio.Event = field(default_factory=asyncio.Event)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        if body.get("stream") is not False:
            return await super().handle(request)
        transport = request.transport
        assert transport is not None
        self.transports.append(transport)
        self.requests.append(body)
        self.request_headers.append({key.lower(): value for key, value in request.headers.items()})
        assert body["model"] == self.expected_model
        assert self.json_replies, "Unexpected buffered model request"
        value = self.json_replies.popleft()
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        response = web.StreamResponse(
            headers={"Content-Type": "application/json", **self.response_headers}
        )
        await response.prepare(request)
        gate = self.body_gate
        try:
            if gate is not None:
                cut = len(data) // 2
                await response.write(data[:cut])
                self.body_entered.set()
                await gate.wait()
                await response.write(data[cut:])
            else:
                await response.write(data)
            await response.write_eof()
        except ConnectionResetError:
            self.disconnected_bodies += 1
        else:
            self.finished_bodies += 1
        return response


@asynccontextmanager
async def json_peer() -> AsyncIterator[JSONPeer]:
    peer = JSONPeer()
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    async with TestServer(app) as server:
        peer.url = str(server.make_url("/v1"))
        yield peer


def provider(
    peer: JSONPeer, mode: Literal["streaming", "buffered"] | None = "buffered"
) -> NativeProvider:
    if mode is None:
        return NativeProvider(base_url=peer.url, api_key="synthetic-json-key")
    return NativeProvider(base_url=peer.url, api_key="synthetic-json-key", response_mode=mode)


def json_response(text: str, response_id: str, item_id: str) -> dict[str, Any]:
    item = assistant_output(text)["item"]
    item["id"] = item_id
    return {
        "id": response_id,
        "object": "response",
        "status": "completed",
        "output": [item],
        "usage": {
            "input_tokens": 11,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 3,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 14,
        },
    }


def assistant_texts(items: list[dict[str, Any]]) -> list[str]:
    return [
        part["text"]
        for item in items
        if item["type"] == "message" and item["role"] == "assistant"
        for part in item["content"]
        if part["type"] == "output_text"
    ]


def assert_dispatches(events: list[Any], transport: str, count: int) -> None:
    dispatches = [event for event in events if isinstance(event, ModelRequestDispatching)]
    assert len(dispatches) == count
    assert all(event.transport == transport for event in dispatches)


async def test_buffered_lite_model_fails_before_http_dispatch() -> None:
    async with timeout(30), json_peer() as peer, provider(peer) as owner:
        # Pin the bundled Lite model rather than a configurable/catalog default.
        agent = Agent(model="gpt-6.1-sol", instructions="Answer.", provider=owner)
        with pytest.raises(
            NativeError,
            match="Buffered Responses requires HTTP without Responses Lite or WebSockets",
        ):
            await agent.run("unsupported Lite request")
        assert peer.requests == []
        assert peer.transports == []


async def test_buffered_session_retains_output_usage_ids_and_history() -> None:
    text = "First unique café🐈 answer."
    async with timeout(30), json_peer() as peer:
        peer.json_replies.extend(
            [
                json_response(text, "json-response-first", "json-item-first"),
                json_response("follow-up", "json-response-next", "json-item-next"),
                json_response("restored answer", "restored-response", "restored-item"),
            ]
        )
        async with provider(peer) as owner:
            agent = Agent(model="gpt-5.2", instructions="Answer.", provider=owner)
            async with agent.session() as session:
                turn = await session.submit("first input")
                result = await turn.get_result()
                assert result.final_output == text
                assert result.thread_usage is not None
                assert result.thread_usage.total_token_usage.total_tokens == 14
                events = await collect(turn.events())
                assert events == await collect(turn.events())
                assert isinstance(events[0], TurnStarted)
                assert isinstance(events[-1], TurnFinished)
                assert events[-1].status == "completed"
                deltas = [event for event in events if isinstance(event, OutputTextDelta)]
                assert "".join(event.delta for event in deltas) == text
                assert {event.item_id for event in deltas} == {"json-item-first"}
                assert all(event.turn_id == turn.turn_id for event in deltas)
                assert all(event.submission_id == turn.submission_id for event in deltas)
                assert all(
                    a.sequence < b.sequence for a, b in zip(events, events[1:], strict=False)
                )
                assert_dispatches(events, "http_json", 1)
                [linked] = [event for event in events if isinstance(event, ModelResponseLinked)]
                [dispatch] = [
                    event for event in events if isinstance(event, ModelRequestDispatching)
                ]
                assert linked.request_id == dispatch.request_id
                items = [item async for item in session.items()]
                assert assistant_texts(items) == [text]
                assert any(item["id"] == "json-item-first" for item in items)
                recorded = [entry async for entry in session.turns()]
                assert recorded[0]["id"] == turn.turn_id
                assert recorded[0]["status"] == "completed"
                [response] = recorded[0]["responses"]
                assert response["response_id"] == "json-response-first"
                assert response["id"] == linked.response_observation_id
                assert response["usage"]["input_tokens_details"] == {"cached_tokens": 0}
                assert response["usage"]["output_tokens_details"] == {"reasoning_tokens": 0}
                state = await session.save_state()
                following = await session.submit("next input")
                continued = await following.get_result()
                assert continued.final_output == "follow-up"
                assert continued.thread_usage is not None
                assert continued.thread_usage.total_token_usage.total_tokens == 28
                assert text in json.dumps(peer.requests[1]["input"], ensure_ascii=False)
            async with agent.session(state=state) as restored:
                assert [entry async for entry in restored.turns()] == recorded
                resumed = await restored.submit("restore input")
                assert (await resumed.get_result()).final_output == "restored answer"
                assert_dispatches(await collect(resumed.events()), "http_json", 1)
                replay = json.dumps(peer.requests[2]["input"], ensure_ascii=False)
                assert text in replay
                assert "follow-up" not in replay
                assert "next input" not in replay
            assert len(peer.requests) == 3
            assert all(request["stream"] is False for request in peer.requests)
        await disconnected(peer.transports)


async def test_buffered_runs_keep_fresh_histories_and_provider_cleanup() -> None:
    async with timeout(30), json_peer() as peer:
        async with provider(peer) as owner:
            agent = Agent(model="gpt-5.2", instructions="Answer.", provider=owner)
            sessions = []
            for index in range(2):
                answer = f"answer-{index}-界🐈"
                peer.json_replies.append(
                    json_response(answer, f"response-{index}", f"item-{index}")
                )
                result = await agent.run(f"independent-input-{index}", return_state=True)
                assert result.final_output == answer
                assert result.thread_usage is not None
                assert result.thread_usage.total_token_usage.total_tokens == 14
                assert result.state is not None
                assert answer in json.dumps(result.state.history, ensure_ascii=False)
                events = await collect(result.turn.events())
                assert_dispatches(events, "http_json", 1)
                assert (
                    "".join(event.delta for event in events if isinstance(event, OutputTextDelta))
                    == answer
                )
                assert events == await collect(result.turn.events())
                sessions.append(result.turn.session_id)
                if index:
                    assert "independent-input-0" not in json.dumps(peer.requests[1]["input"])
                    assert "answer-0" not in json.dumps(peer.requests[1]["input"])
            assert len(set(sessions)) == 2
            assert len(peer.requests) == 2
            assert all(request["stream"] is False for request in peer.requests)
        await disconnected(peer.transports)


async def test_buffered_custom_tool_input_executes_once() -> None:
    seen: list[str] = []

    @function_tool
    async def capture(value: str) -> str:
        seen.append(value)
        return value

    code = 'text(await tools.capture({value: "exact-once"}));'
    response = json_response("", "code-response", "unused-item")
    response["output"] = [
        {"type": "custom_tool_call", "name": "exec", "call_id": "code-once", "input": code}
    ]
    async with timeout(30), json_peer() as peer:
        peer.json_replies.extend([response, json_response("done", "done-response", "done-item")])
        async with provider(peer) as owner:
            agent = Agent(
                model="gpt-5.2",
                instructions="Use capture through code.",
                tools=[capture],
                provider=owner,
                programmatic_tool_calling=True,
            )
            result = await agent.run("Capture the value once.")
            assert result.final_output == "done"
            assert seen == ["exact-once"]
            assert len(peer.requests) == 2
            calls = [
                item for item in peer.requests[1]["input"] if item["type"] == "custom_tool_call"
            ]
            assert [(call["call_id"], call["input"]) for call in calls] == [("code-once", code)]
            output = tool_output(peer.requests[1], "code-once", "custom_tool_call_output")
            assert output.count("exact-once") == 1
        await disconnected(peer.transports)


@pytest.mark.parametrize("content_length", [False, True], ids=["chunked", "declared"])
async def test_buffered_body_limit_rejects_oversize_without_retry(content_length: bool) -> None:
    limit = 64 * 1024 * 1024
    body = json.dumps(json_response("oversized", "large-response", "large-item")).encode()
    body += b" " * limit
    async with timeout(30), json_peer() as peer:
        peer.json_replies.append(body)
        if content_length:
            peer.response_headers["Content-Length"] = str(len(body))
        async with provider(peer) as owner:
            agent = Agent(model="gpt-5.2", instructions="Answer.", provider=owner)
            with pytest.raises(NativeError, match=f"response body exceeds the {limit} byte limit"):
                await agent.run("Reject the oversized response.")
            assert len(peer.requests) == 1
        await disconnected(peer.transports)


@pytest.mark.parametrize(
    "mode", [None, "streaming", "buffered"], ids=["default", "streaming", "buffered"]
)
async def test_response_mode_controls_progress_delivery(
    mode: Literal["streaming", "buffered"] | None,
) -> None:
    async with timeout(30), json_peer() as peer:
        gate = asyncio.Event()
        if mode == "buffered":
            peer.json_replies.append(json_response("first second", "gated-response", "gated-item"))
            peer.body_gate = gate
        else:
            peer.respond(*text_response("gated-item", "first ", "second"))
            peer.delta_gate = gate
        async with provider(peer, mode) as owner:
            agent = Agent(model="gpt-5.2", instructions="Answer.", provider=owner)
            async with agent.session() as session:
                turn = await session.submit("stream the answer")
                first: asyncio.Future[OutputTextDelta] = asyncio.get_running_loop().create_future()
                events: list[Any] = []

                async def observe() -> None:
                    async for event in turn.events():
                        events.append(event)
                        if isinstance(event, OutputTextDelta) and not first.done():
                            first.set_result(event)

                reader = asyncio.create_task(observe())
                result = asyncio.create_task(turn.get_result())
                try:
                    if mode == "buffered":
                        await peer.body_entered.wait()
                        with pytest.raises(asyncio.TimeoutError):
                            await asyncio.wait_for(asyncio.shield(first), 0.1)
                        assert not any(
                            isinstance(event, (OutputTextDelta, ModelResponseLinked))
                            for event in events
                        )
                        assert not result.done()
                    else:
                        assert (await asyncio.wait_for(asyncio.shield(first), 5)).delta == "first "
                        assert not result.done()
                    gate.set()
                    assert (await result).final_output == "first second"
                    await reader
                    assert events == await collect(turn.events())
                    assert (
                        "".join(
                            event.delta for event in events if isinstance(event, OutputTextDelta)
                        )
                        == "first second"
                    )
                    assert_dispatches(events, "http_json" if mode == "buffered" else "http_sse", 1)
                    assert peer.requests[0]["stream"] is (mode != "buffered")
                finally:
                    gate.set()
                    reader.cancel()
                    result.cancel()
                    await asyncio.gather(reader, result, return_exceptions=True)
        await disconnected(peer.transports)


async def test_buffered_function_tool_uses_core_continuation_once() -> None:
    effects: list[str] = []

    @function_tool
    async def lookup(token: str) -> str:
        """Look up the supplied token."""
        effects.append(token)
        return "resolved:" + token

    async with timeout(30), json_peer() as peer:
        first = json_response("unused", "tool-response", "unused-item")
        call = function_call("lookup", {"token": "exact-token"}, "json-call")["item"]
        call["id"] = "json-function-item"
        first["output"] = [call]
        peer.json_replies.extend(
            [first, json_response("tool done", "answer-response", "answer-item")]
        )
        async with provider(peer) as owner:
            agent = Agent(
                model="gpt-5.2",
                instructions="Use lookup then answer.",
                tools=[lookup],
                provider=owner,
            )
            async with agent.session() as session:
                turn = await session.submit("look up exact-token")
                result = await turn.get_result()
                assert result.final_output == "tool done"
                assert effects == ["exact-token"]
                assert len(peer.requests) == 2
                assert all(request["stream"] is False for request in peer.requests)
                assert tool_output(peer.requests[1], "json-call") == "resolved:exact-token"
                inputs = peer.requests[1]["input"]
                call_index = next(
                    i
                    for i, item in enumerate(inputs)
                    if item.get("type") == "function_call" and item.get("call_id") == "json-call"
                )
                output_index = next(
                    i
                    for i, item in enumerate(inputs)
                    if item.get("type") == "function_call_output"
                    and item.get("call_id") == "json-call"
                )
                assert call_index < output_index
                events = await collect(turn.events())
                [started] = [event for event in events if isinstance(event, ToolStarted)]
                [finished] = [event for event in events if isinstance(event, ToolFinished)]
                assert started.call_id == finished.call_id == "json-call"
                assert started.sequence < finished.sequence
                assert finished.status == "completed"
                assert_dispatches(events, "http_json", 2)
                recorded = [entry async for entry in session.turns()]
                assert {response["response_id"] for response in recorded[0]["responses"]} == {
                    "tool-response",
                    "answer-response",
                }
                assert "json-call" in json.dumps([item async for item in session.items()])
                assert result.thread_usage is not None
                assert result.thread_usage.total_token_usage.total_tokens == 28
        await disconnected(peer.transports)


@pytest.mark.parametrize("entry,count", [(b"{}", 400_000), (b"0", 1_000_010)])
async def test_buffered_structural_limit_rejects_dense_metadata_without_retry(
    entry: bytes, count: int
) -> None:
    async with timeout(30), json_peer() as peer:
        response = json.dumps(
            json_response("ordinary answer", "dense-response", "dense-item")
        ).encode()
        body = response[:-1] + b', "ignored": [' + (entry + b",") * (count - 1) + entry + b"]}"
        assert len(body) < 64 * 1024 * 1024
        peer.json_replies.append(body)
        async with provider(peer) as owner:
            agent = Agent(model="gpt-5.2", instructions="Answer.", provider=owner)
            with pytest.raises(NativeError, match="structural complexity limit"):
                await agent.run("Reject dense metadata")
            assert len(peer.requests) == 1
        await disconnected(peer.transports)


async def test_buffered_mcp_namespace_uses_core_continuation_once() -> None:
    mcp = MCPPeer(names=("search",), label="buffered-mcp-receipt")
    async with timeout(30), json_peer() as peer, mcp.serve() as url:
        peer.expected_model = "gpt-5.5"
        first = json_response("unused", "mcp-response", "unused-item")
        call = mcp_call("search", "mcp__docs", "json-mcp-call")["item"]
        call["id"] = "json-mcp-item"
        first["output"] = [call]
        peer.json_replies.extend(
            [
                first,
                json_response("MCP done", "mcp-answer-response", "mcp-answer-item"),
            ]
        )
        async with provider(peer) as owner:
            agent = Agent(
                model="gpt-5.5",
                instructions="Use docs search, then answer.",
                provider=owner,
                tools=[HttpMCP(name="docs", url=url, tools=["search"])],
            )
            async with agent.session() as session:
                turn = await session.submit("read key 43")
                result = await turn.get_result()
                assert result.final_output == "MCP done"
                assert len(mcp.calls) == 1
                assert mcp.calls[0]["name"] == "search"
                assert mcp.calls[0]["arguments"] == {"key": "43"}
                assert mcp.calls[0]["_meta"]["callId"] == "json-mcp-call"
                assert len(peer.requests) == 2
                assert all(request["stream"] is False for request in peer.requests)
                inputs = peer.requests[1]["input"]
                [output] = [item for item in inputs if item.get("type") == "function_call_output"]
                assert output["call_id"] == "json-mcp-call"
                assert mcp.label in str(output["output"])
                items = [item async for item in session.items()]
                [recorded_call] = [item for item in items if item["type"] == "function_call"]
                assert recorded_call["call_id"] == "json-mcp-call"
                assert recorded_call["name"] == "mcp__docs.search"
                [recorded_output] = [
                    item for item in items if item["type"] == "function_call_output"
                ]
                assert recorded_output["call_id"] == "json-mcp-call"
                assert mcp.label in str(recorded_output["output"])
                events = await collect(turn.events())
                assert_dispatches(events, "http_json", 2)
                [started] = [event for event in events if isinstance(event, ToolStarted)]
                assert started.family == "mcp"
                assert started.call_id == "json-mcp-call"
                assert result.thread_usage is not None
                assert result.thread_usage.total_token_usage.total_tokens == 28
        await disconnected(peer.transports)


class Answer(BaseModel):
    value: int


@pytest.mark.parametrize("mode", ["streaming", "buffered"])
@pytest.mark.parametrize("valid", [True, False], ids=["valid", "invalid"])
async def test_buffered_native_schema_preserves_validation(
    mode: Literal["streaming", "buffered"], valid: bool
) -> None:
    raw = '{"value":7}' if valid else '{"value":"wrong"}'
    async with timeout(30), json_peer() as peer:
        if mode == "buffered":
            peer.json_replies.append(json_response(raw, "schema-response", "schema-item"))
        else:
            peer.respond(*text_response("schema-item", raw))
        async with provider(peer, mode) as owner:
            agent = Agent(
                model="gpt-5.2", instructions="Return a value.", output_type=Answer, provider=owner
            )
            async with agent.session() as session:
                turn = await session.submit("return the value")
                if valid:
                    assert (await turn.get_result()).final_output == Answer(value=7)
                    assert_dispatches(
                        await collect(turn.events()),
                        "http_json" if mode == "buffered" else "http_sse",
                        1,
                    )
                else:
                    with pytest.raises(OutputValidationError) as failure:
                        await turn.get_result()
                    assert failure.value.raw_output == raw
                assert assistant_texts([item async for item in session.items()]) == [raw]
                output_format = peer.requests[0]["text"]["format"]
                assert output_format["type"] == "json_schema"
                assert output_format["strict"] is True
                assert output_format["schema"]["required"] == ["value"]
                assert peer.requests[0]["stream"] is (mode == "streaming")
        await disconnected(peer.transports)


@pytest.mark.parametrize("status", ["failed", "incomplete", "malformed-output"])
async def test_buffered_terminal_rejection_keeps_facts_without_effects(
    status: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agents import _native

    create_engine = _native.create_engine

    def no_stream_retries(config_json: str, *args: Any, **kwargs: Any) -> Any:
        config = json.loads(config_json)
        config["model_providers.native_agent"]["stream_max_retries"] = 0
        return create_engine(json.dumps(config), *args, **kwargs)

    # Select the existing retry policy, as the terminal-history tests do; the
    # real engine, HTTP request, parser, observation and tool authority still run.
    monkeypatch.setattr(_native, "create_engine", no_stream_retries)
    effects: list[str] = []

    @function_tool
    async def unexpected() -> str:
        """Record an effect that a rejected response must never admit."""
        effects.append("executed")
        return "unexpected"

    async with timeout(30), json_peer() as peer:
        response = json_response("rejected-output", "rejected-response", "rejected-item")
        if status == "malformed-output":
            # Missing content makes this a codec/shape failure. Predecoding the
            # output vector must fail before admitting even the valid first call.
            # This does not claim transactional SDK semantic-policy validation.
            response["output"] = [
                function_call("unexpected", {}, "never-execute")["item"],
                {"type": "message", "id": "malformed-item", "role": "assistant"},
            ]
        else:
            response["status"] = status
            response["output"] = [function_call("unexpected", {}, "never-execute")["item"]]
            if status == "failed":
                response["error"] = {
                    "type": "invalid_request_error",
                    "code": "invalid_prompt",
                    "message": "private provider detail",
                }
            else:
                response["incomplete_details"] = {"reason": "max_output_tokens"}
        peer.json_replies.append(response)
        async with provider(peer) as owner:
            agent = Agent(
                model="gpt-5.2", instructions="Answer.", tools=[unexpected], provider=owner
            )
            async with agent.session() as session:
                turn = await session.submit("reject the response")
                with pytest.raises(NativeError):
                    await turn.get_result()
                assert effects == []
                assert len(peer.requests) == 1
                recorded = [entry async for entry in session.turns()]
                [fact] = recorded[0]["responses"]
                assert fact["response_id"] == "rejected-response"
                assert fact["status"] == ("completed" if status == "malformed-output" else status)
                assert fact["usage"]["input_tokens"] == 11
                assert "private provider detail" not in repr(fact)
                history = json.dumps([item async for item in session.items()])
                assert "never-execute" not in history
                assert "rejected-output" not in history
        await disconnected(peer.transports)


@pytest.mark.parametrize("operation", ["turn", "owned-run"])
async def test_cancel_buffered_body_joins_and_provider_remains_usable(operation: str) -> None:
    async with timeout(30), json_peer() as peer:
        gate = asyncio.Event()
        peer.body_gate = gate
        peer.json_replies.append(
            json_response("unsettled-body", "unsettled-response", "unsettled-item")
        )
        async with provider(peer) as owner:
            agent = Agent(model="gpt-5.2", instructions="Answer.", provider=owner)
            try:
                if operation == "owned-run":
                    running = asyncio.create_task(agent.run("cancel this body"))
                    try:
                        await peer.body_entered.wait()
                        running.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await running
                        await disconnected(peer.transports[:1])
                    finally:
                        gate.set()
                        running.cancel()
                        await asyncio.gather(running, return_exceptions=True)
                    peer.body_gate = None
                    peer.json_replies.append(
                        json_response("recovered", "recovered-response", "recovered-item")
                    )
                    assert (await agent.run("independent recovery")).final_output == "recovered"
                else:
                    async with agent.session() as session:
                        turn = await session.submit("cancel this body")
                        await peer.body_entered.wait()
                        await turn.cancel()
                        with pytest.raises(TurnStoppedError):
                            await turn.get_result()
                        await disconnected(peer.transports[:1])
                        [recorded] = [entry async for entry in session.turns()]
                        assert recorded["status"] == "cancelled"
                        assert "responses" not in recorded
                        assert "unsettled-body" not in json.dumps(
                            [item async for item in session.items()]
                        )
                        gate.set()
                        peer.body_gate = None
                        peer.json_replies.append(
                            json_response("recovered", "recovered-response", "recovered-item")
                        )
                        recovered = await (await session.submit("recover")).get_result()
                        assert recovered.final_output == "recovered"
                        assert recovered.thread_usage is not None
                        assert recovered.thread_usage.total_token_usage.total_tokens == 14
                assert len(peer.requests) == 2
                assert all(request["stream"] is False for request in peer.requests)
            finally:
                gate.set()
        await disconnected(peer.transports)


async def test_buffered_body_error_uses_existing_stream_retry_policy() -> None:
    async with timeout(30), json_peer() as peer:
        peer.json_replies.extend(
            [
                b'{"id":"unparsed-response","status":"completed","output":[',
                json_response("retry succeeded", "retry-response", "retry-item"),
            ]
        )
        async with provider(peer) as owner:
            agent = Agent(model="gpt-5.2", instructions="Answer.", provider=owner)
            async with agent.session() as session:
                turn = await session.submit("retry a truncated body")
                result = await turn.get_result()
                assert result.final_output == "retry succeeded"
                assert len(peer.requests) == 2
                assert all(request["stream"] is False for request in peer.requests)
                assert result.thread_usage is not None
                assert result.thread_usage.total_token_usage.total_tokens == 14
                [recorded] = [entry async for entry in session.turns()]
                assert [fact["response_id"] for fact in recorded["responses"]] == ["retry-response"]
                assert assistant_texts([item async for item in session.items()]) == [
                    "retry succeeded"
                ]
        await disconnected(peer.transports)


@pytest.mark.parametrize(
    "saved_mode,current_mode",
    [("buffered", "streaming"), ("streaming", "buffered")],
)
async def test_restore_uses_current_provider_response_mode(
    saved_mode: Literal["streaming", "buffered"],
    current_mode: Literal["streaming", "buffered"],
) -> None:
    text = "saved answer café🐈"
    async with timeout(30), json_peer() as peer:
        if saved_mode == "buffered":
            peer.json_replies.append(json_response(text, "saved-response", "saved-item"))
        else:
            peer.respond(*text_response("saved-item", text))
        async with provider(peer, saved_mode) as original:
            agent = Agent(model="gpt-5.2", instructions="Answer.", provider=original)
            async with agent.session() as session:
                turn = await session.submit("original input")
                assert (await turn.get_result()).final_output == text
                assert peer.requests[0]["stream"] is (saved_mode == "streaming")
                saved = await session.save_state()
                recorded = [entry async for entry in session.turns()]
        await disconnected(peer.transports)

        # Serve either route so a wrong mode fails the wire/observation assertion,
        # rather than becoming an unrelated unexpected-request/retry failure.
        peer.respond(*text_response("restored-item", "restored answer"))
        peer.json_replies.append(
            json_response("restored answer", "restored-response", "restored-item")
        )
        async with provider(peer, current_mode) as current:
            # Override the Agent's original Provider as well as the saved state.
            # The Provider actually selected for this opening controls the mode.
            async with agent.session(state=saved, provider=current) as restored:
                assert [entry async for entry in restored.turns()] == recorded
                turn = await restored.submit("restored input")
                result = await turn.get_result()
                assert result.final_output == "restored answer"
                assert len(peer.requests) == 2
                assert peer.requests[1]["stream"] is (current_mode == "streaming")
                assert text in json.dumps(peer.requests[1]["input"], ensure_ascii=False)
                events = await collect(turn.events())
                assert_dispatches(
                    events, "http_sse" if current_mode == "streaming" else "http_json", 1
                )
                assert (
                    "".join(event.delta for event in events if isinstance(event, OutputTextDelta))
                    == "restored answer"
                )
                assert result.thread_usage is not None
                assert result.thread_usage.total_token_usage.total_tokens == 28
        await disconnected(peer.transports)
