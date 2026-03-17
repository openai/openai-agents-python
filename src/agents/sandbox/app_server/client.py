from __future__ import annotations

import json
import threading
import uuid
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Callable, TypeVar, cast

from pydantic import BaseModel
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect

from .errors import AppServerError, TransportClosedError, map_jsonrpc_error
from .generated.notification_registry import NOTIFICATION_MODELS
from .generated.v2_all import (
    AgentMessageDeltaNotification,
    ModelListResponse,
    ThreadArchiveResponse,
    ThreadCompactStartResponse,
    ThreadForkParams as V2ThreadForkParams,
    ThreadForkResponse,
    ThreadListParams as V2ThreadListParams,
    ThreadListResponse,
    ThreadReadResponse,
    ThreadResumeParams as V2ThreadResumeParams,
    ThreadResumeResponse,
    ThreadSetNameResponse,
    ThreadStartParams as V2ThreadStartParams,
    ThreadStartResponse,
    ThreadUnarchiveResponse,
    TurnCompletedNotification,
    TurnInterruptResponse,
    TurnStartParams as V2TurnStartParams,
    TurnStartResponse,
    TurnSteerResponse,
)
from .models import (
    InitializeResponse,
    JsonObject,
    JsonValue,
    Notification,
    UnknownNotification,
)
from .retry import retry_on_overload

ModelT = TypeVar("ModelT", bound=BaseModel)
ApprovalHandler = Callable[[str, JsonObject | None], JsonObject]


def _params_dict(
    params: (
        V2ThreadStartParams
        | V2ThreadResumeParams
        | V2ThreadListParams
        | V2ThreadForkParams
        | V2TurnStartParams
        | JsonObject
        | None
    ),
) -> JsonObject:
    if params is None:
        return {}
    if hasattr(params, "model_dump"):
        dumped = params.model_dump(
            by_alias=True,
            exclude_none=True,
            mode="json",
        )
        if not isinstance(dumped, dict):
            raise TypeError("Expected model_dump() to return dict")
        return dumped
    if isinstance(params, dict):
        return params
    raise TypeError(f"Expected generated params model or dict, got {type(params).__name__}")


@dataclass(frozen=True)
class AppServerTransportOps:
    ws_connect: Callable[..., ClientConnection]


def _default_transport_ops() -> AppServerTransportOps:
    return AppServerTransportOps(ws_connect=connect)


@dataclass(slots=True)
class AppServerConfig:
    websocket_url: str | None = None
    websocket_headers: dict[str, str] | None = None
    client_name: str = "codex_python_sdk"
    client_title: str = "Codex Python SDK"
    client_version: str = "0.2.0"
    experimental_api: bool = True
    websocket_open_timeout_s: float = 10.0
    websocket_recv_timeout_s: float | None = None


