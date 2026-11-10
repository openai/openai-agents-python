"""Public provider transport choices retain history and application ownership."""

from __future__ import annotations

import asyncio
import json
import socket
import socketserver
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiohttp import ClientConnectionResetError, WSMsgType, web
from aiohttp.test_utils import TestServer

from agents import (
    Agent,
    FunctionTool,
    InteractionExpiredError,
    NativeError,
    NativeProvider,
    ToolContext,
    TurnStoppedError,
    function_tool,
)
from agents.observation import ContextCompacted, OutputTextDelta, ToolFinished, TurnFinished
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, function_call, tool_output
from tests.test_native_agent_observation import text_response


def unauthorized() -> web.Response:
    return web.json_response(
        {
            "error": {
                "message": "synthetic credential rejected",
                "type": "invalid_request_error",
                "code": "invalid_api_key",
            }
        },
        status=401,
        headers={"Retry-After": "0"},
    )


@dataclass
class WebSocketPeer(ResponsesFixture):
    """Reconstruct continuation input only on the synthetic provider side."""

    upgrade_status: int | None = None
    http_failure: tuple[int, str, str] | None = None
    tls_context: ssl.SSLContext | None = None
    upgrades: list[dict[str, str]] = field(default_factory=list)
    frames: list[dict[str, Any]] = field(default_factory=list)
    http_requests: list[dict[str, Any]] = field(default_factory=list)
    completed: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    sockets: set[web.WebSocketResponse] = field(default_factory=set)
    errors: list[Exception] = field(default_factory=list)
    connections: int = 0
    warmups: int = 0
    handlers: int = 0
    providers: list[NativeProvider] = field(default_factory=list)
    disconnected: asyncio.Event = field(default_factory=asyncio.Event)
    creation_override: dict[str, Any] | None = None
    completion_id_override: str | None = None
    expected_disconnect_event: dict[str, Any] | None = None

    def provider(
        self, *, websocket: bool = True, key: str | None = "synthetic-ws-key"
    ) -> NativeProvider:
        provider = NativeProvider(base_url=self.url, api_key=key, supports_websockets=websocket)
        self.providers.append(provider)
        return provider

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.http_requests.append(await request.json())
        if self.http_failure is not None:
            self.request_headers.append(
                {key.lower(): value for key, value in request.headers.items()}
            )
            status, code, request_id = self.http_failure
            return web.json_response(
                {
                    "error": {
                        "message": "Final HTTP rejection",
                        "type": "invalid_request_error",
                        "code": code,
                        "param": None,
                    }
                },
                status=status,
                headers={"x-request-id": request_id},
            )
        if self.upgrade_status == 401:
            self.request_headers.append(
                {key.lower(): value for key, value in request.headers.items()}
            )
            return unauthorized()
        return await super().handle(request)

    async def websocket(self, request: web.Request) -> web.StreamResponse:
        headers = {key.lower(): value for key, value in request.headers.items()}
        self.upgrades.append(headers)
        if self.upgrade_status == 401:
            return unauthorized()
        if self.upgrade_status is not None:
            return web.json_response(
                {"error": "synthetic upgrade rejection"}, status=self.upgrade_status
            )
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self.connections += 1
        self.handlers += 1
        self.sockets.add(socket)
        self.disconnected.clear()
        try:
            async for message in socket:
                assert message.type == WSMsgType.TEXT
                payload = json.loads(message.data)
                assert payload["type"] == "response.create"
                assert payload["store"] is False
                self.frames.append(payload)
                previous = payload.get("previous_response_id")
                if previous is None:
                    inputs = deepcopy(payload["input"])
                else:
                    assert previous in self.completed, "unknown previous_response_id"
                    inputs = deepcopy(self.completed[previous]) + deepcopy(payload["input"])
                response_id = f"resp_ws_{len(self.frames)}"
                if payload.get("generate") is False:
                    self.warmups += 1
                    events: list[dict[str, Any]] = []
                    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
                else:
                    self.requests.append({**payload, "input": inputs})
                    self.request_headers.append(headers)
                    assert self.replies, "unexpected generating request"
                    events = self.replies.popleft()
                    usage = {
                        "input_tokens": 11,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens": 3,
                        "output_tokens_details": {"reasoning_tokens": 0},
                        "total_tokens": 14,
                    }
                output = [
                    event["item"]
                    for event in events
                    if event["type"] == "response.output_item.done"
                ]
                # The provider retains history under the actual terminal ID
                # it returns, including when a test changes creation metadata.
                terminal_id = self.completion_id_override or response_id
                self.completed[terminal_id] = inputs + deepcopy(output)
                await socket.send_json(
                    {
                        "type": "response.created",
                        "response": (
                            self.creation_override
                            if self.creation_override is not None
                            else {"id": response_id}
                        ),
                    }
                )
                may_disconnect = False

                async def send_event(event: dict[str, Any]) -> bool:
                    nonlocal may_disconnect
                    try:
                        await socket.send_json(event)
                    except ClientConnectionResetError:
                        if may_disconnect:
                            return False
                        raise
                    # The chosen frame has been marking the earliest permitted peer close.
                    # Other transport and hook errors have been remaining failures.
                    if event is self.expected_disconnect_event:
                        may_disconnect = True
                    return True

                for event in events:
                    if self.before_event is not None:
                        await self.before_event(event)
                    if not await send_event(event):
                        return socket
                completed = {
                    "type": "response.completed",
                    "response": {
                        "id": terminal_id,
                        "output": output,
                        "usage": usage,
                    },
                }
                if payload.get("generate") is not False and self.before_event is not None:
                    await self.before_event(completed)
                if not await send_event(completed):
                    return socket
        except Exception as error:
            self.errors.append(error)
            raise
        finally:
            # aiohttp can cancel the receive when the native client disconnects.
            self.handlers -= 1
            self.sockets.discard(socket)
            if self.handlers == 0:
                self.disconnected.set()
        return socket

    async def close_sockets(self, app: web.Application) -> None:
        await asyncio.gather(*(socket.close() for socket in tuple(self.sockets)))


