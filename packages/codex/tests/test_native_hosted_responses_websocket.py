from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI

from agents import Agent, HostedResponsesWebSocketModel, NativeProvider, function_tool
from agents.observation import RawProviderEvent
from tests._asyncio import timeout


@pytest.mark.parametrize("terminal_before_ack", [False, True])
async def test_hosted_pause_inject_and_native_tool_owner(terminal_before_ack: bool) -> None:
    frames: list[dict[str, Any]] = []
    beta: list[str] = []
    incoming: list[dict[str, Any]] = []
    effects: list[str] = []
    closed = asyncio.Event()
    call = {
        "type": "function_call",
        "namespace": "functions",
        "call_id": "call_hosted",
        "name": "lookup",
        "arguments": "{}",
        "agent": {"agent_name": "/worker"},
        "vendor": {"enabled": False},
    }
    final = {
        "type": "message",
        "id": "msg_done",
        "role": "assistant",
        "status": "completed",
        "phase": "final_answer",
        "agent": {"agent_name": "/root"},
        "content": [{"type": "output_text", "text": "done"}],
    }

    async def route(request: web.Request) -> web.WebSocketResponse:
        beta.append(request.headers["OpenAI-Beta"])
        socket = web.WebSocketResponse()
        await socket.prepare(request)

        async def emit(frame: dict[str, Any]) -> None:
            incoming.append(frame)
            await socket.send_json(frame)

        try:
            async for message in socket:
                frame = json.loads(message.data)
                frames.append(frame)
                if frame["type"] == "response.create":
                    assert "stream" not in frame
                    assert frame["multi_agent"] == {"enabled": True, "max_concurrent_subagents": 2}
                    await emit(
                        {
                            "type": "response.created",
                            "response": {"id": "resp_hosted", "output": []},
                        }
                    )
                    await emit(
                        {
                            "type": "response.output_item.done",
                            "item": {**final, "id": "msg_other", "agent": {"agent_name": "/other"}},
                        }
                    )
                    await emit({"type": "response.output_item.done", "item": call})
                else:
                    assert frame["type"] == "response.inject"
                    assert frame["response_id"] == "resp_hosted"
                    assert len(frame["input"]) == 1
                    assert frame["input"][0]["type"] == "function_call_output"
                    assert frame["input"][0]["call_id"] == call["call_id"]
                    done = {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_hosted",
                            "status": "completed",
                            "output": [
                                call,
                                {"type": "agent_message", "id": "collaboration"},
                                {**final, "id": "msg_other", "agent": {"agent_name": "/other"}},
                                final,
                            ],
                            "usage": {
                                "input_tokens": 3,
                                "output_tokens": 2,
                                "total_tokens": 5,
                                "input_tokens_details": {
                                    "cached_tokens": 2,
                                    "cache_write_tokens": 1,
                                },
                                "output_tokens_details": {"reasoning_tokens": 1},
                            },
                        },
                    }
                    ack = {"type": "response.inject.created", "response_id": "resp_hosted"}
                    for event in [done, ack] if terminal_before_ack else [ack, done]:
                        await emit(event)
        finally:
            closed.set()
        return socket

    @function_tool
    def lookup() -> str:
        effects.append("lookup")
        assert len(frames) == 1
        assert not closed.is_set()
        return "synthetic record"

    app = web.Application()
    app.router.add_get("/v1/responses", route)
    async with timeout(20), TestServer(app) as server:
        async with AsyncOpenAI(
            api_key="synthetic-hosted", base_url=str(server.make_url("/v1"))
        ) as client:
            model = HostedResponsesWebSocketModel(
                client,
                max_concurrent_subagents=2,
                request_options={"headers": {"OpenAI-Beta": "existing-beta"}},
            )
            async with NativeProvider(model_backend=model) as provider:
                agent = Agent(
                    model="gpt-5.5", instructions="Use lookup.", tools=[lookup], provider=provider
                )
                async with agent.session(
                    include_raw_provider_events=True, observation_retention="all"
                ) as session:
                    turn = await session.submit("start")
                    raw = [
                        event
                        async for event in turn.events(include_execution_details=True)
                        if isinstance(event, RawProviderEvent)
                    ]
                    result = await turn.get_result()
                assert [(event.source, event.transport, event.payload) for event in raw] == [
                    ("responses", "websocket", frame) for frame in incoming
                ]
            assert result.final_output == "done"
            assert result.run_usage.requests == 1
            assert result.run_usage.missing_usage_requests == 0
            assert result.run_usage.cached_input_tokens == 2
            assert result.run_usage.cache_write_input_tokens == 1
            assert result.run_usage.reasoning_output_tokens == 1
            assert effects == ["lookup"]
            assert [frame["type"] for frame in frames] == ["response.create", "response.inject"]
            assert beta == ["existing-beta,responses_multi_agent=v1"]
            await closed.wait()
            assert not client.is_closed()
            receipt = os.environ.get("HOSTED_BETA_WIRE_RECEIPT")
            if receipt:
                usage = result.run_usage
                Path(receipt + "-" + str(terminal_before_ack) + ".json").write_text(
                    json.dumps(
                        {
                            "frames": frames,
                            "beta": beta,
                            "effects": effects,
                            "provider_creates": 1,
                            "native_usage": {
                                "requests": usage.requests,
                                "missing_usage_requests": usage.missing_usage_requests,
                                "input_tokens": usage.input_tokens,
                                "output_tokens": usage.output_tokens,
                                "total_tokens": usage.total_tokens,
                            },
                        },
                        indent=2,
                    )
                    + "\n"
                )


