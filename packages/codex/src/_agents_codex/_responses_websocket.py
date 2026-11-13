"""Run-owned Responses sockets over an application-owned AsyncOpenAI client."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import re
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, NoReturn, TypeAlias, cast
from weakref import WeakKeyDictionary

from typing_extensions import TypedDict

from ._lifecycle import _clear_error_frames, _error_contexts, _join, _retain_cleanup_error
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


def _socket_scope(options: ResponsesSocketOptions) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "url": options["url"],
                "headers": sorted((k.lower(), v) for k, v in options["headers"].items()),
            }
        ).encode()
    ).hexdigest()


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
    if timeout is None:
        return await value

    # wait_for can lose the caller's cancellation on Python 3.10 when the
    # child finishes in the same event-loop cycle.
    task = asyncio.ensure_future(value)
    try:
        if timeout > 0 and not task.done():
            await asyncio.wait((task,), timeout=timeout)
        if task.done():
            return task.result()

        task.cancel()
        # Wait on an independent completion signal so task cancellation means
        # timeout, while cancellation of this caller continues to propagate.
        await _join(asyncio.create_task(asyncio.wait((task,))))
        if task.cancelled():
            raise asyncio.TimeoutError()
        return task.result()
    except asyncio.CancelledError as error:
        if not task.done():
            task.cancel()
        try:
            await _join(task)
        except asyncio.CancelledError:
            pass
        except BaseException as cleanup_error:
            _retain_cleanup_error(error, cleanup_error, _error_contexts(error.__context__))
        raise


@dataclass(eq=False, repr=False)
class _Connection:
    socket: Any
    key: str
    closing: asyncio.Task[None] | None = None

    def close(self) -> asyncio.Task[None]:
        if self.closing is None:

            async def finish(socket: Any) -> None:
                result = None
                try:
                    result = socket.close()
                    if inspect.isawaitable(result):
                        await result
                except BaseException as error:
                    _clear_error_frames(error)
                    raise
                finally:
                    del socket, result

            self.closing = asyncio.create_task(finish(self.socket))
        return self.closing


def _reject_json_constant(_value: str) -> NoReturn:
    raise ValueError("Non-finite JSON number")


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        _reject_json_constant(value)
    return parsed


# Bound decoded allocation before building the provider-controlled object graph.
# Count containers, object keys, and scalar values; string contents are one node.
_MAX_SOCKET_JSON_NODES = 65_536
_MAX_SOCKET_JSON_DEPTH = 128
_JSON_TOKEN = re.compile(r'[\[\]{}"]|[^ \t\r\n\[\]{}":,]+')
_JSON_STRING_SPECIAL = re.compile(r'["\\]')


class _SocketJsonBudget:
    def __init__(self) -> None:
        self.remaining = _MAX_SOCKET_JSON_NODES


def _check_socket_json_structure(payload: str, max_nodes: int | None = None) -> int:
    nodes = depth = position = 0
    while (token := _JSON_TOKEN.search(payload, position)) is not None:
        position = token.end()
        first = payload[token.start()]
        if first in "}]":
            depth -= 1
            continue
        nodes += 1
        if nodes > _MAX_SOCKET_JSON_NODES or (max_nodes is not None and nodes > max_nodes):
            raise ValueError("Responses WebSocket frame exceeds its JSON node limit")
        if first in "{[":
            depth += 1
            if depth > _MAX_SOCKET_JSON_DEPTH:
                raise ValueError("Responses WebSocket frame exceeds its JSON depth limit")
        elif first == '"':
            # Search without capturing/copying string contents or allocating a
            # token list. Escapes skip one character; json.loads validates them.
            while (special := _JSON_STRING_SPECIAL.search(payload, position)) is not None:
                position = special.end()
                if payload[special.start()] == '"':
                    break
                position += 1
            else:
                return nodes  # The normal decoder rejects an unterminated string.
    return nodes


def _socket_frame_size(payload: Any, limit: int | None) -> int:
    size = 0
    try:
        if not isinstance(payload, (str, bytes, bytearray)):
            raise TypeError("Unsupported WebSocket frame")
        if limit is not None and len(payload) > limit:
            raise ValueError("WebSocket frame exceeds its receive limit")
        if isinstance(payload, str):
            # Count UTF-8 without allocating a second whole peer-controlled frame.
            for start in range(0, len(payload), 4096):
                size += len(payload[start : start + 4096].encode("utf-8"))
                if limit is not None and size > limit:
                    raise ValueError("WebSocket frame exceeds its receive limit")
        else:
            size = len(payload)
        return size
    except (TypeError, ValueError):
        pass
    finally:
        del payload
    raise ValueError("Responses WebSocket received an invalid frame size") from None


def _decode_socket_frame(
    payload: Any, limit: int | None, *, budget: _SocketJsonBudget | None = None
) -> dict[str, Any]:
    decoded: Any = None
    nodes = 0
    try:
        _socket_frame_size(payload, limit)
        if isinstance(payload, (bytes, bytearray)):
            # Preserve the standard decoder's supported byte encodings.
            payload = payload.decode(json.detect_encoding(payload), "surrogatepass")
        if isinstance(payload, str):
            nodes = _check_socket_json_structure(payload, budget.remaining if budget else None)
        decoded = json.loads(
            payload, parse_constant=_reject_json_constant, parse_float=_finite_json_float
        )
        if isinstance(decoded, dict):
            if budget is not None:
                budget.remaining -= nodes
            return decoded
    except (ValueError, TypeError, RecursionError):
        # Decoder exceptions and their tracebacks can retain the input value.
        pass
    finally:
        del payload, decoded, budget
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

    Frames allow at most 65,536 JSON containers, keys, and scalar values, with
    nesting depth at most 128. String contents count as one value.

    Default receive ceiling: 8 MiB. Explicit unlimited byte override:
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

            async def close(current: _Owner) -> None:
                try:
                    if current.opening is not None:
                        try:
                            await _join(current.opening)
                        except BaseException:
                            pass
                    if current.connection is not None:
                        await _join(current.connection.close())
                finally:
                    del current

            state.closing = asyncio.create_task(close(state))
        try:
            await _join(state.closing)
        except BaseException as error:
            _clear_error_frames(error)
            raise
        finally:
            if self._owners.get(owner) is state:
                del self._owners[owner]
            del self, owner, state

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
        key = sending = response_id = candidate = socket = None
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
            key = _socket_scope(options)
            if state.connection is not None:
                socket = state.connection.socket
                scope_changed = state.connection.key != key
                replace_connection = scope_changed or _socket_closed(socket)
                # Only the SDK-owned hosted wrapper needs to reconcile a pause
                # before retiring its connection or sending a new configuration.
                from ._hosted_responses_websocket import _Socket

                if isinstance(socket, _Socket):
                    replace_connection = await socket._replace_before_send(
                        body, replace_connection, scope_changed=scope_changed
                    )
                if replace_connection:
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
                    except BaseException as error:
                        _clear_error_frames(error)
                        raise
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
            try:
                sending = connection.socket.send(json.dumps(body, allow_nan=False))
                if inspect.isawaitable(sending):
                    await _limited(sending, _timeout(self.client, selected, "write"))
            except BaseException as error:
                _clear_error_frames(error)
                raise
            response_id = None
            while True:
                try:
                    payload = await _limited(
                        connection.socket.recv(), _timeout(self.client, selected, "read")
                    )
                except BaseException as error:
                    _clear_error_frames(error)
                    raise
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
            del key, sending, response_id, candidate, socket


_preserve_module_identity(globals(), "agents.models")