@asynccontextmanager
async def peer(
    *,
    upgrade_status: int | None = None,
    tls_context: ssl.SSLContext | None = None,
    http_failure: tuple[int, str, str] | None = None,
) -> AsyncIterator[WebSocketPeer]:
    fixture = WebSocketPeer(
        upgrade_status=upgrade_status, tls_context=tls_context, http_failure=http_failure
    )
    app = web.Application()
    app.router.add_get("/v1/responses", fixture.websocket)
    app.router.add_post("/v1/responses", fixture.handle)
    app.on_shutdown.append(fixture.close_sockets)
    server = TestServer(app, shutdown_timeout=1, scheme="https" if tls_context else "http")
    await server.start_server(ssl=tls_context)
    try:
        fixture.url = str(server.make_url("/v1"))
        try:
            yield fixture
        finally:
            for provider in fixture.providers:
                await provider.aclose()
            if fixture.connections:
                async with timeout(10):
                    await fixture.disconnected.wait()
            # Observe client retirement before server shutdown could hide it.
            assert not fixture.sockets and fixture.handlers == 0
    finally:
        await server.close()
    assert not fixture.sockets and fixture.handlers == 0
    assert not fixture.errors


def message_text(items: list[dict[str, Any]], role: str) -> list[str]:
    return [
        part["text"]
        for item in items
        if item.get("role") == role
        for part in item.get("content", [])
        if "text" in part
    ]


async def test_provider_never_shares_generated_session_history() -> None:
    async with timeout(20), peer() as server:
        shared = server.provider()
        agent = Agent(instructions="Answer.", provider=shared)
        sessions = set()
        for index in range(3):
            server.respond(assistant_output(f"answer-{index}"))
            result = await agent.run(f"prompt-{index}")
            assert result.final_output == f"answer-{index}"
            await server.disconnected.wait()
            assert not server.sockets and server.handlers == 0
            sessions.add(result.turn.session_id)
            assert message_text(server.requests[-1]["input"], "user") == [f"prompt-{index}"]
            assert message_text(server.requests[-1]["input"], "assistant") == []
            # Core can continue its own non-generating warmup. It must never
            # continue a response generated for another Session.
            older_generated = {
                f"resp_ws_{number}"
                for number, frame in enumerate(server.frames[:-1], 1)
                if frame.get("generate") is not False
            }
            assert server.requests[-1].get("previous_response_id") not in older_generated
            assert result.thread_usage is not None
            assert result.thread_usage.total_token_usage.total_tokens == 14
        assert len(sessions) == 3
        assert not server.http_requests


async def test_concurrent_plain_runs_retire_sockets_before_provider_close() -> None:
    async with timeout(20), peer() as server:
        agent = Agent(instructions="Answer.", provider=server.provider())
        responses = [assistant_output(f"answer-{index}") for index in range(2)]
        arrivals = [asyncio.Event(), asyncio.Event()]
        release = asyncio.Event()

        async def hold_response(event: dict[str, Any]) -> None:
            for index, response in enumerate(responses):
                if event is response:
                    arrivals[index].set()
                    await release.wait()

        server.before_event = hold_response
        for response in responses:
            server.respond(response)
        runs = []
        try:
            for index, arrival in enumerate(arrivals):
                runs.append(asyncio.create_task(agent.run(f"prompt-{index}")))
                await arrival.wait()
            assert len(server.requests) == 2
            assert all(not run.done() for run in runs)
            release.set()
            results = await asyncio.gather(*runs)
        finally:
            release.set()
            for run in runs:
                if not run.done():
                    run.cancel()
            await asyncio.gather(*runs, return_exceptions=True)
        assert [result.final_output for result in results] == ["answer-0", "answer-1"]
        assert len({result.turn.session_id for result in results}) == 2
        assert [message_text(request["input"], "user") for request in server.requests] == [
            ["prompt-0"],
            ["prompt-1"],
        ]
        assert all(not message_text(request["input"], "assistant") for request in server.requests)
        await server.disconnected.wait()
        assert not server.sockets and server.handlers == 0
        assert server.connections == 2 and not server.http_requests