@pytest.mark.parametrize("store", [True, False])
@pytest.mark.parametrize("terminal_first", [True, False])
async def test_hosted_terminal_fallback(store: bool, terminal_first: bool) -> None:
    frames: list[dict[str, Any]] = []
    effects: list[str] = []
    call = {
        "type": "function_call",
        "id": "fc_fallback",
        "namespace": "functions",
        "call_id": "call_fallback",
        "name": "lookup",
        "arguments": "{}",
        "agent": {"agent_name": "/worker"},
        "vendor": {"enabled": False},
    }
    hidden = {
        "type": "agent_message",
        "id": "collaboration_fallback",
        "vendor": {"exact": "retained"},
    }
    final = {
        "type": "message",
        "id": "msg_fallback",
        "role": "assistant",
        "status": "completed",
        "phase": "final_answer",
        "agent": {"agent_name": "/root"},
        "content": [{"type": "output_text", "text": "done"}],
    }
    first = {
        "id": "resp_first",
        "status": "completed",
        "store": store,
        "output": [call, hidden],
        "usage": {
            "input_tokens": 3,
            "output_tokens": 2,
            "total_tokens": 5,
            "input_tokens_details": {"cached_tokens": 2, "cache_write_tokens": 1},
            "output_tokens_details": {"reasoning_tokens": 1},
        },
    }

    async def route(request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        async for message in socket:
            frame = json.loads(message.data)
            frames.append(frame)
            if len(frames) == 1:
                assert frame["type"] == "response.create"
                assert frame["store"] is store
                await socket.send_json(
                    {"type": "response.created", "response": {"id": "resp_first", "output": []}}
                )
                await socket.send_json({"type": "response.output_item.done", "item": call})
            elif len(frames) == 2:
                assert frame["type"] == "response.inject"
                assert len(frame["input"]) == 1
                done = {"type": "response.completed", "response": first}
                rejected = {
                    "type": "response.inject.failed",
                    "error": {"code": "response_already_completed"},
                    "input": frame["input"],
                }
                for event in [done, rejected] if terminal_first else [rejected, done]:
                    await socket.send_json(event)
            else:
                assert len(frames) == 3
                assert frame["type"] == "response.create"
                assert "stream" not in frame
                if store:
                    assert frame["previous_response_id"] == "resp_first"
                    assert frame["input"] == frames[1]["input"]
                else:
                    assert "previous_response_id" not in frame
                    assert frame["input"] == [
                        *frames[0]["input"],
                        *first["output"],
                        *frames[1]["input"],
                    ]
                await socket.send_json(
                    {"type": "response.created", "response": {"id": "resp_second", "output": []}}
                )
                await socket.send_json(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_second",
                            "status": "completed",
                            "output": [final],
                            "usage": {
                                "input_tokens": 7,
                                "output_tokens": 4,
                                "total_tokens": 11,
                                "input_tokens_details": {
                                    "cached_tokens": 3,
                                    "cache_write_tokens": 2,
                                },
                                "output_tokens_details": {"reasoning_tokens": 2},
                            },
                        },
                    }
                )
        return socket

    @function_tool
    def lookup() -> str:
        effects.append("lookup")
        return "result"

    app = web.Application()
    app.router.add_get("/v1/responses", route)
    async with timeout(20), TestServer(app) as server:
        async with AsyncOpenAI(
            api_key="synthetic-hosted", base_url=str(server.make_url("/v1"))
        ) as client:
            model = HostedResponsesWebSocketModel(client)
            async with NativeProvider(model_backend=model) as provider:
                result = await Agent(
                    model="gpt-5.5",
                    instructions="Use lookup.",
                    tools=[lookup],
                    provider=provider,
                    model_settings={"store": store},
                ).run("start")
            assert result.final_output == "done"
            assert effects == ["lookup"]
            assert [frame["type"] for frame in frames] == [
                "response.create",
                "response.inject",
                "response.create",
            ]
            usage = result.run_usage
            assert (
                usage.input_tokens,
                usage.output_tokens,
                usage.total_tokens,
                usage.cached_input_tokens,
                usage.cache_write_input_tokens,
                usage.reasoning_output_tokens,
                usage.requests,
                usage.missing_usage_requests,
            ) == (10, 6, 16, 5, 3, 3, 2, 0)
            assert not client.is_closed()


