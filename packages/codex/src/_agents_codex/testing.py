"""Scripted HTTP responses for testing the real native agent loop.

Import from `agents.testing`. Each sequence is one model response::

    async with ScriptedResponses(
        [tool_call("lookup", {"key": "example"}, call_id="lookup-1")],
        [text("Found it.")],
    ) as peer:
        agent = Agent(instructions="Look up the key.", tools=[lookup], provider=peer.provider)
        result = await agent.run("Find example.")
        assert result.final_output == "Found it."
        assert peer.tool_output("lookup-1") == "the real function result"

The peer records request bodies for explicit inspection, so use synthetic test
data. It does not record HTTP headers. Close all Sessions before leaving the
peer context. An ordinary successful exit also checks for exact exhaustion;
exceptions from the test or native loop are preserved.
"""

from __future__ import annotations

import asyncio as asyncio
import json as json
import re as re
import socket as socket
from collections import deque as deque
from collections.abc import Callable as Callable, Mapping as Mapping, Sequence as Sequence
from contextlib import AbstractAsyncContextManager as AbstractAsyncContextManager
from copy import deepcopy as deepcopy
from dataclasses import dataclass as dataclass, field as field
from functools import partial as partial
from http.server import (
    BaseHTTPRequestHandler as BaseHTTPRequestHandler,
    ThreadingHTTPServer as ThreadingHTTPServer,
)
from socketserver import TCPServer as TCPServer
from threading import Lock as Lock, Thread as Thread
from types import TracebackType as TracebackType
from typing import cast as cast
from uuid import uuid4 as uuid4

from agents.config import JsonValue as JsonValue

from ._lifecycle import _close_after_error as _close_after_error, _join as _join
from ._public import preserve_module_identity as _preserve_module_identity
from .agent import NativeProvider as NativeProvider


@dataclass(frozen=True)
class _ScriptItem:
    encoded: str


def event(value: Mapping[str, JsonValue]) -> _ScriptItem:
    """Emit an arbitrary Responses SSE event, including malformed-wire fixtures."""
    return _ScriptItem(json.dumps(dict(value), allow_nan=False))


def text(value: str, *, item_id: str | None = None) -> _ScriptItem:
    """Emit one assistant text item."""
    return _ScriptItem(
        json.dumps(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "id": item_id if item_id is not None else f"msg_{uuid4().hex}",
                    "content": [{"type": "output_text", "text": value}],
                },
            }
        )
    )


def tool_call(
    name: str,
    arguments: Mapping[str, JsonValue],
    *,
    call_id: str,
    namespace: str | None = None,
) -> _ScriptItem:
    """Ask the native loop to invoke a declared tool with these arguments.

    namespace is separate from the short tool name. Omit it to keep the bare form.
    """
    return _ScriptItem(
        json.dumps(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    **({"namespace": namespace} if namespace is not None else {}),
                    "arguments": json.dumps(dict(arguments)),
                },
            }
        )
    )


@dataclass(frozen=True)
class ScriptedResponse:
    """One response with optional provider usage and HTTP metadata.

    Omitted usage stays unknown. Content type and framing remain SDK-owned.
    ScriptedResponses captures items, usage and headers before starting HTTP.
    """

    items: Sequence[_ScriptItem]
    usage: Mapping[str, JsonValue] | None = field(default=None, kw_only=True)
    headers: Mapping[str, str] | None = field(default=None, kw_only=True)
    response_id: str | None = field(default=None, kw_only=True)
    status: int = field(default=200, kw_only=True)
    error_body: Mapping[str, JsonValue] | None = field(default=None, kw_only=True)


ScriptResult = Sequence[_ScriptItem] | ScriptedResponse
Script = ScriptResult | Callable[[Mapping[str, JsonValue]], ScriptResult]


class _LoopbackHTTPServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # Numeric loopback needs no reverse DNS lookup during local startup.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = cast(tuple[str, int], self.server_address)