async def test_cancelled_plain_run_retires_socket_before_provider_close() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    events = text_response("held-id", "cancelled-answer")

    async def hold(event: dict[str, Any]) -> None:
        if event is events[1]:
            entered.set()
            await release.wait()

    async with timeout(20), peer() as server:
        server.before_event = hold
        server.expected_disconnect_event = events[0]
        server.respond(*events)
        agent = Agent(instructions="Answer.", provider=server.provider())
        running = asyncio.create_task(agent.run("cancelled-prompt"))
        try:
            await entered.wait()
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        finally:
            release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        await server.disconnected.wait()
        assert not server.sockets and server.handlers == 0
        server.before_event = None
        server.expected_disconnect_event = None
        server.respond(assistant_output("fresh-answer"))
        assert (await agent.run("fresh-prompt")).final_output == "fresh-answer"
        await server.disconnected.wait()
        assert not server.sockets and server.handlers == 0
        assert message_text(server.requests[-1]["input"], "user") == ["fresh-prompt"]
        assert not message_text(server.requests[-1]["input"], "assistant")
        assert server.connections == 2 and not server.http_requests


async def test_failed_plain_run_retires_socket_before_provider_close() -> None:
    added, delta, _ = text_response("first-id", "unaccepted-answer")
    malformed = {
        "type": "response.output_item.added",
        "item": {"type": "message", "id": "broken", "role": "assistant"},
    }
    async with timeout(20), peer() as server:
        server.expected_disconnect_event = malformed
        server.respond(added, delta, malformed)
        agent = Agent(instructions="Answer.", provider=server.provider())
        with pytest.raises(
            NativeError, match="^Native model response ended without complete supported output$"
        ):
            await agent.run("failed-prompt")
        await server.disconnected.wait()
        assert not server.sockets and server.handlers == 0
        server.expected_disconnect_event = None
        server.respond(assistant_output("fresh-answer"))
        assert (await agent.run("fresh-prompt")).final_output == "fresh-answer"
        await server.disconnected.wait()
        assert not server.sockets and server.handlers == 0
        assert message_text(server.requests[-1]["input"], "user") == ["fresh-prompt"]
        assert not message_text(server.requests[-1]["input"], "assistant")
        assert server.connections == 2 and not server.http_requests


@pytest.mark.parametrize("cancel", [False, True], ids=["success", "cancel"])
async def test_plain_run_tool_callback_joins_cleanup_before_provider_close(cancel: bool) -> None:
    entered = asyncio.Event()
    finish = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleaned = asyncio.Event()
    effects = []

    @function_tool
    async def held() -> str:
        """Return one application value after the caller releases it."""
        effects.append("accepted-effect")
        entered.set()
        try:
            await finish.wait()
            return "tool-value"
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            cleaned.set()

    async with timeout(20), peer() as server:
        server.respond(function_call("held", {}, "call_held_ws"))
        if not cancel:
            server.respond(assistant_output("tool-finished"))
        agent = Agent(instructions="Use held when asked.", tools=[held], provider=server.provider())
        running = asyncio.create_task(agent.run("use held"))
        try:
            await entered.wait()
            if cancel:
                running.cancel()
            else:
                finish.set()
            await cleanup_started.wait()
            assert not running.done() and not cleaned.is_set()
            assert effects == ["accepted-effect"]
            release_cleanup.set()
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await running
                assert len(server.requests) == 1
            else:
                result = await running
                assert result.final_output == "tool-finished"
                assert tool_output(server.requests[-1], "call_held_ws") == "tool-value"
                assert len(server.requests) == 2
        finally:
            finish.set()
            release_cleanup.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        assert cleaned.is_set() and effects == ["accepted-effect"]
        await server.disconnected.wait()
        assert not server.sockets and server.handlers == 0
        server.respond(assistant_output("fresh-answer"))
        assert (await agent.run("fresh-prompt")).final_output == "fresh-answer"
        await server.disconnected.wait()
        assert not server.sockets and server.handlers == 0
        assert message_text(server.requests[-1]["input"], "user") == ["fresh-prompt"]
        assert not message_text(server.requests[-1]["input"], "assistant")
        assert "call_held_ws" not in json.dumps(server.requests[-1]["input"])
        assert effects == ["accepted-effect"]
        assert server.connections == 2 and not server.http_requests


async def test_concurrent_sessions_retire_sockets_without_sharing_history() -> None:
    async with timeout(20), peer() as server:
        agent = Agent(instructions="Answer.", provider=server.provider())
        async with agent.session() as first, agent.session() as second:
            for index, session in enumerate((first, second)):
                server.respond(assistant_output(f"answer-{index}"))
                submission = await session.submit(f"prompt-{index}")
                assert (await (submission).get_result()).final_output == f"answer-{index}"
                assert message_text(server.requests[-1]["input"], "user") == [f"prompt-{index}"]
            assert server.connections == 2
            assert len(server.sockets) == 2
        await server.disconnected.wait()
        assert not server.sockets and server.handlers == 0
        server.respond(assistant_output("next-answer"))
        assert (await agent.run("next-prompt")).final_output == "next-answer"
        assert server.connections == 3
        older_generated = {
            f"resp_ws_{number}"
            for number, frame in enumerate(server.frames[:-1], 1)
            if frame.get("generate") is not False
        }
        assert server.requests[-1].get("previous_response_id") not in older_generated
        assert message_text(server.requests[-1]["input"], "user") == ["next-prompt"]
        assert message_text(server.requests[-1]["input"], "assistant") == []