async def test_hosted_abort_closes_before_joining_entered_tool_without_redispatch() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()
    frames: list[dict[str, Any]] = []
    effects: list[str] = []

    async def route(request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        try:
            async for message in socket:
                frames.append(json.loads(message.data))
                await socket.send_json(
                    {"type": "response.created", "response": {"id": "resp_abort", "output": []}}
                )
                await socket.send_json(
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "function_call",
                            "namespace": "functions",
                            "call_id": "call_abort",
                            "name": "lookup",
                            "arguments": "{}",
                        },
                    }
                )
        finally:
            closed.set()
        return socket

    @function_tool
    async def lookup() -> str:
        effects.append("lookup")
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        return "result"

    app = web.Application()
    app.router.add_get("/v1/responses", route)
    async with timeout(20), TestServer(app) as server:
        async with AsyncOpenAI(api_key="synthetic", base_url=str(server.make_url("/v1"))) as client:
            model = HostedResponsesWebSocketModel(client)
            async with NativeProvider(model_backend=model) as provider:
                task = asyncio.create_task(
                    Agent(
                        model="gpt-5.5",
                        instructions="Use lookup.",
                        tools=[lookup],
                        provider=provider,
                    ).run("start")
                )
                try:
                    await entered.wait()
                    task.cancel()
                    await closed.wait()
                    assert not task.done()
                    assert effects == ["lookup"]
                    assert [frame["type"] for frame in frames] == ["response.create"]
                finally:
                    release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert not client.is_closed()


@pytest.mark.parametrize(
    "invalid_field",
    ["response", "usage", "input_tokens_details", "output", "nonfinite", "overflow", "oversized"],
)
async def test_hosted_invalid_frame_does_not_retain_peer_diagnostic_objects(invalid_field) -> None:
    from tests.test_native_responses_websocket_review import _diagnostics

    private = "synthetic-private-hosted-provider-frame"
    pending: asyncio.Queue[str] = asyncio.Queue()
    closes = []

    class Socket:
        async def send(self, value: str) -> None:
            pending.put_nowait(
                json.dumps({"type": "response.created", "response": {"id": "resp_bad_hosted"}})
            )
            response: Any = {"id": "resp_bad_hosted", "status": "completed", "output": []}
            if invalid_field == "response":
                response = private
            elif invalid_field == "usage":
                response["usage"] = [private]
            elif invalid_field == "input_tokens_details":
                response["usage"] = {"input_tokens_details": private}
            else:
                response["output"] = [private]
            terminal = json.dumps({"type": "response.completed", "response": response})
            if invalid_field in ("nonfinite", "overflow"):
                number = "NaN" if invalid_field == "nonfinite" else "1e999"
                terminal = terminal[:-1] + f',"{private}":{number}' + "}"
            elif invalid_field == "oversized":
                terminal = terminal[:-1] + f',"{private}":"' + "x" * 2048 + '"}'
            pending.put_nowait(terminal)

        async def recv(self) -> str:
            return await pending.get()

        async def close(self) -> None:
            closes.append(True)

    async def connect(_request, _options):
        return Socket()

    async with timeout(20), AsyncOpenAI(api_key="synthetic") as client:
        backend = HostedResponsesWebSocketModel(
            client, socket_factory=connect, websocket_options={"max_size": 1024}
        )
        async with NativeProvider(model_backend=backend) as provider:
            agent = Agent(model="gpt-5.5", instructions="Complete once.", provider=provider)
            with pytest.raises(Exception) as failure:
                await agent.run("Synthetic request.")
    assert private not in _diagnostics(failure.value)
    assert len(closes) == 1