class AppServerClient:
    """Synchronous typed JSON-RPC client for a remote `codex app-server` websocket."""

    def __init__(
        self,
        config: AppServerConfig | None = None,
        approval_handler: ApprovalHandler | None = None,
        transport_ops: AppServerTransportOps | None = None,
    ) -> None:
        self.config = config or AppServerConfig()
        self._approval_handler = approval_handler or self._default_approval_handler
        self._ops = transport_ops or _default_transport_ops()
        self._conn: ClientConnection | None = None
        self._lock = threading.Lock()
        self._turn_consumer_lock = threading.Lock()
        self._active_turn_consumer: str | None = None
        self._pending_notifications: deque[Notification] = deque()

    def __enter__(self) -> AppServerClient:
        self.start()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    @property
    def connected_url(self) -> str | None:
        return self.config.websocket_url if self._conn is not None else None

    def start(self) -> None:
        if self._conn is not None:
            return

        websocket_url = self.config.websocket_url
        if not websocket_url:
            raise ValueError(
                "AppServerConfig.websocket_url is required for remote app-server clients."
            )

        try:
            self._conn = self._ops.ws_connect(
                websocket_url,
                additional_headers=self.config.websocket_headers,
                open_timeout=self.config.websocket_open_timeout_s,
                max_size=None,
            )
        except Exception as exc:
            raise TransportClosedError(
                f"Failed to connect to app-server websocket `{websocket_url}`: {exc}"
            ) from exc

    def close(self) -> None:
        conn = self._conn
        self._conn = None
        self._active_turn_consumer = None

        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def initialize(self) -> InitializeResponse:
        result = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": self.config.client_name,
                    "title": self.config.client_title,
                    "version": self.config.client_version,
                },
                "capabilities": {
                    "experimentalApi": self.config.experimental_api,
                },
            },
            response_model=InitializeResponse,
        )
        self.notify("initialized", None)
        return result

    def request(
        self,
        method: str,
        params: JsonObject | None,
        *,
        response_model: type[ModelT],
    ) -> ModelT:
        result = self._request_raw(method, params)
        if not isinstance(result, dict):
            raise AppServerError(f"{method} response must be a JSON object")
        return response_model.model_validate(result)

    def _request_raw(self, method: str, params: JsonObject | None = None) -> JsonValue:
        request_id = str(uuid.uuid4())
        self._write_message({"id": request_id, "method": method, "params": params or {}})

        while True:
            msg = self._read_message()
            method_name = msg.get("method")

            if isinstance(method_name, str) and "id" in msg:
                response = self._handle_server_request(msg)
                self._write_message({"id": msg["id"], "result": response})
                continue

            if isinstance(method_name, str) and "id" not in msg:
                self._pending_notifications.append(
                    self._coerce_notification(method_name, msg.get("params"))
                )
                continue

            if msg.get("id") != request_id:
                continue

            if "error" in msg:
                err = msg["error"]
                if isinstance(err, dict):
                    code_value = err.get("code", -32000)
                    code = int(code_value) if isinstance(code_value, (int, float, str)) else -32000
                    raise map_jsonrpc_error(
                        code,
                        str(err.get("message", "unknown")),
                        err.get("data"),
                    )
                raise AppServerError("Malformed JSON-RPC error response")

            return msg.get("result")

    def notify(self, method: str, params: JsonObject | None = None) -> None:
        self._write_message({"method": method, "params": params or {}})

    def next_notification(self) -> Notification:
        if self._pending_notifications:
            return self._pending_notifications.popleft()

        while True:
            msg = self._read_message()
            method_name = msg.get("method")
            if isinstance(method_name, str) and "id" in msg:
                response = self._handle_server_request(msg)
                self._write_message({"id": msg["id"], "result": response})
                continue
            if isinstance(method_name, str) and "id" not in msg:
                return self._coerce_notification(method_name, msg.get("params"))

    def acquire_turn_consumer(self, turn_id: str) -> None:
        with self._turn_consumer_lock:
            if self._active_turn_consumer is not None:
                raise RuntimeError(
                    "Concurrent turn consumers are not yet supported in the experimental SDK. "
                    f"Client is already streaming turn {self._active_turn_consumer!r}; "
                    f"cannot start turn {turn_id!r} until the active consumer finishes."
                )
            self._active_turn_consumer = turn_id

    def release_turn_consumer(self, turn_id: str) -> None:
        with self._turn_consumer_lock:
            if self._active_turn_consumer == turn_id:
                self._active_turn_consumer = None

    def thread_start(
        self, params: V2ThreadStartParams | JsonObject | None = None
    ) -> ThreadStartResponse:
        return self.request(
            "thread/start", _params_dict(params), response_model=ThreadStartResponse
        )

    def thread_resume(
        self,
        thread_id: str,
        params: V2ThreadResumeParams | JsonObject | None = None,
    ) -> ThreadResumeResponse:
        payload = {"threadId": thread_id, **_params_dict(params)}
        return self.request("thread/resume", payload, response_model=ThreadResumeResponse)

    def thread_list(
        self, params: V2ThreadListParams | JsonObject | None = None
    ) -> ThreadListResponse:
        return self.request("thread/list", _params_dict(params), response_model=ThreadListResponse)

    def thread_read(self, thread_id: str, include_turns: bool = False) -> ThreadReadResponse:
        return self.request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": include_turns},
            response_model=ThreadReadResponse,
        )

    def thread_fork(
        self,
        thread_id: str,
        params: V2ThreadForkParams | JsonObject | None = None,
    ) -> ThreadForkResponse:
        payload = {"threadId": thread_id, **_params_dict(params)}
        return self.request("thread/fork", payload, response_model=ThreadForkResponse)

    def thread_archive(self, thread_id: str) -> ThreadArchiveResponse:
        return self.request(
            "thread/archive", {"threadId": thread_id}, response_model=ThreadArchiveResponse
        )

    def thread_unarchive(self, thread_id: str) -> ThreadUnarchiveResponse:
        return self.request(
            "thread/unarchive", {"threadId": thread_id}, response_model=ThreadUnarchiveResponse
        )

    def thread_set_name(self, thread_id: str, name: str) -> ThreadSetNameResponse:
        return self.request(
            "thread/name/set",
            {"threadId": thread_id, "name": name},
            response_model=ThreadSetNameResponse,
        )

    def thread_compact(self, thread_id: str) -> ThreadCompactStartResponse:
        return self.request(
            "thread/compact/start",
            {"threadId": thread_id},
            response_model=ThreadCompactStartResponse,
        )

    def turn_start(
        self,
        thread_id: str,
        input_items: list[JsonObject] | JsonObject | str,
        params: V2TurnStartParams | JsonObject | None = None,
    ) -> TurnStartResponse:
        payload: JsonObject = {
            **_params_dict(params),
            "threadId": thread_id,
            "input": cast(JsonValue, self._normalize_input_items(input_items)),
        }
        return self.request("turn/start", payload, response_model=TurnStartResponse)

    def turn_interrupt(self, thread_id: str, turn_id: str) -> TurnInterruptResponse:
        return self.request(
            "turn/interrupt",
            {"threadId": thread_id, "turnId": turn_id},
            response_model=TurnInterruptResponse,
        )

    def turn_steer(
        self,
        thread_id: str,
        expected_turn_id: str,
        input_items: list[JsonObject] | JsonObject | str,
    ) -> TurnSteerResponse:
        payload: JsonObject = {
            "threadId": thread_id,
            "expectedTurnId": expected_turn_id,
            "input": cast(JsonValue, self._normalize_input_items(input_items)),
        }
        return self.request(
            "turn/steer",
            payload,
            response_model=TurnSteerResponse,
        )

    def model_list(self, include_hidden: bool = False) -> ModelListResponse:
        return self.request(
            "model/list",
            {"includeHidden": include_hidden},
            response_model=ModelListResponse,
        )

    def request_with_retry_on_overload(
        self,
        method: str,
        params: JsonObject | None,
        *,
        response_model: type[ModelT],
        max_attempts: int = 3,
        initial_delay_s: float = 0.25,
        max_delay_s: float = 2.0,
    ) -> ModelT:
        return retry_on_overload(
            lambda: self.request(method, params, response_model=response_model),
            max_attempts=max_attempts,
            initial_delay_s=initial_delay_s,
            max_delay_s=max_delay_s,
        )

    def wait_for_turn_completed(self, turn_id: str) -> TurnCompletedNotification:
        while True:
            notification = self.next_notification()
            if (
                notification.method == "turn/completed"
                and isinstance(notification.payload, TurnCompletedNotification)
                and notification.payload.turn.id == turn_id
            ):
                return notification.payload

    def stream_until_methods(self, methods: Iterable[str] | str) -> list[Notification]:
        target_methods = {methods} if isinstance(methods, str) else set(methods)
        out: list[Notification] = []
        while True:
            notification = self.next_notification()
            out.append(notification)
            if notification.method in target_methods:
                return out

    def stream_text(
        self,
        thread_id: str,
        text: str,
        params: V2TurnStartParams | JsonObject | None = None,
    ) -> Iterator[AgentMessageDeltaNotification]:
        started = self.turn_start(thread_id, text, params=params)
        turn_id = started.turn.id
        while True:
            notification = self.next_notification()
            if (
                notification.method == "item/agentMessage/delta"
                and isinstance(notification.payload, AgentMessageDeltaNotification)
                and notification.payload.turn_id == turn_id
            ):
                yield notification.payload
                continue
            if (
                notification.method == "turn/completed"
                and isinstance(notification.payload, TurnCompletedNotification)
                and notification.payload.turn.id == turn_id
            ):
                break

    def _coerce_notification(self, method: str, params: object) -> Notification:
        params_dict = params if isinstance(params, dict) else {}

        model = NOTIFICATION_MODELS.get(method)
        if model is None:
            return Notification(method=method, payload=UnknownNotification(params=params_dict))

        try:
            payload = model.model_validate(params_dict)
        except Exception:
            return Notification(method=method, payload=UnknownNotification(params=params_dict))
        return Notification(method=method, payload=cast(Any, payload))

    def _normalize_input_items(
        self,
        input_items: list[JsonObject] | JsonObject | str,
    ) -> list[JsonObject]:
        if isinstance(input_items, str):
            return [{"type": "text", "text": input_items}]
        if isinstance(input_items, dict):
            return [input_items]
        return input_items

    def _default_approval_handler(self, method: str, params: JsonObject | None) -> JsonObject:
        if method == "item/commandExecution/requestApproval":
            return {"decision": "accept"}
        if method == "item/fileChange/requestApproval":
            return {"decision": "accept"}
        return {}

    def _handle_server_request(self, msg: dict[str, JsonValue]) -> JsonObject:
        method = msg["method"]
        params = msg.get("params")
        if not isinstance(method, str):
            return {}
        return self._approval_handler(
            method,
            params if isinstance(params, dict) else None,
        )

    def _write_message(self, payload: JsonObject) -> None:
        if self._conn is None:
            raise TransportClosedError("app-server websocket is not connected")

        try:
            with self._lock:
                self._conn.send(json.dumps(payload), text=True)
        except ConnectionClosed as exc:
            raise TransportClosedError(
                f"app-server websocket closed while sending. url={self.config.websocket_url!r}"
            ) from exc
        except Exception as exc:
            raise AppServerError(f"Failed to send websocket message: {exc}") from exc

    def _read_message(self) -> dict[str, JsonValue]:
        if self._conn is None:
            raise TransportClosedError("app-server websocket is not connected")

        try:
            frame = self._conn.recv(timeout=self.config.websocket_recv_timeout_s)
        except ConnectionClosed as exc:
            raise TransportClosedError(
                f"app-server websocket closed while receiving. url={self.config.websocket_url!r}"
            ) from exc
        except Exception as exc:
            raise AppServerError(f"Failed to receive websocket message: {exc}") from exc

        if isinstance(frame, bytes):
            try:
                raw_message = frame.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise AppServerError(
                    "Received non-UTF-8 websocket binary frame from app-server"
                ) from exc
        else:
            raw_message = frame

        try:
            message = json.loads(raw_message)
        except json.JSONDecodeError as exc:
            raise AppServerError(f"Invalid JSON-RPC frame: {raw_message!r}") from exc

        if not isinstance(message, dict):
            raise AppServerError(f"Invalid JSON-RPC payload: {message!r}")
        return message