@pytest.mark.parametrize("malformed", ["created", "terminal"])
async def test_fresh_authenticated_socket_keeps_tools_and_history_session_owned(
    malformed: str,
) -> None:
    effects = []

    @function_tool
    async def lookup() -> str:
        """Read an application value."""
        effects.append("current-lookup")
        return "value"

    async with timeout(20), peer() as server:
        agent = Agent(instructions="Answer.", provider=server.provider(), tools=[lookup])
        server.respond(assistant_output("previous-session-only-answer"))
        result = await agent.run("previous-session-only-input")
        assert result.final_output == "previous-session-only-answer"
        await server.disconnected.wait()
        assert not server.sockets
        warmups_before_current = server.warmups
        async with agent.session() as current:
            # Change generating metadata after Core's own warmup. Otherwise a
            # bad warmup ID would test the synthetic fixture before any call.
            while server.warmups <= warmups_before_current:
                await asyncio.sleep(0)
            if malformed == "created":
                server.creation_override = {}
            else:
                server.completion_id_override = "different-response"
            server.respond(function_call("lookup", {}, "current-tool"))
            server.respond(assistant_output("current answer"))
            handle = await current.submit("use lookup in this session")
            result = await (handle).get_result()
            assert result.final_output == "current answer"
            assert result.thread_usage is not None
            assert result.thread_usage.total_token_usage.total_tokens == 28
            stored = json.dumps((await current.save_state()).history)
        assert effects == ["current-lookup"]
        assert len(server.requests) == 3
        assert server.connections == 2
        assert tool_output(server.requests[-1], "current-tool") == "value"
        assert message_text(server.requests[1]["input"], "user") == ["use lookup in this session"]
        assert message_text(server.requests[1]["input"], "assistant") == []
        assert "previous-session-only-input" not in stored
        assert "previous-session-only-answer" not in stored
        assert "previous-session-only-input" not in json.dumps(server.requests[1:])
        assert "previous-session-only-answer" not in json.dumps(server.requests[1:])
        assert all(
            headers["authorization"] == "Bearer synthetic-ws-key"
            for headers in server.request_headers
        )
        assert not server.http_requests


async def test_provider_without_created_identity_works_without_socket_reuse() -> None:
    async with timeout(20), peer() as server:
        server.creation_override = {}
        agent = Agent(instructions="Answer.", provider=server.provider())
        for text in ("first", "second"):
            server.respond(assistant_output(text))
            assert (await agent.run(text)).final_output == text
        assert len(server.requests) == 2
        assert server.connections == 2


@pytest.mark.parametrize("cancel", [False, True], ids=["reply", "cancel"])
async def test_fresh_manual_websocket_keeps_core_grant_authority(cancel: bool) -> None:
    from tests.test_native_manual_tools import LookupArgs, next_request

    lookup = FunctionTool(
        name="lookup", description="Look up a key", input_type=LookupArgs, output_type=str
    )
    async with timeout(20), peer() as server:
        server.respond(function_call("lookup", {"key": "A"}, "manual-ws-call"))
        agent = Agent(instructions="Use lookup.", provider=server.provider(), tools=[lookup])
        async with agent.session() as session:
            # Manual WebSocket grants keep their published Core owner.
            handle = await session.submit("Look up A")
            request = await next_request(session.interactions(), lookup)
            assert request.arguments.key == "A" and request.call_id == "manual-ws-call"
            if cancel:
                await session._cancel(handle)
                with pytest.raises(TurnStoppedError):
                    await (handle).get_result()
                with pytest.raises(InteractionExpiredError):
                    await request.respond("too late")
                assert len(server.requests) == 1
            else:
                server.respond(assistant_output("manual answer"))
                assert await request.respond("value:A") is None
                result = await (handle).get_result()
                assert result.final_output == "manual answer"
                assert tool_output(server.requests[-1], "manual-ws-call") == "value:A"
                with pytest.raises(InteractionExpiredError):
                    await request.respond("again")
                assert len(server.requests) == 2
        assert not server.http_requests