async def test_hosted_initial_connect_failure_reaches_result_and_event_reader() -> None:
    attempts = 0

    async def connect(_request, _options):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("synthetic hosted connection failure")

    async with timeout(20), AsyncOpenAI(api_key="synthetic") as client:
        model = HostedResponsesWebSocketModel(client, socket_factory=connect)
        async with NativeProvider(model_backend=model) as provider:
            async with Agent(instructions="Complete once.", provider=provider).session() as session:
                turn = await session.submit("Synthetic request.")
                with pytest.raises(Exception, match="synthetic hosted connection failure"):
                    await turn.get_result()
                assert [event async for event in turn.events()]
        assert attempts == 1
        assert not client.is_closed()


@pytest.mark.parametrize("terminal_first", [True, False])
async def test_hosted_nonstored_fallback_replays_all_injected_outputs(terminal_first) -> None:
    frames: list[dict[str, Any]] = []
    first, second = [
        {
            "type": "function_call",
            "id": f"fc_{key}",
            "namespace": "functions",
            "call_id": f"call_{key}",
            "name": "lookup",
            "arguments": json.dumps({"key": key}),
        }
        for key in ("one", "two")
    ]
    effects: list[str] = []

    async def route(request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        async for message in socket:
            frame = json.loads(message.data)
            frames.append(frame)
            if len(frames) == 1:
                await socket.send_json(
                    {"type": "response.created", "response": {"id": "first", "output": []}}
                )
                await socket.send_json({"type": "response.output_item.done", "item": first})
            elif len(frames) == 2:
                await socket.send_json({"type": "response.inject.created", "response_id": "first"})
                await socket.send_json({"type": "response.output_item.done", "item": second})
            elif len(frames) == 3:
                terminal = {
                    "type": "response.completed",
                    "response": {
                        "id": "first",
                        "store": False,
                        "status": "completed",
                        "output": [first, second],
                    },
                }
                rejected = {
                    "type": "response.inject.failed",
                    "error": {"code": "response_already_completed"},
                }
                for event in (terminal, rejected) if terminal_first else (rejected, terminal):
                    await socket.send_json(event)
            else:
                await socket.send_json(
                    {"type": "response.created", "response": {"id": "second", "output": []}}
                )
                await socket.send_json(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "second",
                            "status": "completed",
                            "output": [
                                {
                                    "type": "message",
                                    "id": "msg_two",
                                    "role": "assistant",
                                    "status": "completed",
                                    "phase": "final_answer",
                                    "agent": {"agent_name": "/root"},
                                    "content": [{"type": "output_text", "text": "finished"}],
                                }
                            ],
                        },
                    }
                )
        return socket

    @function_tool
    def lookup(key: str) -> str:
        effects.append(key)
        return f"result for {key}"

    app = web.Application()
    app.router.add_get("/v1/responses", route)
    async with timeout(20), TestServer(app) as server:
        async with AsyncOpenAI(api_key="synthetic", base_url=str(server.make_url("/v1"))) as client:
            backend = HostedResponsesWebSocketModel(client)
            async with NativeProvider(model_backend=backend) as provider:
                result = await Agent(
                    model="gpt-5.5",
                    instructions="Use lookup.",
                    tools=[lookup],
                    provider=provider,
                    model_settings={"store": False},
                ).run("start")
    assert result.final_output == "finished"
    assert effects == ["one", "two"]
    assert [frame["type"] for frame in frames] == [
        "response.create",
        "response.inject",
        "response.inject",
        "response.create",
    ]
    assert frames[-1]["input"] == [
        *frames[0]["input"],
        first,
        *frames[1]["input"],
        second,
        *frames[2]["input"],
    ]
    assert result.run_usage.requests == 2


