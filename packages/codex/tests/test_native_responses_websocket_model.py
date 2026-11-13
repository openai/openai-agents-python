from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from openai import AsyncOpenAI

from agents import (
    Agent,
    ModelRequest,
    ModelRunOwner,
    NativeProvider,
    ResponsesWebSocketModel,
    function_tool,
)
from agents.models import ModelRetryAdviceRequest, ResponsesWebSocketOptions
from tests._asyncio import timeout


def events(index: int, *, call: bool = False) -> list[dict[str, Any]]:
    identity = f"resp_socket_{index}"
    item = (
        {
            "type": "function_call",
            "name": "lookup",
            "namespace": "functions",
            "call_id": "call_socket",
            "arguments": "{}",
        }
        if call
        else {
            "type": "message",
            "id": f"msg_socket_{index}",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "accepted"}],
        }
    )
    return [
        {"type": "response.created", "response": {"id": identity, "status": "in_progress"}},
        {"type": "response.output_item.done", "item": item},
        {
            "type": "response.completed",
            "response": {
                "id": identity,
                "status": "completed",
                "output": [item],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            },
        },
    ]


class Socket:
    def __init__(self, *, first_call: bool = False) -> None:
        self.first_call = first_call
        self.sent: list[dict[str, Any]] = []
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.closed = 0

    async def send(self, value: str) -> None:
        self.sent.append(json.loads(value))
        for event in events(len(self.sent), call=self.first_call and len(self.sent) == 1):
            self.queue.put_nowait(json.dumps(event))

    async def recv(self) -> str:
        return await self.queue.get()

    async def close(self) -> None:
        self.closed += 1


def request(owner: ModelRunOwner) -> ModelRequest:
    return ModelRequest({"model": "fixture", "input": []}, None, "session", "turn", run_owner=owner)


async def test_socket_factory_captures_options_and_detaches_request() -> None:
    made = []
    socket = Socket()
    options: ResponsesWebSocketOptions = {
        "ping_interval": None,
        "ping_timeout": 2,
        "max_size": 4096,
    }

    async def factory(current, selected):
        made.append((current, selected))
        current.body["model"] = "caller mutation"
        return socket

    async with AsyncOpenAI(
        api_key="synthetic-socket",
        base_url="https://example.invalid/v1?base=1",
        default_headers={"X-Default": "keep", "X-Omit": "remove"},
        default_query={"default": "old"},
    ) as client:
        model = ResponsesWebSocketModel(
            client,
            socket_factory=factory,
            websocket_options=options,
            request_options={
                "headers": {"x-omit": None, "X-Request": "present"},
                "query": {"default": "new", "request": "present"},
                "timeout_ms": 1234,
            },
        )
        options["max_size"] = 1
        owner = ModelRunOwner("session", "turn")
        original = request(owner)
        assert (await model.complete(original)).output
        assert not client.is_closed()
        await model.cleanup_on_run_end(owner)
        assert not client.is_closed()
    assert socket.closed == 1
    assert socket.sent[0]["model"] == "fixture"
    assert original.body["model"] == "fixture"
    selected = made[0][1]
    assert selected["url"].startswith("wss://example.invalid/v1/responses?")
    assert "default=new" in selected["url"] and "request=present" in selected["url"]
    headers = {k.lower(): v for k, v in selected["headers"].items()}
    assert headers["authorization"] == "Bearer synthetic-socket"
    assert headers["x-default"] == "keep" and "x-omit" not in headers
    assert headers["x-request"] == "present"
    assert selected["open_timeout"] == 1.234
    assert selected["websocket_options"]["max_size"] == 4096


async def test_post_send_failure_has_unsafe_native_retry_advice() -> None:
    class Broken(Socket):
        async def send(self, value: str) -> None:
            raise ValueError("application transport failure")

    socket = Broken()

    async def factory(current, options):
        return socket

    async with AsyncOpenAI(api_key="synthetic-socket") as client:
        model = ResponsesWebSocketModel(client, socket_factory=factory)
        current = request(ModelRunOwner("session", "turn"))
        with pytest.raises(RuntimeError, match="after send") as failure:
            await model.complete(current)
        advice = model.get_retry_advice(
            ModelRetryAdviceRequest(
                request=current,
                error=failure.value,
                attempt_id="attempt",
                attempt=1,
                request_attempt=1,
            )
        )
        assert advice is not None and advice.replay_safety == "unsafe" and advice.suggested is False
        assert current.run_owner is not None
        await model.cleanup_on_run_end(current.run_owner)
    assert socket.closed == 1