async def test_websocket_continuation_stream_tools_usage_and_history() -> None:
    effects = []

    @function_tool
    async def lookup(value: str) -> str:
        """Read one application value."""
        effects.append(value)
        return f"actual-result:{value}"

    async with timeout(20), peer() as provider:
        provider.respond(*text_response("msg_first", "first-", "answer"))
        provider.respond(*text_response("msg_second", "second-", "answer"))
        provider.respond(function_call("lookup", {"value": "record"}, "call_ws_tool"))
        provider.respond(*text_response("msg_tool", "tool-", "answer"))
        agent = Agent(
            instructions="Use lookup when asked.",
            model="gpt-5.1-codex",
            tools=[lookup],
            provider=provider.provider(),
        )
        async with agent.session() as session:
            for index, (prompt, answer, total) in enumerate(
                [
                    ("first", "first-answer", 14),
                    ("second", "second-answer", 28),
                    ("tool", "tool-answer", 56),
                ]
            ):
                handle = await session.submit(prompt)
                result = await (handle).get_result()
                events = [event async for event in handle.events()]
                assert result.final_output == answer
                assert (
                    "".join(event.delta for event in events if isinstance(event, OutputTextDelta))
                    == answer
                )
                assert isinstance(events[-1], TurnFinished)
                assert events[-1].status == "completed"
                assert result.thread_usage is not None
                assert result.thread_usage.total_token_usage.total_tokens == total
                assert result.thread_usage.last_token_usage.total_tokens == 14
                assert (
                    message_text(provider.requests[-1]["input"], "user")
                    == ["first", "second", "tool"][: index + 1]
                )
            tool_events = [event for event in events if isinstance(event, ToolFinished)]
            assert len(tool_events) == 1 and tool_events[0].status == "completed"
            assert effects == ["record"]
            assert tool_output(provider.requests[-1], "call_ws_tool") == "actual-result:record"
            state = await session.save_state()
            persisted = [row["payload"] for row in state.history if row["type"] == "response_item"]
            assert [
                item["type"] for item in persisted if item.get("call_id") == "call_ws_tool"
            ] == ["function_call", "function_call_output"]
            assert message_text(persisted, "assistant") == [
                "first-answer",
                "second-answer",
                "tool-answer",
            ]
        assert provider.connections == 1 and provider.warmups == 1
        assert len(provider.requests) == 4 and not provider.replies
        assert not provider.http_requests
        assert any(frame.get("previous_response_id") for frame in provider.frames)
        assert {header["authorization"] for header in provider.upgrades} == {
            "Bearer synthetic-ws-key"
        }


async def test_websocket_upgrade_required_falls_back_once_and_stays_http() -> None:
    async with timeout(20), peer(upgrade_status=426) as provider:
        provider.respond(assistant_output("first"))
        provider.respond(assistant_output("second"))
        async with Agent(instructions="Answer.", provider=provider.provider()).session() as session:
            for text in ["first", "second"]:
                handle = await session.submit(text)
                assert (await (handle).get_result()).final_output == text
        assert len(provider.upgrades) == 1
        assert provider.connections == 0
        assert len(provider.http_requests) == len(provider.requests) == 2
        assert message_text(provider.http_requests[1]["input"], "user") == ["first", "second"]
        assert all(
            header["authorization"] == "Bearer synthetic-ws-key"
            for header in provider.request_headers
        )


async def test_websocket_unauthorized_fails_without_anonymous_fallback() -> None:
    async with timeout(20), peer(upgrade_status=401) as provider:
        agent = Agent(
            instructions="Answer.", provider=provider.provider(key="synthetic-rejected-key")
        )
        async with agent.session() as session:
            handle = await session.submit("must fail")
            with pytest.raises(NativeError, match="401"):
                await (handle).get_result()
        assert provider.upgrades
        assert all(
            header.get("authorization") == "Bearer synthetic-rejected-key"
            for header in [*provider.upgrades, *provider.request_headers]
        )
        # Rejection must never remove credentials.
        assert not provider.requests and provider.connections == 0


@pytest.mark.parametrize("close", [False, True], ids=["cancel", "close"])
async def test_websocket_stop_joins_held_callback_without_effect_replay(close: bool) -> None:
    entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleaned = asyncio.Event()
    effects = []

    @function_tool
    async def held() -> str:
        """Hold one admitted application operation until cancellation."""
        effects.append("accepted-effect")
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()
            cleaned.set()
        return "late-value-must-not-be-submitted"

    async with timeout(20), peer() as provider:
        provider.respond(function_call("held", {}, "call_held_ws"))
        agent = Agent(
            instructions="Use held when asked.", tools=[held], provider=provider.provider()
        )
        async with agent.session() as session:
            handle = await session.submit("hold")
            await entered.wait()
            stopping = asyncio.create_task(session._aclose() if close else session._cancel(handle))
            try:
                await cleanup_started.wait()
                assert not stopping.done()
                assert effects == ["accepted-effect"] and not cleaned.is_set()
            finally:
                release_cleanup.set()
                await stopping
            with pytest.raises(TurnStoppedError):
                await (handle).get_result()
            assert cleaned.is_set() and effects == ["accepted-effect"]
            assert len(provider.requests) == 1
            if not close:
                provider.respond(assistant_output("recovered"))
                recovered = await session.submit("continue without held")
                assert (await (recovered).get_result()).final_output == "recovered"
                assert len(provider.requests) == 2
                assert "late-value-must-not-be-submitted" not in json.dumps(provider.requests)
                assert effects == ["accepted-effect"]
        assert not provider.http_requests


@dataclass
class Application:
    value: str
    calls: list[str] = field(default_factory=list)


def lookup_tool():
    @function_tool
    async def lookup(context: ToolContext[Application]) -> str:
        """Read the current application's value."""
        context.context.calls.append(context.call_id)
        return context.context.value

    return lookup