class ScriptedResponses(AbstractAsyncContextManager["ScriptedResponses"]):
    """Serve a script from a loopback HTTP endpoint through an ordinary native provider.

    Configure and keep a direct connection or proxy bypass for 127.0.0.1
    throughout the peer and all its Sessions. The provider follows the process
    and operating system proxy configuration; this peer is not a network
    isolation boundary. Use synthetic prompts, scripts, and tool data only.

    Every sequence is one model response; items in it are delivered together.
    Tools execute in the real Session. The script does not emulate tool
    implementations, MCP, an executor, WebSockets, or model reasoning.
    The peer rejects unexpected requests and never selects a fallback provider.
    Normal context exit checks exhaustion: an unused script fails even if a
    proxy returned a response. A result before exit does not prove the peer
    served it; requests and tool_output provide readback.
    """

    def __init__(self, *responses: Script) -> None:
        self._responses = deque(self._capture_script(script) for script in responses)
        self._requests: list[dict[str, JsonValue]] = []
        self._unexpected = 0
        self._callback_error: Exception | None = None
        self._lock = Lock()
        self._server: ThreadingHTTPServer | None = None
        self._provider: NativeProvider | None = None

    @staticmethod
    def _capture_script(script: Script) -> Script:
        if callable(script):
            return script
        response = script if isinstance(script, ScriptedResponse) else ScriptedResponse(script)
        headers = dict(response.headers or {})
        for name, value in headers.items():
            if (
                not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name)
                or name.lower() in {"content-type", "content-length", "transfer-encoding"}
                or not isinstance(value, str)
                or "\r" in value
                or "\n" in value
            ):
                raise ValueError("Invalid scripted response headers")
            value.encode("latin-1")
        if (
            isinstance(response.status, bool)
            or not isinstance(response.status, int)
            or (response.status != 200 and not 400 <= response.status <= 599)
        ):
            raise ValueError("Scripted status must be 200 or an HTTP error status")
        if response.response_id is not None and not isinstance(response.response_id, str):
            raise TypeError("response_id must be a string or None")
        return ScriptedResponse(
            tuple(response.items),
            usage=None
            if response.usage is None
            else json.loads(json.dumps(dict(response.usage), allow_nan=False)),
            headers=headers,
            response_id=response.response_id,
            status=response.status,
            error_body=None
            if response.error_body is None
            else json.loads(json.dumps(dict(response.error_body), allow_nan=False)),
        )

    def append(self, *responses: Script) -> None:
        """Append responses for subsequent requests; dynamic callbacks may append."""
        captured = [self._capture_script(script) for script in responses]
        with self._lock:
            self._responses.extend(captured)

    def clear(self) -> None:
        """Discard queued responses without changing recorded request history."""
        with self._lock:
            self._responses.clear()

    async def __aenter__(self) -> ScriptedResponses:
        if self._server is not None:
            raise RuntimeError("ScriptedResponses can only be entered once")
        server = _LoopbackHTTPServer(("127.0.0.1", 0), partial(_Handler, self))
        # server_close() joins only non-daemon handlers, including canceled native requests.
        server.daemon_threads = False
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        try:
            thread.start()
        except BaseException:
            # No loop or admitted handlers exist; shutdown() would wait forever.
            server.server_close()
            raise
        self._server = server
        self._provider = NativeProvider(
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            api_key="synthetic-agents-testing-key",
            supports_websockets=False,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        assert self._server is not None
        server = self._server

        async def close() -> None:
            try:
                await self.provider.aclose()
            finally:
                try:
                    await asyncio.to_thread(server.shutdown)
                    await asyncio.to_thread(server.server_close)
                finally:
                    self._provider = None

        if exc is not None:
            await _close_after_error(exc, close)
            if self._callback_error is not None:
                raise self._callback_error from None
        else:
            await _join(asyncio.create_task(close()))
            self.assert_exhausted()

    @property
    def provider(self) -> NativeProvider:
        """Borrow this provider while the peer is open. Close Sessions first."""
        if self._provider is None:
            raise RuntimeError("provider is only available inside the ScriptedResponses context")
        return self._provider

    @property
    def requests(self) -> tuple[Mapping[str, JsonValue], ...]:
        """Return snapshots of request bodies, never HTTP headers.

        Request bodies may include prompts and tool results. Inspect them
        deliberately and use synthetic data in scripts and tests.
        """
        with self._lock:
            return tuple(deepcopy(self._requests))

    def tool_output(self, call_id: str) -> str | None:
        """Read the most recent submitted function result, or None if absent."""
        for request in reversed(self.requests):
            inputs = request.get("input")
            if not isinstance(inputs, list):
                continue
            for item in reversed(inputs):
                if (
                    isinstance(item, dict)
                    and item.get("type") == "function_call_output"
                    and item.get("call_id") == call_id
                ):
                    output = item.get("output")
                    if isinstance(output, str):
                        return output
                    if isinstance(output, list):
                        return "".join(
                            value
                            for part in output
                            if isinstance(part, dict) and isinstance(value := part.get("text"), str)
                        )
        return None

    def assert_exhausted(self) -> None:
        """Fail on unused responses or unexpected requests; omit body contents."""
        with self._lock:
            if self._callback_error is not None:
                raise self._callback_error
            if self._responses or self._unexpected:
                raise AssertionError(
                    f"ScriptedResponses: {len(self._responses)} unused response(s), "
                    f"{self._unexpected} unexpected request(s)"
                )


class _Handler(BaseHTTPRequestHandler):
    # Local scripts need no external model I/O. An abandoned read or write must
    # time out so server_close() can join every non-daemon request handler.
    timeout = 2.0

    def __init__(
        self,
        peer: ScriptedResponses,
        request: socket.socket,
        client_address: tuple[str, int],
        server: ThreadingHTTPServer,
    ) -> None:
        self.peer = peer
        super().__init__(request, client_address, server)

    def log_message(self, format: str, *args: object) -> None:
        # HTTP paths, headers and request data must not enter automatic logs.
        pass

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        with self.peer._lock:
            self.peer._unexpected += 1
        super().send_error(
            code,
            "Unexpected scripted request",
            "Only the scripted number of POST /v1/responses requests are accepted.",
        )

    def do_POST(self) -> None:
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        except (TypeError, ValueError):
            self.send_error(400)
            return
        if not isinstance(body, dict):
            self.send_error(400)
            return
        with self.peer._lock:
            self.peer._requests.append(body)
            response_id = f"resp_{len(self.peer._requests)}"
            items = (
                self.peer._responses.popleft()
                if self.path == "/v1/responses" and self.peer._responses
                else None
            )
        if items is None:
            # A 400 is terminal in the ordinary native retry policy.
            self.send_error(400)
            return
        try:
            if callable(items):
                items = self.peer._capture_script(items(deepcopy(body)))
            assert isinstance(items, ScriptedResponse)
        except Exception as error:
            with self.peer._lock:
                if self.peer._callback_error is None:
                    self.peer._callback_error = error
            self.send_error(500)
            return
        if items.status != 200:
            payload = json.dumps(
                dict(
                    {"error": {"message": "Scripted provider error", "type": "scripted_error"}}
                    if items.error_body is None
                    else items.error_body
                ),
                allow_nan=False,
            ).encode()
            self.send_response(items.status)
            for name, value in (items.headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        response: dict[str, object] = {
            "id": items.response_id if items.response_id is not None else response_id
        }
        created = json.dumps({"type": "response.created", "response": response})
        if items.usage is not None:
            response["usage"] = items.usage
        completed = json.dumps({"type": "response.completed", "response": response})
        events = [created, *(item.encoded for item in items.items), completed]
        payload = "".join(f"data: {event}\n\n" for event in events).encode()
        self.send_response(200)
        for name, value in (items.headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


_preserve_module_identity(globals(), "agents.testing")
