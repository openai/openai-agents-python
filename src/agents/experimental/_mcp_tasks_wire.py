"""The 2026 Tasks extension for the qualified MCP Python client.

Each HTTP response may deliver up to 256 KiB of identity-encoded body data, including
SSE framing and notifications. A reply above that limit cannot establish a write receipt.
"""

# The base SDK can be type-checked without its optional protocol packages. The provider's
# focused checks also run with the pinned packages and their full type information.
# mypy: disable-error-code=import-not-found, disallow_subclassing_any=False
# pyright: reportMissingImports=false
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from contextvars import Context, ContextVar, copy_context
from typing import Annotated, Any, Literal, NoReturn, TypeVar

import anyio
import httpx2
from mcp.client._probe import negotiate_auto
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.dispatcher import CallOptions
from mcp.shared.inbound import (
    MCP_METHOD_HEADER,
    find_invalid_x_mcp_header,
    mcp_param_headers,
    x_mcp_header_map,
)
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp_types import (
    CallToolRequest,
    CallToolRequestParams,
    CallToolResult,
    ElicitResult,
    EmptyResult,
    InputRequests,
    ListToolsRequest,
    ListToolsResult,
    PaginatedRequestParams,
    Request,
    RequestParams,
    Result,
)
from mcp_types.jsonrpc import RequestId
from pydantic import BeforeValidator, Field, TypeAdapter

from .._lifecycle import _join

VERSION = "2026-07-28"
EXTENSION = "io.modelcontextprotocol/tasks"
_MAX_RESPONSE_BYTES = 256 * 1024
_MAX_TOOL_LIST_PAGES = 32
T = TypeVar("T")


def _task_integer(value: object) -> int:
    # JSON Schema integers include numbers with a zero fractional part.
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if type(value) is not int or abs(value) > 2**53 - 1:
        raise ValueError("Expected a safe JSON integer")
    return value


_TaskInteger = Annotated[int, BeforeValidator(_task_integer)]


class _Task(Result):
    task_id: str = Field(repr=False, min_length=1)
    status: Literal["working", "input_required", "completed", "cancelled", "failed"]
    created_at: str
    last_updated_at: str
    ttl_ms: _TaskInteger | None
    poll_interval_ms: _TaskInteger = Field(default=0, ge=0)


class Created(_Task):
    result_type: Literal["task"]


_CALL_RESULT: TypeAdapter[Created | CallToolResult] = TypeAdapter(Created | CallToolResult)


class Detail(_Task):
    result_type: Literal["complete"] = "complete"
    result: CallToolResult | None = Field(default=None, repr=False)
    input_requests: InputRequests | None = Field(default=None, repr=False)


class TaskParams(RequestParams):
    task_id: str = Field(repr=False)


class GetOrCancel(Request[TaskParams, Literal["tasks/get", "tasks/cancel"]]):
    name_param = "taskId"


class UpdateParams(TaskParams):
    input_responses: dict[str, ElicitResult] = Field(repr=False)


class Update(Request[UpdateParams, Literal["tasks/update"]]):
    method: Literal["tasks/update"] = "tasks/update"
    name_param = "taskId"


class Acknowledged(EmptyResult):
    result_type: Literal["complete"] = "complete"