@pytest.mark.parametrize("source_ws", [False, True], ids=["http-to-ws", "ws-to-http"])
async def test_checkpoint_changes_transport_without_rewriting_history_or_current_authority(
    source_ws: bool,
) -> None:
    instructions = "Use lookup for the current application value."
    original = Application("original-value")
    current = Application("current-value")
    async with timeout(20), peer() as source, peer() as destination:
        source.respond(function_call("lookup", {}, "call_source"))
        source.respond(assistant_output("original-answer"))
        agent = Agent(
            instructions=instructions,
            tools=[lookup_tool()],
            provider=source.provider(websocket=source_ws, key="synthetic-source-key"),
        )
        async with agent.session(context=original) as session:
            handle = await session.submit("original-input")
            assert (await (handle).get_result()).final_output == "original-answer"
            saved = (await session.save_state()).to_json()
            items = [entry async for entry in session.items()]
        destination.respond(function_call("lookup", {}, "call_current"))
        destination.respond(assistant_output("current-answer"))
        restored_agent = Agent(
            instructions=instructions,
            tools=[lookup_tool()],
            provider=destination.provider(websocket=not source_ws, key="synthetic-current-key"),
        )
        async with restored_agent.session(
            state=SessionState.from_json(saved), context=current
        ) as restored:
            assert [entry async for entry in restored.items()] == items
            handle = await restored.submit("current-input")
            result = await (handle).get_result()
            assert result.final_output == "current-answer"
            assert (
                result.thread_usage is not None
                and result.thread_usage.total_token_usage.total_tokens == 56
            )
        assert original.calls == ["call_source"] and current.calls == ["call_current"]
        assert len(source.requests) == len(destination.requests) == 2
        assert message_text(destination.requests[0]["input"], "user") == [
            "original-input",
            "current-input",
        ]
        assert tool_output(destination.requests[1], "call_current") == "current-value"
        assert all(
            header["authorization"] == "Bearer synthetic-current-key"
            for header in destination.request_headers
        )
        assert bool(source.upgrades) is source_ws
        assert bool(destination.upgrades) is not source_ws
        assert len(source.http_requests) == (0 if source_ws else 2)
        assert len(destination.http_requests) == (2 if source_ws else 0)


async def test_websocket_session_compaction_keeps_native_summary_and_continues() -> None:
    async with timeout(20), peer() as provider:
        provider.respond(assistant_output("remembered"))
        provider.respond(assistant_output("Native summary: the label is atlas."))
        provider.respond(assistant_output("atlas"))
        agent = Agent(instructions="Remember atlas.", model="gpt-5.5", provider=provider.provider())
        async with agent.session() as session:
            stream = session.events()
            handle = await session.submit("Remember atlas.")
            assert (await (handle).get_result()).final_output == "remembered"
            await session.compact()
            state = await session.save_state()
            assert any(row["type"] == "compacted" for row in state.history)
            handle = await session.submit("Recall the label.")
            result = await (handle).get_result()
            assert result.final_output == "atlas"
            assert (
                result.thread_usage is not None
                and result.thread_usage.last_token_usage.total_tokens == 14
            )
        events = [event async for event in stream]
        assert len([event for event in events if isinstance(event, ContextCompacted)]) == 1
        assert len(provider.requests) == 3
        assert "Native summary: the label is atlas." in json.dumps(provider.requests[-1]["input"])
        assert provider.connections >= 1


@pytest.mark.parametrize("revocation", ["malformed-item", "non-assistant", "mismatched-id"])
async def test_websocket_transient_observations_do_not_change_accepted_output_or_history(
    revocation: str,
) -> None:
    added, delta, _ = text_response("first-id", "authorized-first")
    fresh, fresh_delta, done = text_response("fresh-id", "authorized-second")
    done["item"]["content"][0]["text"] = "accepted-answer"
    if revocation == "malformed-item":
        reset = {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "broken", "role": "assistant"},
        }
    elif revocation == "non-assistant":
        reset = {
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "other-id", "role": "user", "content": []},
        }
    else:
        reset = {
            "type": "response.output_text.delta",
            "item_id": "other-id",
            "delta": "untrusted-text",
        }
    async with timeout(20), peer() as provider:
        stale = {
            "type": "response.output_text.delta",
            "item_id": "first-id" if revocation != "mismatched-id" else "other-id",
            "delta": "untrusted-text",
        }
        if revocation == "malformed-item":
            provider.expected_disconnect_event = reset
        provider.respond(added, delta, reset, stale, fresh, fresh_delta, done)
        provider.respond(assistant_output("follow-up"))
        async with Agent(instructions="Answer.", provider=provider.provider()).session() as session:
            first = await session.submit("first")
            if revocation == "malformed-item":
                with pytest.raises(
                    NativeError,
                    match="^Native model response ended without complete supported output$",
                ):
                    await first.get_result()
            else:
                assert (await first.get_result()).final_output == "accepted-answer"
            assert len(provider.requests) == 1
            deltas = [
                event.delta async for event in first.events() if isinstance(event, OutputTextDelta)
            ]
            # Core's observations may include transient provider text. Valid
            # chunks remain visible; only settled output and history are final.
            assert deltas[0] == "authorized-first"
            if revocation == "malformed-item":
                assert deltas == ["authorized-first"]
            else:
                assert deltas[-1] == "authorized-second"
            stored_history = json.dumps((await session.save_state()).history)
            assert ("accepted-answer" in stored_history) is (revocation != "malformed-item")
            item_history = json.dumps([item async for item in session.items()])
            assert ("accepted-answer" in item_history) is (revocation != "malformed-item")
            assert "broken" not in stored_history and "broken" not in item_history
            assert "untrusted-text" not in item_history
            assert "authorized-first" not in item_history
            assert "authorized-second" not in item_history
            assert "untrusted-text" not in stored_history
            assert "authorized-first" not in stored_history
            assert "authorized-second" not in stored_history
            second = await session.submit("second")
            result = await (second).get_result()
            assert result.final_output == "follow-up"
            assert result.thread_usage is not None
            assert result.thread_usage.last_token_usage.total_tokens == 14
            assert result.thread_usage.total_token_usage.total_tokens == (
                14 if revocation == "malformed-item" else 28
            )
            assert len(provider.requests) == 2
        history = json.dumps(provider.requests[1]["input"])
        assert ("accepted-answer" in history) is (revocation != "malformed-item")
        assert "broken" not in history
        assert "untrusted-text" not in history
        assert "authorized-first" not in history and "authorized-second" not in history
        assert provider.connections == (2 if revocation == "malformed-item" else 1)
        assert not provider.http_requests