async def test_native_turn_owns_socket_through_tool_continuation_and_closes_before_result() -> None:
    sockets: list[Socket] = []
    owners = []
    effects = []

    @function_tool
    def lookup() -> str:
        effects.append("lookup")
        return "found"

    async def factory(current, selected):
        owners.append(current.run_owner)
        socket = Socket(first_call=not sockets)
        sockets.append(socket)
        return socket

    async with timeout(30), AsyncOpenAI(api_key="synthetic-socket") as client:
        model = ResponsesWebSocketModel(client, socket_factory=factory)
        async with NativeProvider(model_backend=model) as provider:
            agent = Agent(
                model="gpt-5.5", instructions="Use lookup.", tools=[lookup], provider=provider
            )
            async with agent.session() as session:
                first = await session.submit("First")
                assert (await first.get_result()).final_output == "accepted"
                assert sockets[0].closed == 1
                second = await session.submit("Second")
                assert (await second.get_result()).final_output == "accepted"
                assert sockets[1].closed == 1
        assert not client.is_closed()
    assert effects == ["lookup"]
    assert [len(socket.sent) for socket in sockets] == [2, 1]
    assert owners[0] is not owners[1]
    assert len(model._owners) == 0


async def test_real_websocket_retains_borrowed_client_headers_query_and_native_tool_owner() -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    handshakes = []
    frames = []
    effects = []
    closed = asyncio.Event()
    http_calls = []

    async def socket_route(current: web.Request) -> web.WebSocketResponse:
        handshakes.append((dict(current.headers), dict(current.query)))
        socket = web.WebSocketResponse()
        await socket.prepare(current)
        try:
            async for message in socket:
                frames.append(json.loads(message.data))
                for event in events(len(frames), call=len(frames) == 1):
                    await socket.send_json(event)
        finally:
            closed.set()
        return socket

    async def forbidden_http(current: web.Request) -> web.Response:
        http_calls.append(current.path)
        return web.Response(status=500)

    @function_tool
    def lookup() -> str:
        effects.append("lookup")
        return "found"

    app = web.Application()
    app.router.add_get("/v1/responses", socket_route)
    app.router.add_post("/v1/responses", forbidden_http)
    async with timeout(30), TestServer(app) as server:
        async with AsyncOpenAI(
            api_key="synthetic-live-socket",
            base_url=str(server.make_url("/v1")),
            default_headers={"X-Default": "retained", "X-Omit": "removed"},
            default_query={"base": "retained", "override": "old"},
        ) as client:
            model = ResponsesWebSocketModel(
                client,
                request_options={
                    "headers": {"x-omit": None, "X-Request": "retained"},
                    "query": {"override": "current"},
                    "timeout_ms": 5000,
                },
                websocket_options={"ping_interval": None, "ping_timeout": None, "max_size": 65536},
            )
            async with NativeProvider(model_backend=model) as provider:
                result = await Agent(
                    model="gpt-5.5",
                    instructions="Use lookup.",
                    tools=[lookup],
                    provider=provider,
                ).run("Run once.")
                assert result.final_output == "accepted"
                await closed.wait()
                assert not client.is_closed()
    assert len(handshakes) == 1 and len(frames) == 2
    headers, query = handshakes[0]
    headers = {key.lower(): value for key, value in headers.items()}
    assert headers["authorization"] == "Bearer synthetic-live-socket"
    assert headers["x-default"] == "retained" and "x-omit" not in headers
    assert headers["x-request"] == "retained"
    assert query == {"base": "retained", "override": "current"}
    assert all(frame["type"] == "response.create" and frame["stream"] is True for frame in frames)
    assert any(item["type"] == "function_call_output" for item in frames[1]["input"])
    assert effects == ["lookup"] and http_calls == []


async def test_cancelled_socket_request_joins_one_physical_close() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    sent = asyncio.Event()

    class Held(Socket):
        async def send(self, value: str) -> None:
            sent.set()

        async def close(self) -> None:
            self.closed += 1
            entered.set()
            await release.wait()

    socket = Held()

    async def connect(request: ModelRequest, options: Any) -> Held:
        return socket

    async with timeout(10), AsyncOpenAI(api_key="synthetic") as client:
        model = ResponsesWebSocketModel(client, socket_factory=connect)
        owner = ModelRunOwner("session", "turn")
        current = request(owner)
        task = asyncio.create_task(model.complete(current))
        try:
            await sent.wait()
            task.cancel()
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and socket.closed == 1
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await model.cleanup_on_run_end(owner)
        assert socket.closed == 1 and not client.is_closed()


async def test_native_result_waits_for_model_cleanup_after_passive_waiter_cancellation() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class Held(Socket):
        async def close(self) -> None:
            self.closed += 1
            entered.set()
            await release.wait()

    socket = Held()

    async def connect(request: ModelRequest, options: Any) -> Held:
        return socket

    async with timeout(20), AsyncOpenAI(api_key="synthetic") as client:
        model = ResponsesWebSocketModel(client, socket_factory=connect)
        async with NativeProvider(model_backend=model) as provider:
            async with Agent(
                model="gpt-5.5", instructions="Answer briefly.", provider=provider
            ).session() as session:
                turn = await session.submit("Complete once.")
                waiter = asyncio.create_task(turn.get_result())
                try:
                    await entered.wait()
                    assert not waiter.done() and socket.closed == 1
                    waiter.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await waiter
                    assert socket.closed == 1
                finally:
                    release.set()
                assert (await turn.get_result()).final_output == "accepted"
        assert socket.closed == 1 and not client.is_closed()
