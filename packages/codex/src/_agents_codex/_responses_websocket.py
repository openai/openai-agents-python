"""Run-owned Responses sockets over an application-owned AsyncOpenAI client."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, NoReturn, TypeAlias, cast
from weakref import WeakKeyDictionary

from typing_extensions import TypedDict

from ._lifecycle import _error_contexts, _join, _retain_cleanup_error
from ._public import preserve_module_identity as _preserve_module_identity
from .models import (
    _MAX_CHAT_STREAM_BYTES,
    ModelRequest,
    ModelRequestOptions,
    ModelRequestOptionsFactory,
    ModelRetryAdviceRequest,
    ModelRunOwner,
    ResponsesModel,
    _request_options_for,
    _ResponsesWebSocketFrame,
)
from .retry import RetryAdvice


class ResponsesWebSocketOptions(TypedDict, total=False):
    ping_interval: float | None
    ping_timeout: float | None
    max_size: int | None


class ResponsesSocketOptions(TypedDict):
    url: str
    headers: dict[str, str]
    open_timeout: float | None
    websocket_options: ResponsesWebSocketOptions


ResponsesSocketFactory: TypeAlias = Callable[[ModelRequest, ResponsesSocketOptions], Awaitable[Any]]


if TYPE_CHECKING:
    _SocketLoggerBase = logging.LoggerAdapter[logging.Logger]
else:
    _SocketLoggerBase = logging.LoggerAdapter


class _QuietSocketLogger(_SocketLoggerBase):
    def isEnabledFor(self, level: int) -> bool:
        return level > logging.DEBUG and super().isEnabledFor(level)


async def _connect(request: ModelRequest, options: ResponsesSocketOptions) -> Any:
    socket_options: ResponsesWebSocketOptions = {
        "max_size": _MAX_CHAT_STREAM_BYTES,
        **options["websocket_options"],
    }
    try:
        from websockets.asyncio.client import connect

        return await connect(
            options["url"],
            additional_headers=options["headers"],
            user_agent_header=None,
            logger=_QuietSocketLogger(logging.getLogger("websockets.client"), {}),
            open_timeout=options["open_timeout"],
            **cast(Any, socket_options),
        )
    finally:
        del request, options, socket_options


def _capture_websocket_options(
    value: ResponsesWebSocketOptions | None,
) -> ResponsesWebSocketOptions:
    if value is not None and not isinstance(value, Mapping):
        raise TypeError("Responses WebSocket options must be a mapping")
    data = {"max_size": _MAX_CHAT_STREAM_BYTES, **(dict(value) if value is not None else {})}
    if data.keys() - {"ping_interval", "ping_timeout", "max_size"}:
        raise ValueError("Unsupported Responses WebSocket option")
    for key, item in data.items():
        if item is None:
            continue
        if key == "max_size":
            valid = type(item) is int and item >= 0
        else:
            valid = (
                isinstance(item, (int, float))
                and not isinstance(item, bool)
                and math.isfinite(item)
                and item >= 0
            )
        if not valid:
            raise ValueError("Invalid Responses WebSocket option")
    return cast(ResponsesWebSocketOptions, data)


def _timeout(client: Any, selected: ModelRequestOptions, phase: str) -> float | None:
    if "timeout_ms" in selected:
        return selected["timeout_ms"] / 1000
    value = client.timeout
    value = getattr(value, phase, value)
    return None if value is None else float(value)


async def _socket_options(
    client: Any, selected: ModelRequestOptions, websocket_options: ResponsesWebSocketOptions
) -> ResponsesSocketOptions:
    import httpx2
    from openai import NotGiven, Omit

    headers: dict[str, str] = {}
    params: dict[str, Any] = {}
    refresh = refreshing = source = key = value = existing = url = None
    try:
        refresh = getattr(client, "_refresh_api_key", None)
        if callable(refresh):
            refreshing = refresh()
            if not inspect.isawaitable(refreshing):
                raise TypeError("Borrowed OpenAI credential refresh must be asynchronous")
            await refreshing
        for source in (client.auth_headers, client.default_headers, selected.get("headers", {})):
            for key, value in source.items():
                if isinstance(value, NotGiven):
                    continue
                for existing in tuple(headers):
                    if existing.lower() == key.lower():
                        del headers[existing]
                if value is not None and not isinstance(value, Omit):
                    headers[str(key)] = str(value)
        url = httpx2.URL(str(getattr(client, "websocket_base_url", None) or client.base_url))
        scheme = {"http": "ws", "https": "wss"}.get(url.scheme, url.scheme)
        if scheme not in ("ws", "wss") or not url.host or url.userinfo:
            raise ValueError("Responses WebSocket requires an absolute WS(S) endpoint")
        params.update(url.params)
        for source in (client.default_query, selected.get("query", {})):
            for key, value in source.items():
                if isinstance(value, NotGiven):
                    continue
                if isinstance(value, Omit):
                    params.pop(key, None)
                else:
                    params[key] = value
        url = url.copy_with(scheme=scheme, path=url.path.rstrip("/") + "/responses", params=params)
        return {
            "url": str(url),
            "headers": dict(headers),
            "open_timeout": _timeout(client, selected, "connect"),
            "websocket_options": deepcopy(websocket_options),
        }
    finally:
        del client, selected, websocket_options, headers, params
        del refresh, refreshing, source, key, value, existing, url


async def _limited(value: Awaitable[Any], timeout: float | None) -> Any:
    return await value if timeout is None else await asyncio.wait_for(value, timeout)


@dataclass(eq=False, repr=False)
class _Connection:
    socket: Any
    key: str
    closing: asyncio.Task[None] | None = None

    def close(self) -> asyncio.Task[None]:
        if self.closing is None:

            async def finish() -> None:
                result = self.socket.close()
                if inspect.isawaitable(result):
                    await result

            self.closing = asyncio.create_task(finish())
        return self.closing


def _reject_json_constant(_value: str) -> NoReturn:
    raise ValueError("Non-finite JSON number")


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        _reject_json_constant(value)
    return parsed


def _decode_socket_frame(payload: Any, limit: int | None) -> dict[str, Any]:
    decoded: Any = None
    try:
        if limit is not None and isinstance(payload, (str, bytes, bytearray)):
            if len(payload) > limit or (
                isinstance(payload, str) and len(payload.encode("utf-8")) > limit
            ):
                raise ValueError("Responses WebSocket frame exceeds its receive limit")
        decoded = json.loads(
            payload, parse_constant=_reject_json_constant, parse_float=_finite_json_float
        )
        if isinstance(decoded, dict):
            return decoded
    except (ValueError, TypeError, RecursionError):
        # Decoder exceptions and their tracebacks can retain the input value.
        pass
    finally:
        del payload, decoded
    raise ValueError("Responses WebSocket received an invalid JSON frame") from None


def _socket_closed(socket: Any) -> bool:
    return getattr(socket, "close_code", None) is not None or getattr(
        getattr(socket, "state", None), "name", None
    ) in ("CLOSING", "CLOSED")


@dataclass(eq=False, repr=False)
class _Owner:
    busy: bool = False
    retired: bool = False
    connection: _Connection | None = None
    opening: asyncio.Task[_Connection] | None = None
    closing: asyncio.Task[None] | None = None


class _SentWebSocketError(RuntimeError):
    pass


class ResponsesWebSocketModel(ResponsesModel):
    """Borrow AsyncOpenAI credentials and own sockets for one native run.

    Each owned continuation can reuse its run's socket. Headers and query values
    select the connection; changed values open a replacement. The SDK closes
    sockets before releasing a run and leaves the borrowed client open.

    Default receive ceiling: 8 MiB. Explicit unlimited override:
    ``websocket_options={"max_size": None}``.
    """

    _socket_factory: ResponsesSocketFactory
    _websocket_options: ResponsesWebSocketOptions
    _owners: WeakKeyDictionary[ModelRunOwner, _Owner]

    def __init__(
        self,
        client: Any,
        model: str | None = None,
        *,
        prompt_cache_key: str | None = None,
        request_options: ModelRequestOptions | ModelRequestOptionsFactory | None = None,
        websocket_options: ResponsesWebSocketOptions | None = None,
        socket_factory: ResponsesSocketFactory | None = None,
    ) -> None:
        if socket_factory is not None and not callable(socket_factory):
            raise TypeError("Responses socket factory must be callable")
        super().__init__(
            client,
            model,
            prompt_cache_key=prompt_cache_key,
            request_options=request_options,
            stream=True,
        )
        object.__setattr__(self, "_socket_factory", socket_factory or _connect)
        object.__setattr__(
            self, "_websocket_options", _capture_websocket_options(websocket_options)
        )
        object.__setattr__(self, "_owners", WeakKeyDictionary())

    @property
    def stream(self) -> Callable[[ModelRequest], AsyncIterator[Mapping[str, Any]]]:
        return self._stream_websocket

    def get_retry_advice(self, request: ModelRetryAdviceRequest) -> RetryAdvice | None:
        if isinstance(request.error, _SentWebSocketError):
            return RetryAdvice(
                suggested=False,
                replay_safety="unsafe",
                reason="Responses WebSocket frame may have reached the provider",
            )
        return None

    async def _create(self, request: ModelRequest, *, stream: bool) -> Any:
        event: Any = None
        events = self._stream_websocket(request)
        try:
            async for event in events:
                if isinstance(event, _ResponsesWebSocketFrame) and event.raw:
                    continue
                if event.get("type") in (
                    "response.completed",
                    "response.failed",
                    "response.incomplete",
                ):
                    return event["response"]
            raise ValueError("Responses WebSocket ended without a terminal response")
        finally:
            try:
                await events.aclose()
            finally:
                del request, event, events

    async def cleanup_on_run_end(self, owner: ModelRunOwner) -> None:
        state = self._owners.get(owner)
        if state is None:
            return
        if state.closing is None:
            state.retired = True

            async def close() -> None:
                if state.opening is not None:
                    try:
                        await _join(state.opening)
                    except BaseException:
                        pass
                if state.connection is not None:
                    await _join(state.connection.close())

            state.closing = asyncio.create_task(close())
        try:
            await _join(state.closing)
        finally:
            if self._owners.get(owner) is state:
                del self._owners[owner]

    async def _stream_websocket(
        self, request: ModelRequest
    ) -> AsyncGenerator[Mapping[str, Any], None]:
        if request.run_owner is None:
            raise TypeError("ResponsesWebSocketModel requires a native run owner")
        state = self._owners.setdefault(request.run_owner, _Owner())
        if state.busy or state.retired:
            raise RuntimeError("Responses socket owner is busy or retired")
        state.busy = True
        connection: _Connection | None = None
        body: Any = None
        selected: Any = None
        options: Any = None
        snapshot: Any = None
        payload: Any = None
        event: Any = None
        key = sending = response_id = candidate = None
        sent, terminal = False, False
        try:
            self._check_video_input(request)
            body = deepcopy(request.body)
            if self.model:
                body["model"] = self.model
            if self.prompt_cache_key is not None:
                body["prompt_cache_key"] = self.prompt_cache_key
            body.pop("stream", None)
            body["type"] = "response.create"
            selected = await _request_options_for(self._request_options_source, request)
            options = await _socket_options(self.client, selected, self._websocket_options)
            key = hashlib.sha256(
                json.dumps(
                    {
                        "url": options["url"],
                        "headers": sorted((k.lower(), v) for k, v in options["headers"].items()),
                    }
                ).encode()
            ).hexdigest()
            if state.connection is not None and (
                state.connection.key != key or _socket_closed(state.connection.socket)
            ):
                await _join(state.connection.close())
                state.connection = None
            if state.connection is None:
                snapshot = replace(request, body=deepcopy(request.body))

                async def open_connection(
                    captured_request: ModelRequest,
                    captured_options: ResponsesSocketOptions,
                    captured_key: str,
                ) -> _Connection:
                    socket = opened = None
                    try:
                        socket = await _limited(
                            self._socket_factory(captured_request, captured_options),
                            captured_options["open_timeout"],
                        )
                        opened = _Connection(socket, captured_key)
                        state.connection = opened
                        if not all(
                            callable(getattr(socket, name, None))
                            for name in ("send", "recv", "close")
                        ):
                            raise TypeError(
                                "Responses socket requires send, recv, and close methods"
                            )
                        if state.retired:
                            await _join(opened.close())
                            raise RuntimeError("Responses socket owner retired during connection")
                        return opened
                    finally:
                        del captured_request, captured_options, captured_key, socket, opened

                state.opening = asyncio.create_task(
                    open_connection(snapshot, deepcopy(options), key)
                )
                try:
                    connection = await _join(state.opening)
                finally:
                    state.opening = None
            else:
                connection = state.connection
            if state.retired:
                raise RuntimeError("Responses socket owner retired")
            assert connection is not None
            sent = True
            sending = connection.socket.send(json.dumps(body, allow_nan=False))
            if inspect.isawaitable(sending):
                await _limited(sending, _timeout(self.client, selected, "write"))
            response_id = None
            while True:
                payload = await _limited(
                    connection.socket.recv(), _timeout(self.client, selected, "read")
                )
                if isinstance(payload, _ResponsesWebSocketFrame):
                    event = payload
                    if event.raw:
                        if getattr(request, "_observe_raw_provider_events", False):
                            yield event
                        continue
                else:
                    event = _ResponsesWebSocketFrame(
                        _decode_socket_frame(payload, self._websocket_options.get("max_size"))
                    )
                if "response" in event and not isinstance(event["response"], dict):
                    raise ValueError("Responses WebSocket response must be an object")
                if not isinstance(payload, _ResponsesWebSocketFrame) and getattr(
                    request, "_observe_raw_provider_events", False
                ):
                    yield _ResponsesWebSocketFrame(event, raw=True)
                candidate = event.get("response", {}).get("id")
                if candidate is not None:
                    if response_id is not None and response_id != candidate:
                        raise ValueError("Responses WebSocket changed response identity")
                    response_id = candidate
                terminal = event.get("type") in (
                    "response.completed",
                    "response.failed",
                    "response.incomplete",
                )
                if event.get("type") == "error":
                    raise ValueError("Responses WebSocket provider error")
                yield event
                if terminal:
                    return
        except BaseException as error:
            if terminal and isinstance(error, GeneratorExit):
                raise
            if connection is None and state.connection is not None:
                connection = state.connection
            if connection is not None:
                try:
                    await _join(connection.close())
                except BaseException as cleanup_error:
                    _retain_cleanup_error(error, cleanup_error, _error_contexts(error.__context__))
                if state.connection is connection:
                    state.connection = None
            if sent and not isinstance(error, (asyncio.CancelledError, GeneratorExit)):
                raise _SentWebSocketError(
                    "Responses WebSocket request failed after send"
                ) from error
            raise
        finally:
            state.busy = False
            if not terminal and connection is not None and connection.closing is None:
                await _join(connection.close())
                if state.connection is connection:
                    state.connection = None
            del request, body, selected, options, snapshot, payload, event, connection
            del key, sending, response_id, candidate


_preserve_module_identity(globals(), "agents.models")