async def test_websocket_cancelled_session_does_not_retire_shared_provider_sibling() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold(event: dict[str, Any]) -> None:
        if event.get("item", {}).get("id") == "held-id":
            entered.set()
            await release.wait()
            raise asyncio.CancelledError

    async with timeout(20), peer() as provider:
        provider.before_event = hold
        provider.respond(*text_response("held-id", "cancelled-answer"))
        provider.respond(assistant_output("sibling-first"))
        provider.respond(assistant_output("sibling-second"))
        agent = Agent(instructions="Answer.", provider=provider.provider())
        async with agent.session() as held, agent.session() as sibling:
            first = await held.submit("hold")
            await entered.wait()
            try:
                other = await sibling.submit("sibling first")
                assert (await (other).get_result()).final_output == "sibling-first"
                await held._cancel(first)
                with pytest.raises(TurnStoppedError):
                    await (first).get_result()
                await held._aclose()
                other = await sibling.submit("sibling second")
                assert (await (other).get_result()).final_output == "sibling-second"
                assert provider.connections == 2
                assert "hold" not in message_text(provider.requests[-1]["input"], "user")
            finally:
                # End the synthetic server hold after observing local cancellation.
                release.set()
        assert not provider.http_requests


async def test_websocket_malformed_terminal_retires_socket_before_next_request() -> None:
    async with timeout(20), peer() as provider:
        # The fixture follows this malformed terminal with its ordinary valid
        # empty completion. That unread frame must never satisfy a later request.
        provider.respond({"type": "response.completed", "response": {"id": 17}})
        provider.respond(assistant_output("fresh-connection-answer"))
        async with Agent(instructions="Answer.", provider=provider.provider()).session() as session:
            first = await session.submit("first")
            try:
                result = await (first).get_result()
            except NativeError:
                # If the official parser terminates this turn, recovery still
                # has to retire its socket before a later public submission.
                recovered = await session.submit("recover")
                result = await (recovered).get_result()
            assert result.final_output == "fresh-connection-answer"
            assert len(provider.requests) == 2
            assert provider.connections == 2
            assert not provider.http_requests