class _TaskDispatcher(JSONRPCDispatcher):
    async def send_raw_request(
        self,
        method: str,
        params: Mapping[str, Any] | None,
        opts: CallOptions | None = None,
        *,
        _related_request_id: RequestId | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = await super().send_raw_request(
            method, params, opts, _related_request_id=_related_request_id
        )
        # The client floors negative cache TTLs before validating the typed result.
        # Check the peer's original Task TTL before that can turn a fraction into zero.
        if method == "tasks/get" or (method == "tools/call" and result.get("resultType") == "task"):
            ttl = result.get("ttlMs")
            if ttl is not None:
                _task_integer(ttl)
        return result


class Unsupported(PermissionError):
    """The selected peer or operation is outside this adapter's supported profile."""


class Unavailable(Exception):
    """The operation returned no usable receipt."""


_private_wire = ContextVar("agents_mcp_tasks_private_wire", default=False)
_EGRESS_ERROR = "Invalid MCP HTTPS transport configuration. Check the host proxy and CA settings."


def _private_diagnostics(record: logging.LogRecord) -> bool:
    # MCP logs serialized frames and HTTP core logs response headers at DEBUG. The filter
    # applies only to this operation and its transport tasks, including malformed messages.
    return not _private_wire.get()


def _check_ca_directories() -> None:
    # OpenSSL looks up hashed CA files lazily and accepts a list of trust directories.
    directories = os.environ.get("SSL_CERT_DIR")
    if directories and not os.environ.get("SSL_CERT_FILE"):
        if not any(os.path.isdir(path) for path in directories.split(os.pathsep)):
            raise PermissionError(_EGRESS_ERROR)


def check_https_transport() -> None:
    # Validate the same host settings before a deferred journal can claim a creation.
    # A synchronous client can be closed here without opening any connection.
    try:
        _check_ca_directories()
        with httpx2.Client(trust_env=True):
            return
    except Exception:
        pass
    raise PermissionError(_EGRESS_ERROR)


class _BoundedStream(httpx2.AsyncByteStream):
    def __init__(self, stream: httpx2.AsyncByteStream, reject: Callable[[], NoReturn]) -> None:
        self._stream = stream
        self._reject = reject

    async def __aiter__(self) -> AsyncIterator[bytes]:
        remaining = _MAX_RESPONSE_BYTES
        source = self._stream.__aiter__()
        try:
            async for chunk in source:
                remaining -= len(chunk)
                if remaining < 0:
                    self._reject()
                yield chunk
        finally:
            if isinstance(source, AsyncGenerator):
                await source.aclose()

    async def aclose(self) -> None:
        await self._stream.aclose()


class _Workers:
    """Bound network threads and outstanding system lookups across caller loops."""

    def __init__(self) -> None:
        self.available = 4
        self.lock = threading.Lock()
        self.waiters: dict[Future[None], None] = {}

    async def acquire(self) -> None:
        while True:
            with self.lock:
                if self.available:
                    self.available -= 1
                    return
                waiter: Future[None] = Future()
                self.waiters[waiter] = None
            try:
                await asyncio.wrap_future(waiter)
            except asyncio.CancelledError:
                with self.lock:
                    self.waiters.pop(waiter, None)
                raise

    def release(self) -> None:
        with self.lock:
            self.available += 1
            waiters, self.waiters = self.waiters, {}
        # Only a running caller can claim capacity. A closed caller can discard its wakeup.
        for waiter in waiters:
            if waiter.set_running_or_notify_cancel():
                waiter.set_result(None)


_workers = _Workers()


def _after_fork() -> None:
    global _workers
    _workers = _Workers()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


async def exchange(
    endpoint: str,
    authorization: Callable[[], Awaitable[str]],
    timeout_seconds: float,
    operation: Callable[[ClientSession], Awaitable[T]],
    reserve: Callable[[], Awaitable[bool]] | None = None,
) -> T:
    deadline = time.monotonic() + timeout_seconds
    # Asyncio can log a finished task's result. Keep the accepted reply in this call.
    received: list[T] = []
    transport = asyncio.create_task(
        _run_exchange(endpoint, authorization, deadline, operation, reserve, received)
    )
    try:
        try:
            # The private loop settles a timeout. Caller cancellation interrupts it immediately.
            await asyncio.wait((transport,), timeout=deadline - time.monotonic())
        except asyncio.CancelledError:
            transport.cancel()
            raise
        finally:
            with anyio.CancelScope(shield=True), suppress(asyncio.CancelledError):
                await _join(asyncio.gather(transport, return_exceptions=True))
        transport.result()
    except (PermissionError, Unavailable) as error:
        # Only the classified wire error crosses into the caller's diagnostics.
        public_error: BaseException = type(error)(*error.args)
    except asyncio.CancelledError as interruption:
        public_error = interruption.with_traceback(None)
    else:
        return received[0]
    finally:
        del transport, endpoint, received
    raise public_error


async def _run_exchange(
    endpoint: str,
    authorization: Callable[[], Awaitable[str]],
    deadline: float,
    operation: Callable[[ClientSession], Awaitable[T]],
    reserve: Callable[[], Awaitable[bool]] | None,
    received: list[T],
) -> None:
    owner_loop = asyncio.get_running_loop()
    finished = owner_loop.create_future()
    admissions: asyncio.Queue[Callable[[], None] | None] = asyncio.Queue()
    finished.add_done_callback(lambda _: admissions.put_nowait(None))
    stop: Future[None] = Future()
    task: asyncio.Task[None] | None = None
    workers = _workers
    async with anyio.create_task_group() as host_tasks:

        async def on_owner(callback: Callable[[], Awaitable[None]]) -> None:
            acknowledged: Future[None] = Future()

            async def invoke() -> None:
                # Cancel this task once so an ordinary asyncio rollback can finish.
                with anyio.CancelScope(shield=True):
                    host_task = asyncio.current_task()
                    assert host_task is not None

                    def stop_callback(done: Future[None]) -> None:
                        if done.cancelled():
                            with suppress(RuntimeError):
                                owner_loop.call_soon_threadsafe(host_task.cancel)

                    acknowledged.add_done_callback(stop_callback)
                    try:
                        if not acknowledged.cancelled():
                            await callback()
                    finally:
                        if acknowledged.set_running_or_notify_cancel():
                            acknowledged.set_result(None)

            def schedule() -> None:
                if not acknowledged.cancelled():
                    host_tasks.start_soon(invoke)

            # Posting a handle must leave the network loop free to enforce its deadline.
            try:
                owner_loop.call_soon_threadsafe(admissions.put_nowait, schedule)
                await asyncio.wrap_future(acknowledged)
            finally:
                acknowledged.cancel()

        def run() -> None:
            nonlocal task
            loop = None
            resolver = None
            try:
                loop = asyncio.new_event_loop()
                loop.set_debug(False)
                resolver = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mcp-dns")
                loop.set_default_executor(resolver)
                if time.monotonic() < deadline and not stop.cancelled():
                    task = loop.create_task(
                        _exchange(
                            endpoint,
                            authorization,
                            deadline,
                            operation,
                            reserve,
                            on_owner,
                            received,
                        )
                    )
                    cancel = task.cancel

                    def interrupt(_: Future[None]) -> None:
                        with suppress(RuntimeError):
                            loop.call_soon_threadsafe(cancel)

                    stop.add_done_callback(interrupt)
                    loop.run_until_complete(asyncio.wait({task}))
                    loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            finally:
                if loop is not None:
                    with suppress(Exception):
                        loop.close()
                with suppress(RuntimeError):
                    owner_loop.call_soon_threadsafe(finished.set_result, None)
                # A system lookup cannot be interrupted. Keep its slot until it exits.
                try:
                    if resolver is not None:
                        resolver.shutdown(wait=True)
                finally:
                    workers.release()

        with anyio.move_on_after(deadline - time.monotonic()) as waiting:
            await workers.acquire()
        if not waiting.cancelled_caught:
            # Keep host bindings on the owner loop. DNS cleanup can outlive the exchange.
            thread = threading.Thread(target=Context().run, args=(run,), name="mcp-tasks")
            try:
                thread.start()
            except RuntimeError:
                workers.release()
            else:
                try:
                    # AnyIO admits children from an async task in the host's context.
                    while (admit := await admissions.get()) is not None:
                        copy_context().run(admit)
                except asyncio.CancelledError:
                    stop.cancel()
                    # Wait for protocol cleanup. The host task group joins its callbacks.
                    with anyio.CancelScope(shield=True), suppress(asyncio.CancelledError):
                        await _join(finished)
                    if task is not None and task.done() and not task.cancelled():
                        task.exception()
                    raise
    if task is None or not task.done():
        raise Unavailable("MCP operation did not return a usable receipt")
    task.result()


async def _exchange(
    endpoint: str,
    authorization: Callable[[], Awaitable[str]],
    expires_at: float,
    operation: Callable[[ClientSession], Awaitable[T]],
    reserve: Callable[[], Awaitable[bool]] | None,
    on_owner: Callable[[Callable[[], Awaitable[None]]], Awaitable[None]],
    received: list[T],
) -> None:
    denied = False

    async def authenticate(request: httpx2.Request) -> None:
        nonlocal denied
        if request.url != target or any(h.startswith("mcp-param-") for h in request.headers):
            denied = True
            raise PermissionError("MCP request routing denied")
        granted = False

        async def grant() -> None:
            nonlocal granted
            try:
                token = await authorization()
                if (
                    time.monotonic() >= expires_at
                    or not isinstance(token, str)
                    or not token
                    or not token.isascii()
                    or any(ord(c) <= 32 or ord(c) == 127 for c in token)
                ):
                    return
                request.headers["Accept-Encoding"] = "identity"
                request.headers["Authorization"] = "Bearer " + token
                del token
                # Only a validated mutating POST spends a key, including on a retry.
                if request.method == "POST" and request.headers.get(MCP_METHOD_HEADER) in {
                    "tasks/update",
                    "tasks/cancel",
                }:
                    if reserve is None or await reserve() is not True:
                        return
                granted = True
            except Exception:
                pass

        await on_owner(grant)
        if time.monotonic() >= expires_at:
            raise Unavailable
        if granted:
            return
        denied = True
        raise PermissionError("MCP request authorization denied")

    def reject_body() -> NoReturn:
        # MCP can resume an interrupted SSE body. End this exchange before it can reconnect.
        deadline.cancel()
        raise Unavailable("MCP response body rejected")

    async def check_access(response: httpx2.Response) -> None:
        nonlocal denied
        if response.status_code in (401, 403):
            denied = True
        if (
            response.headers.get("content-encoding", "identity").lower() != "identity"
            or int(response.headers.get("content-length", "0")) > _MAX_RESPONSE_BYTES
        ):
            reject_body()
        assert isinstance(response.stream, httpx2.AsyncByteStream)
        response.stream = _BoundedStream(response.stream, reject_body)

    # Filters belong to the emitting logger. A filter on its parent cannot protect handlers
    # attached directly to a child. Other asyncio tasks keep their ordinary diagnostics.
    for name in (
        "mcp.client.streamable_http",
        "mcp.client.client",
        "client",
        "mcp.shared.jsonrpc_dispatcher",
        "mcp.shared.dispatcher",
        "mcp.shared.tool_name_validation",
        "httpx2",
        "httpcore2.connection",
        "httpcore2.http11",
        "httpcore2.proxy",
        "httpcore2.socks",
    ):
        logging.getLogger(name).addFilter(_private_diagnostics)
    marker = _private_wire.set(True)
    error: type[Exception] = Unavailable
    http = None
    try:
        timeout_seconds = expires_at - time.monotonic()
        with anyio.fail_after(timeout_seconds) as deadline:
            target = httpx2.URL(endpoint)
            https = target.scheme == "https"
            if https:
                _check_ca_directories()
            http = httpx2.AsyncClient(
                timeout=timeout_seconds,
                follow_redirects=False,
                trust_env=https,
                event_hooks={"request": [authenticate], "response": [check_access]},
            )
            async with (
                http,
                streamable_http_client(endpoint, http_client=http) as streams,
                ClientSession(
                    dispatcher=_TaskDispatcher(*streams),
                    extensions={EXTENSION: {}},
                    read_timeout_seconds=timeout_seconds,
                ) as session,
            ):
                await negotiate_auto(session)
                # Catch here so the transport's task group never wraps an error containing
                # peer data or an application callback exception in an ExceptionGroup.
                try:
                    caps = session.server_capabilities
                    if (
                        session.protocol_version != VERSION
                        or caps is None
                        or caps.tools is None
                        or (caps.extensions or {}).get(EXTENSION) != {}
                    ):
                        raise Unsupported
                    received.append(await operation(session))
                except Unsupported:
                    error = Unsupported
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        _private_wire.reset(marker)
    if http is None:
        raise PermissionError(_EGRESS_ERROR)
    # Retain an accepted receipt even if closing the transport fails.
    if received:
        return
    if denied:
        raise PermissionError("MCP request authorization denied")
    if error is Unsupported:
        raise Unsupported("MCP peer or operation is outside the selected Tasks profile")
    raise Unavailable("MCP operation did not return a usable receipt")


async def call_tool(
    session: ClientSession, name: str, arguments: dict[str, Any]
) -> Created | CallToolResult:
    """Admit one unambiguous declaration and send one typed request to the selected tool."""
    cursor = None
    seen: set[str] = set()
    found = False
    for _ in range(_MAX_TOOL_LIST_PAGES):
        # list_tools filters some declarations before returning them and caches peer schemas.
        listing = await session.send_request(
            ListToolsRequest(params=PaginatedRequestParams(cursor=cursor)), ListToolsResult
        )
        for tool in listing.tools:
            if tool.name == name:
                if (
                    found
                    or _has_reference(tool.output_schema)
                    or find_invalid_x_mcp_header(tool.input_schema) is not None
                    or mcp_param_headers(x_mcp_header_map(tool.input_schema), arguments)
                ):
                    raise Unsupported
                found = True
        cursor = listing.next_cursor
        if cursor is None:
            break
        if cursor in seen:
            raise Unsupported
        seen.add(cursor)
    else:
        raise Unsupported
    if not found:
        raise Unsupported
    # send_request applies the negotiated protocol stamps and validates the wire envelope.
    # Never execute a validator supplied by the selected tool.
    return await session.send_request(
        CallToolRequest(params=CallToolRequestParams(name=name, arguments=arguments)), _CALL_RESULT
    )


def _has_reference(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            k in {"$ref", "$dynamicRef", "$recursiveRef"} or _has_reference(v)
            for k, v in value.items()
        )
    return isinstance(value, list) and any(_has_reference(v) for v in value)