@pytest.mark.parametrize("operation", ["create", "inject", "fallback"])
async def test_hosted_send_failure_releases_private_locals_and_keeps_cause(operation) -> None:
    from tests.test_native_responses_websocket_review import _diagnostics

    private = "synthetic-hosted-private-send"
    failure = ConnectionError("hosted socket disconnected")
    pending: asyncio.Queue[str] = asyncio.Queue()
    sends = []
    call = {
        "type": "function_call",
        "id": "fc_private",
        "namespace": "functions",
        "call_id": "call_private",
        "name": "lookup",
        "arguments": "{}",
    }

    class Socket:
        async def send(self, payload: str) -> None:
            frame = json.loads(payload)
            sends.append(frame["type"])
            if len(sends) == {"create": 1, "inject": 2, "fallback": 3}[operation]:
                assert private in payload
                raise failure
            if len(sends) == 1:
                pending.put_nowait(
                    json.dumps({"type": "response.created", "response": {"id": "first"}})
                )
                pending.put_nowait(json.dumps({"type": "response.output_item.done", "item": call}))
            else:
                pending.put_nowait(
                    json.dumps(
                        {
                            "type": "response.inject.failed",
                            "error": {"code": "response_already_completed"},
                        }
                    )
                )
                pending.put_nowait(
                    json.dumps(
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "first",
                                "store": False,
                                "status": "completed",
                                "output": [call],
                            },
                        }
                    )
                )

        async def recv(self) -> str:
            return await pending.get()

        async def close(self) -> None:
            pass

    async def connect(_request, _options):
        return Socket()

    @function_tool
    def lookup() -> str:
        return private

    async with timeout(20), AsyncOpenAI(api_key="synthetic") as client:
        backend = HostedResponsesWebSocketModel(client, socket_factory=connect)
        async with NativeProvider(model_backend=backend) as provider:
            agent = Agent(
                model="gpt-5.5",
                instructions=f"Use lookup. {private}" if operation == "create" else "Use lookup.",
                provider=provider,
                tools=[lookup],
                model_settings={"store": False},
            )
            with pytest.raises(Exception, match="Hosted|WebSocket|disconnected") as caught:
                await agent.run("Public synthetic input.")
        assert not client.is_closed()

    causes = [caught.value]
    for cause in causes:
        for nested in (cause.__cause__, cause.__context__, getattr(cause, "model_error", None)):
            if isinstance(nested, BaseException) and nested not in causes:
                causes.append(nested)
    assert failure in causes
    assert private not in _diagnostics(caught.value)
    assert len(sends) == {"create": 1, "inject": 2, "fallback": 3}[operation]


async def test_hosted_reconnects_closed_peer_with_the_same_native_run_owner() -> None:
    from agents import ModelRequest, ModelRunOwner

    sockets = []
    owner = ModelRunOwner("session", "turn")

    class Socket:
        close_code: int | None = None
        sends = 0

        def __init__(self, output: str) -> None:
            self.output = output

        async def send(self, payload: str) -> None:
            assert self.close_code is None
            self.sends += 1

        async def recv(self) -> str:
            if self.sends == 1 and self.close_code is None:
                self.close_code = 1000
                return json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": self.output,
                            "status": "completed",
                            "output": [
                                {
                                    "type": "message",
                                    "id": self.output,
                                    "role": "assistant",
                                    "status": "completed",
                                    "phase": "final_answer",
                                    "agent": {"agent_name": "/root"},
                                    "content": [{"type": "output_text", "text": self.output}],
                                }
                            ],
                        },
                    }
                )
            raise AssertionError("unexpected receive")

        async def close(self) -> None:
            self.close_code = 1000

    async def connect(request, _options):
        assert request.run_owner is owner
        socket = Socket(f"answer-{len(sockets) + 1}")
        sockets.append(socket)
        return socket

    async with AsyncOpenAI(api_key="synthetic") as client:
        backend = HostedResponsesWebSocketModel(client, socket_factory=connect)
        for ordinal in (1, 2):
            request = ModelRequest(
                {"model": "gpt-5.5", "input": []}, None, "session", "turn", run_owner=owner
            )
            object.__setattr__(request, "_account_provider_request", lambda op: "lineage")
            result = await backend.complete(request)
            assert result.output[0]["content"][0]["text"] == f"answer-{ordinal}"
        await backend.cleanup_on_run_end(owner)
        assert [socket.sends for socket in sockets] == [1, 1]
        assert not client.is_closed()