@pytest.mark.parametrize("ca_variable", ["SSL_CERT_FILE", "CODEX_CA_CERTIFICATE"])
async def test_plain_websocket_rechecks_custom_ca_before_fresh_connections(
    ca_variable: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    for variable in ("SSL_CERT_FILE", "SSL_CERT_DIR", "CODEX_CA_CERTIFICATE"):
        monkeypatch.delenv(variable, raising=False)
    async with timeout(30), peer() as server:
        shared = NativeProvider(
            base_url=server.url,
            api_key="synthetic-ws-key",
            supports_websockets=True,
            request_max_retries=0,
            stream_max_retries=0,
        )
        server.providers.append(shared)
        agent = Agent(
            instructions="Keep plain transport and fresh custom trust validation.", provider=shared
        )
        server.respond(assistant_output("default trust"))
        first = await agent.run("default trust input")
        assert first.final_output == "default trust"
        connections_before_error = server.connections
        upgrades_before_error = len(server.upgrades)
        frames_before_error = len(server.frames)
        monkeypatch.setenv(ca_variable, str(tmp_path / "missing-custom-ca.pem"))
        server.respond(assistant_output("HTTP fallback"))
        fallback = await agent.run("custom CA validation must precede a plain connection")
        assert fallback.final_output == "HTTP fallback"
        assert len(server.requests) == 2
        assert len(server.http_requests) == 1
        assert server.connections == connections_before_error
        assert len(server.upgrades) == upgrades_before_error
        assert len(server.frames) == frames_before_error
        monkeypatch.delenv(ca_variable)
        server.respond(assistant_output("fresh recovery"))
        recovered = await agent.run("fresh recovery input")
        assert recovered.final_output == "fresh recovery"
        assert (
            len({first.turn.session_id, fallback.turn.session_id, recovered.turn.session_id}) == 3
        )
        assert len(server.requests) == 3
        assert len(server.http_requests) == 1
        assert server.connections == connections_before_error + 1
        assert len(server.upgrades) == upgrades_before_error + 1
        await server.disconnected.wait()
        assert not server.sockets and server.handlers == 0


@pytest.mark.serial
@pytest.mark.parametrize("trust_proxy", [True, False])
async def test_websocket_provider_verifies_https_proxy_including_http_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, trust_proxy: bool
) -> None:
    from urllib.parse import urlsplit

    from tests.test_sdk_session_tls import TLS_FIXTURES

    for name in ("CODEX_CA_CERTIFICATE", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        monkeypatch.delenv(name, raising=False)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    for name in ("server-A.pem", "server-A.key"):
        (tmp_path / name).write_text(TLS_FIXTURES[name])
    context.load_cert_chain(tmp_path / "server-A.pem", tmp_path / "server-A.key")
    if trust_proxy:
        ca = tmp_path / "ca-A.pem"
        ca.write_text(TLS_FIXTURES[ca.name])
        monkeypatch.setenv("SSL_CERT_FILE", str(ca))
    prefixes: list[bytes] = []
    handshake_failures: list[str] = []
    handshake_successes: list[bool] = []
    accepted_data: list[bytes] = []
    loop = asyncio.get_running_loop()
    attempt_observed = asyncio.Event()

    class Proxy(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.settimeout(5)
            # Peek without consuming ClientHello before the real TLS handshake.
            prefix = self.request.recv(1, socket.MSG_PEEK)
            prefixes.append(prefix)
            if prefix != b"\x16":
                loop.call_soon_threadsafe(attempt_observed.set)
                return
            try:
                secured = context.wrap_socket(self.request, server_side=True)
            except OSError as error:
                handshake_failures.append(
                    str(getattr(error, "reason", None) or type(error).__name__)
                )
                loop.call_soon_threadsafe(attempt_observed.set)
                return
            handshake_successes.append(True)
            with secured:
                try:
                    data = b""
                    while b"\r\n" not in data and len(data) < 1024:
                        chunk = secured.recv(min(256, 1024 - len(data)))
                        if not chunk:
                            break
                        data += chunk
                    accepted_data.append(data.split(b"\r\n", 1)[0])
                except OSError:
                    pass
                finally:
                    loop.call_soon_threadsafe(attempt_observed.set)

    proxy = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Proxy)
    serving = asyncio.create_task(asyncio.to_thread(proxy.serve_forever, poll_interval=0.01))
    proxy_url = f"https://127.0.0.1:{proxy.server_address[1]}"
    for name in (
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.setenv(name, proxy_url)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "")
    monkeypatch.delenv("CODEX_SANDBOX", raising=False)
    try:
        async with timeout(30), peer() as destination:
            destination.respond(assistant_output("unexpected proxy bypass"))
            provider = NativeProvider(
                base_url=destination.url,
                api_key="synthetic-proxy-key",
                supports_websockets=True,
                request_max_retries=0,
                stream_max_retries=0,
            )
            request = asyncio.create_task(
                Agent(instructions="Keep proxy TLS verification.", provider=provider).run(
                    "Do not send a request through an untrusted proxy."
                )
            )
            try:
                # Observe the handshake before stopping Core's connection-retry loop.
                await attempt_observed.wait()
            finally:
                request.cancel()
                try:
                    with pytest.raises((asyncio.CancelledError, NativeError)):
                        await request
                finally:
                    await provider.aclose()
            assert not destination.requests
            assert not destination.http_requests
            assert not destination.upgrades
    finally:
        # shutdown joins serve_forever; server_close joins every request thread.
        try:
            await asyncio.to_thread(proxy.shutdown)
            await serving
        finally:
            await asyncio.to_thread(proxy.server_close)
    assert any(prefix == b"\x16" for prefix in prefixes)
    assert all(prefix in (b"", b"\x16") for prefix in prefixes), "plaintext reached the HTTPS proxy"
    if trust_proxy:
        assert handshake_successes, "the trusted proxy never completed TLS negotiation"
        # Proxy policy can select WebSocket CONNECT or the existing HTTP fallback.
        # Both routes must verify proxy TLS before sending application data.
        authority = urlsplit(destination.url).netloc
        expected = {
            f"CONNECT {authority} HTTP/1.1".encode(),
            f"POST {destination.url}/responses HTTP/1.1".encode(),
        }
        assert accepted_data and all(line in expected for line in accepted_data), accepted_data
    else:
        # Platform verifiers can close the stream without sending an unknown-CA alert.
        assert handshake_failures
        assert all(
            reason in {"TLSV1_ALERT_UNKNOWN_CA", "UNEXPECTED_EOF_WHILE_READING"}
            for reason in handshake_failures
        ), handshake_failures
        assert not handshake_successes, "the untrusted proxy completed TLS negotiation"
        assert not accepted_data, "an untrusted proxy accepted CONNECT or application data"
