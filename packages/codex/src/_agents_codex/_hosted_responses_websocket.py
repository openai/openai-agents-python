"""Provider beta response boundaries over the native run-owned socket adapter."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections import Counter, deque
from collections.abc import AsyncIterator, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any, cast
from weakref import WeakKeyDictionary

from ._compat import cancellation_requested
from ._lifecycle import _clear_error_frames
from ._public import preserve_module_identity as _preserve_module_identity
from ._responses_websocket import (
    ResponsesSocketFactory,
    ResponsesSocketOptions,
    ResponsesWebSocketModel,
    ResponsesWebSocketOptions,
    _check_socket_json_structure,
    _connect,
    _decode_socket_frame,
    _limited,
    _socket_frame_size,
    _socket_scope,
    _SocketJsonBudget,
)
from .models import (
    ModelRequest,
    ModelRequestOptions,
    ModelRequestOptionsFactory,
    ModelResponse,
    ModelRetryAdviceRequest,
    ModelRunOwner,
    _capture_model_usage,
    _ResponsesWebSocketFrame,
)
from .retry import RetryAdvice

_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_RESPONSE_ITEMS = 4096
_TERMINAL = {"response.completed", "response.failed", "response.incomplete"}
_COLLABORATION = {"agent_message", "multi_agent_call", "multi_agent_call_output"}
_PROGRAM_ITEMS = {"program", "program_output"}


@dataclass
class _Active:
    create: dict[str, Any]
    lineage: str
    terminal_reported: bool = False
    continuation: dict[str, Any] | None = None
    injection_input: list[dict[str, Any]] | None = None
    fallback_input: list[dict[str, Any]] | None = None
    acknowledged_outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    acknowledged_user_items: dict[str, tuple[str | None, dict[str, Any]]] = field(
        default_factory=dict
    )
    template: dict[str, Any] | None = None
    response_id: str | None = None
    pending: set[str] = field(default_factory=set)
    emitted: set[str] = field(default_factory=set)
    program_parents: set[str] = field(default_factory=set)
    passive: Counter[bytes] = field(default_factory=Counter)
    passive_count: int = 0
    queued: list[dict[str, Any]] = field(default_factory=list)
    injecting: set[str] | None = None
    terminal: dict[str, Any] | None = None
    size: int = 0


@dataclass
class _Run:
    socket_scope: str | None = None
    retired: bool = False
    recovered_budget: _SocketJsonBudget = field(default_factory=_SocketJsonBudget)
    active: _Active | None = None
    request: ModelRequest | None = None
    account: Callable[[dict[str, Any]], str | None] | None = None
    recovered: deque[_ResponsesWebSocketFrame] = field(default_factory=deque)


class HostedResponsesWebSocketModel:
    """Provider-hosted function pauses with native local tool execution."""

    def __init__(
        self,
        client: Any,
        model: str | None = None,
        *,
        prompt_cache_key: str | None = None,
        request_options: ModelRequestOptions | ModelRequestOptionsFactory | None = None,
        websocket_options: ResponsesWebSocketOptions | None = None,
        socket_factory: ResponsesSocketFactory | None = None,
        max_concurrent_subagents: int | None = None,
    ) -> None:
        if max_concurrent_subagents is not None and (
            type(max_concurrent_subagents) is not int or max_concurrent_subagents <= 0
        ):
            raise ValueError("max_concurrent_subagents requires a positive integer")
        config: dict[str, Any] = {"enabled": True}
        if max_concurrent_subagents is not None:
            config["max_concurrent_subagents"] = max_concurrent_subagents
        self._runs: WeakKeyDictionary[ModelRunOwner, _Run] = WeakKeyDictionary()
        factory = socket_factory or _connect

        async def connect(request: ModelRequest, options: ResponsesSocketOptions) -> Any:
            assert request.run_owner is not None
            run = self._runs.setdefault(request.run_owner, _Run())
            if run.retired:
                raise ValueError("Hosted response owner is retired after failed reconciliation")
            scope = _socket_scope(options)
            if run.socket_scope is not None and run.socket_scope != scope:
                raise ValueError("Hosted active response cannot change provider scope")
            selected = deepcopy(options)
            headers = selected["headers"]
            betas: list[str] = []
            for key in tuple(headers):
                if key.lower() == "openai-beta":
                    betas.extend(
                        value.strip() for value in headers.pop(key).split(",") if value.strip()
                    )
            headers["OpenAI-Beta"] = ",".join(dict.fromkeys([*betas, "responses_multi_agent=v1"]))
            socket = await factory(request, selected)
            run.socket_scope = scope
            return _Socket(
                socket,
                run,
                config,
                selected["websocket_options"].get("max_size"),
            )

        self._transport = ResponsesWebSocketModel(
            client,
            model,
            prompt_cache_key=prompt_cache_key,
            request_options=request_options,
            websocket_options=websocket_options,
            socket_factory=connect,
        )

    def _begin(self, request: ModelRequest) -> _Run:
        if request.run_owner is None:
            raise TypeError("Hosted Responses requires a native run owner")
        run = self._runs.setdefault(request.run_owner, _Run())
        if run.request is not None:
            raise TypeError("Hosted Responses already has an active request")
        callback = getattr(request, "_account_provider_request", None)
        if not callable(callback):
            raise TypeError("Hosted transport requires native request accounting")
        account = cast(Callable[[dict[str, Any]], str | None], callback)
        account({"operation": "manage"})
        run.request = request
        run.account = account
        return run

    async def stream(self, request: ModelRequest) -> AsyncIterator[Mapping[str, Any]]:
        run = self._begin(request)
        try:
            async for event in self._transport.stream(request):
                yield event
        finally:
            if run.request is request:
                run.request = None
                run.account = None
            del request, run

    async def complete(self, request: ModelRequest) -> ModelResponse:
        run = self._begin(request)
        try:
            return await self._transport.complete(request)
        finally:
            if run.request is request:
                run.request = None
                run.account = None
            del request, run

    def get_retry_advice(self, request: ModelRetryAdviceRequest) -> RetryAdvice | None:
        return self._transport.get_retry_advice(request)

    async def cleanup_on_run_end(self, owner: ModelRunOwner) -> None:
        await self._transport.cleanup_on_run_end(owner)
        self._runs.pop(owner, None)


def _stable(item: dict[str, Any]) -> bool:
    agent = item.get("agent")
    return item.get("type") not in _COLLABORATION and (
        item.get("type") != "message"
        or isinstance(agent, dict)
        and agent.get("agent_name") == "/root"
        and item.get("phase") == "final_answer"
    )


def _program_key(item: dict[str, Any]) -> bytes:
    # Terminal snapshots can repeat completed output-item frames. The same program
    # may also emit multiple distinct outputs before it finishes.
    return sha256(json.dumps(item, sort_keys=True, separators=(",", ":")).encode()).digest()


def _provider_object(value: Any, diagnostic: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    del value
    raise ValueError(diagnostic)


def _hosted_usage(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    raw = input_details = output_details = None
    try:
        raw = _provider_object(value, "Hosted usage must be an object")
        input_details = _provider_object(
            {} if raw.get("input_tokens_details") is None else raw["input_tokens_details"],
            "Hosted input details must be an object",
        )
        output_details = _provider_object(
            {} if raw.get("output_tokens_details") is None else raw["output_tokens_details"],
            "Hosted output details must be an object",
        )
        return _capture_model_usage(
            {
                "input_tokens": raw.get("input_tokens", 0),
                "output_tokens": raw.get("output_tokens", 0),
                "total_tokens": raw.get("total_tokens", 0),
                "cached_input_tokens": input_details.get("cached_tokens") or 0,
                "cache_write_input_tokens": input_details.get("cache_write_tokens") or 0,
                "reasoning_output_tokens": output_details.get("reasoning_tokens") or 0,
            }
        )
    except (TypeError, ValueError, OverflowError):
        pass
    finally:
        del value, raw, input_details, output_details
    raise ValueError("Hosted Responses received invalid usage") from None


def _response_configuration(frame: dict[str, Any]) -> tuple[dict[str, Any], list[Any]]:
    # Native client metadata and history pointers describe the request, not the Agent.
    config = {
        key: value
        for key, value in frame.items()
        if key not in {"input", "previous_response_id", "stream", "betas", "client_metadata"}
    }
    developer = [
        item.get("content")
        for item in frame.get("input", ())
        if isinstance(item, dict)
        and item.get("type") == "message"
        and item.get("role") == "developer"
    ]
    return config, developer


class _Socket:
    def __init__(
        self, socket: Any, run: _Run, config: dict[str, Any], max_size: int | None
    ) -> None:
        self._socket = socket
        self._run = run
        self._config = config
        self._max_size = max_size
        self._queue: deque[_ResponsesWebSocketFrame] = deque()

    @property
    def close_code(self) -> Any:
        return getattr(self._socket, "close_code", None)

    @property
    def state(self) -> Any:
        return getattr(self._socket, "state", None)

    def _account(self, operation: dict[str, Any]) -> str | None:
        if self._run.account is None:
            raise TypeError("Hosted transport requires an active native request")
        return self._run.account(operation)

    async def _send(self, frame: dict[str, Any]) -> None:
        active = result = None
        try:
            task = asyncio.current_task()
            if task is not None and cancellation_requested(task):
                raise asyncio.CancelledError
            active = self._run.active
            if active is None:
                raise RuntimeError("Hosted send requires a physical response owner")
            self._account({"operation": "dispatch", "lineage": active.lineage})
            result = self._socket.send(json.dumps(frame, allow_nan=False))
            if inspect.isawaitable(result):
                await result
        except BaseException as error:
            _clear_error_frames(error)
            raise
        finally:
            del self, frame, active, result

    async def _create(self, frame: dict[str, Any], emitted: set[str] | None = None) -> None:
        lineage = None
        try:
            size = len(json.dumps(frame, allow_nan=False).encode("utf-8"))
            if size > _MAX_RESPONSE_BYTES:
                raise ValueError("Hosted request exceeds the native retained-byte limit")
            lineage = self._account({"operation": "create"})
            if not lineage:
                raise TypeError("Hosted create requires a native request lineage")
            self._run.active = _Active(
                create=frame, lineage=lineage, emitted=emitted or set(), size=size
            )
            await self._send({**frame, "type": "response.create", "multi_agent": self._config})
        finally:
            del self, frame, emitted, lineage

    async def _fallback(
        self, active: _Active, frame: dict[str, Any], outputs: list[dict[str, Any]]
    ) -> None:
        completed = next_frame = replay = acknowledged = terminal_output = item = result = None
        anchored_users: dict[str | None, list[dict[str, Any]]] | None = None
        call_id = messages = None
        try:
            completed = (active.terminal or {}).get("response")
            if not isinstance(completed, dict) or not isinstance(completed.get("id"), str):
                raise ValueError("Hosted continuation requires an exact terminal response")
            next_frame = {**frame, "input": outputs}
            stored = completed.get("store")
            if type(stored) is not bool:
                stored = active.create.get("store") is not False
            if next_frame.get("conversation") is not None:
                next_frame.pop("previous_response_id", None)
            elif stored:
                next_frame["previous_response_id"] = completed["id"]
            else:
                replay = list(active.create.get("input", []))
                acknowledged = active.acknowledged_outputs.copy()
                anchored_users = {}
                for call_id, item in active.acknowledged_user_items.values():
                    anchored_users.setdefault(call_id, []).append(item)
                replay.extend(anchored_users.pop(None, ()))
                terminal_output = completed.get("output", [])
                for item in terminal_output:
                    replay.append(item)
                    if item.get("type") == "function_call":
                        call_id = item.get("call_id")
                        result = acknowledged.pop(call_id, None)
                        if result is not None:
                            replay.append(result)
                            replay.extend(anchored_users.pop(call_id, ()))
                for call_id, result in acknowledged.items():
                    replay.append(result)
                    replay.extend(anchored_users.pop(call_id, ()))
                for messages in anchored_users.values():
                    replay.extend(messages)
                next_frame["input"] = [*replay, *outputs]
                original_previous = active.create.get("previous_response_id")
                if isinstance(original_previous, str):
                    next_frame["previous_response_id"] = original_previous
                else:
                    next_frame.pop("previous_response_id", None)
            await self._create(next_frame, active.emitted)
        finally:
            del self, active, frame, outputs, completed, next_frame, replay
            del acknowledged, terminal_output, item, result, anchored_users, call_id, messages

    def _continuation(
        self, active: _Active, frame: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], set[str]]:
        items = input_items = original_user_ids = ids = None
        try:
            original_user_ids = {
                item.get("id")
                for item in active.create.get("input", [])
                if isinstance(item, dict) and item.get("role") == "user"
            }
            input_items = frame.get("input", [])
            items = [
                item
                for item in input_items
                if isinstance(item, dict)
                and (
                    item.get("type") == "function_call_output"
                    and item.get("call_id") in active.pending
                    or item.get("type") == "message"
                    and item.get("role") == "user"
                    and isinstance(item.get("id"), str)
                    and item["id"] not in original_user_ids
                    and item["id"] not in active.acknowledged_user_items
                )
            ]
            ids = {item["call_id"] for item in items if item["type"] == "function_call_output"}
            if not active.response_id or not ids or active.pending - ids:
                raise ValueError("Hosted response requires the matching local function outputs")
            return items, ids
        finally:
            del self, frame, active, items, input_items, original_user_ids, ids

    async def _replace_before_send(
        self, frame: dict[str, Any], replacing: bool, *, scope_changed: bool
    ) -> bool:
        active = None
        try:
            active = self._run.active
            if active is None:
                return replacing
            if scope_changed:
                raise ValueError("Hosted active response cannot change provider scope")
            changed = _response_configuration(active.create) != _response_configuration(frame)
            if not replacing and not changed:
                return False
            self._continuation(active, frame)
            if replacing and active.terminal is None:
                await self._reconcile_terminal(active)
            if changed:
                # A synthetic tool pause is not a provider response pointer.
                # Start from the new native input and captured destination config.
                if frame.get("previous_response_id") == active.response_id:
                    previous = active.create.get("previous_response_id")
                    if isinstance(previous, str):
                        frame["previous_response_id"] = previous
                    else:
                        frame.pop("previous_response_id", None)
                self._run.active = None
            return True
        finally:
            del self, frame, active

    async def _drain_terminal(self, active: _Active) -> None:
        event = item = kind = None
        try:
            observe = self._run.request is not None and getattr(
                self._run.request, "_observe_raw_provider_events", False
            )
            while active.terminal is None:
                if observe and len(self._run.recovered) >= _MAX_RESPONSE_ITEMS:
                    raise ValueError("Hosted reconciliation exceeds its retained frame limit")
                event = await self._read(
                    active, budget=self._run.recovered_budget if observe else None
                )
                kind = event.get("type")
                if kind in _TERMINAL:
                    self._terminal(active, event)
                elif kind == "response.output_item.done":
                    item = event.get("item")
                    if (
                        isinstance(item, dict)
                        and item.get("type") == "function_call"
                        and item.get("call_id") not in active.emitted
                    ):
                        raise ValueError("Closed hosted response contains an unadmitted call")
                elif kind in {"response.inject.failed", "response.error", "error"}:
                    raise ValueError("Closed hosted response did not settle")
                if observe:
                    self._run.recovered.append(_ResponsesWebSocketFrame(event, raw=True))
                if kind == "response.output_item.done" and isinstance(item, dict):
                    self._recover_program_output(active, item)
            for item in active.queued:
                self._recover_program_output(active, item)
        except BaseException as error:
            _clear_error_frames(error)
            raise
        finally:
            del self, active, event, item, kind

    def _recover_program_output(self, active: _Active, item: dict[str, Any]) -> None:
        if item.get("type") == "program_output":
            call_id = item.get("call_id")
            if isinstance(call_id, str) and call_id in active.program_parents:
                if len(self._run.recovered) >= _MAX_RESPONSE_ITEMS:
                    raise ValueError("Hosted reconciliation exceeds its retained frame limit")
                frame = {"type": "response.output_item.done", "item": item}
                budget = self._run.recovered_budget
                budget.remaining -= _check_socket_json_structure(
                    json.dumps(frame), budget.remaining
                )
                self._record_program(active, item)
                self._run.recovered.append(_ResponsesWebSocketFrame(frame))

    def _record_program(self, active: _Active, item: dict[str, Any]) -> None:
        if len(active.emitted) + active.passive_count >= _MAX_RESPONSE_ITEMS:
            raise ValueError("Hosted response exceeds the native output-item limit")
        call_id = item.get("call_id")
        if item["type"] == "program" and isinstance(call_id, str):
            active.program_parents.add(call_id)
        active.passive_count += 1
        active.passive[_program_key(item)] += 1

    async def _reconcile_terminal(self, active: _Active) -> None:
        try:
            await _limited(self._drain_terminal(active), 1)
        except BaseException as error:
            self._run.retired = True
            if isinstance(error, Exception) and not isinstance(error, ValueError):
                raise ValueError("Closed hosted response has no genuine terminal") from error
            raise
        finally:
            del self, active

    async def send(self, payload: str) -> None:
        frame = active = items = ids = None
        try:
            frame = json.loads(payload)
            frame.pop("stream", None)
            frame.pop("betas", None)
            active = self._run.active
            if active is None:
                await self._create(frame)
                return
            items, ids = self._continuation(active, frame)
            if active.terminal is not None:
                await self._fallback(active, frame, items)
                return
            active.size += len(json.dumps(items, allow_nan=False).encode("utf-8"))
            if active.size > _MAX_RESPONSE_BYTES:
                raise ValueError("Hosted response exceeds the native retained-byte limit")
            active.continuation = frame
            active.injection_input = items
            active.injecting = ids
            await self._send(
                {"type": "response.inject", "response_id": active.response_id, "input": items}
            )
        finally:
            del self, payload, frame, active, items, ids

    async def _read(
        self, active: _Active, *, budget: _SocketJsonBudget | None = None
    ) -> dict[str, Any]:
        payload = event = None
        try:
            payload = await self._socket.recv()
            remaining = _MAX_RESPONSE_BYTES - active.size
            limit = remaining if self._max_size is None else min(remaining, self._max_size)
            frame_size = _socket_frame_size(payload, limit)
            event = _decode_socket_frame(payload, self._max_size, budget=budget)
            active.size += frame_size
            if "response" in event:
                _provider_object(event["response"], "Hosted Responses response must be an object")
            return event
        finally:
            del self, active, payload, event

    async def recv(self) -> _ResponsesWebSocketFrame:
        event = response = saved = item = active = calls = supported = None
        acknowledged_call = None
        try:
            if self._run.recovered:
                recovered = self._run.recovered.popleft()
                if not self._run.recovered:
                    self._run.recovered_budget = _SocketJsonBudget()
                return recovered
            while not self._queue:
                active = self._run.active
                if active is None:
                    raise RuntimeError("Hosted response has no active owner")
                event = await self._read(active)
                if self._run.request is not None and getattr(
                    self._run.request, "_observe_raw_provider_events", False
                ):
                    self._queue.append(_ResponsesWebSocketFrame(event, raw=True))
                kind = event.get("type")
                if kind == "response.created":
                    response = event.get("response")
                    if (
                        not isinstance(response, dict)
                        or not isinstance(response.get("id"), str)
                        or not response["id"]
                    ):
                        raise ValueError("Hosted response requires a response ID")
                    active.template = deepcopy(response)
                    active.response_id = response["id"]
                    self._enqueue(event)
                elif kind == "response.output_item.done":
                    item = event.get("item")
                    if isinstance(item, dict) and (
                        item.get("type") in _PROGRAM_ITEMS
                        or item.get("type") == "function_call"
                        and isinstance(item.get("call_id"), str)
                        and item["call_id"] not in active.emitted
                        and item["call_id"] not in active.pending
                    ):
                        if item["type"] == "function_call":
                            active.pending.add(item["call_id"])
                        self._queue_item(active, item)
                elif kind == "response.inject.created":
                    if active.injecting is not None:
                        acknowledged_call = None
                        for item in active.injection_input or ():
                            if item["type"] == "function_call_output":
                                acknowledged_call = item["call_id"]
                                active.acknowledged_outputs[acknowledged_call] = item
                            else:
                                active.acknowledged_user_items[item["id"]] = (
                                    acknowledged_call,
                                    item,
                                )
                        active.pending.difference_update(active.injecting)
                        active.injection_input = None
                        active.injecting = None
                elif (
                    kind == "response.inject.failed"
                    and isinstance(event.get("error"), dict)
                    and event["error"].get("code") == "response_already_completed"
                ):
                    active.fallback_input = active.injection_input
                    active.pending.difference_update(active.injecting or ())
                    active.injecting = None
                elif kind in {"response.inject.failed", "response.error", "error"}:
                    raise RuntimeError("Hosted response rejected the provider operation")
                elif kind in _TERMINAL:
                    self._terminal(active, event)
                if (
                    active.terminal is not None
                    and active.fallback_input
                    and active.injecting is None
                ):
                    if active.continuation is None:
                        raise ValueError("Hosted fallback requires its original continuation")
                    for item in active.queued:
                        if item.get("type") in _PROGRAM_ITEMS:
                            self._record_program(active, item)
                            self._enqueue({"type": "response.output_item.done", "item": item})
                    await self._fallback(active, active.continuation, active.fallback_input)
                    continue
                if active.queued and active.injecting is None:
                    if active.template is None or active.response_id is None:
                        raise ValueError("Hosted function call preceded response creation")
                    calls, active.queued = active.queued, []
                    has_call = False
                    for index, item in enumerate(calls):
                        if item.get("type") in _PROGRAM_ITEMS:
                            self._record_program(active, item)
                        else:
                            active.emitted.add(item["call_id"])
                            has_call = True
                        self._enqueue(
                            {
                                "type": "response.output_item.done",
                                "output_index": index,
                                "item": item,
                            }
                        )
                    if has_call:
                        self._enqueue(
                            {
                                "type": "response.completed",
                                "response": {
                                    **active.template,
                                    "id": active.response_id,
                                    "status": "completed",
                                    "output": calls,
                                    "usage": None,
                                },
                            }
                        )
                        continue
                if active.terminal is not None and active.injecting is None:
                    saved = active.terminal
                    response = saved["response"]
                    # Program occurrences have already been queued in wire order.
                    supported = [
                        item
                        for item in response.get("output", [])
                        if _stable(item)
                        and item.get("type") not in _PROGRAM_ITEMS
                        and not (
                            item.get("type") == "function_call"
                            and item.get("call_id") in active.emitted
                        )
                    ]
                    self._run.active = None
                    for index, item in enumerate(supported):
                        self._enqueue(
                            {
                                "type": "response.output_item.done",
                                "output_index": index,
                                "item": item,
                            }
                        )
                    self._enqueue({**saved, "response": {**response, "output": supported}})
            return self._queue.popleft()

        finally:
            del event, response, saved, item, active, calls, supported, self

    def _terminal(self, active: _Active, event: dict[str, Any]) -> None:
        response = output = usage = None
        try:
            if active.terminal_reported:
                raise ValueError("Hosted response received more than one terminal")
            response = _provider_object(
                event.get("response"), "Hosted terminal response must be an object"
            )
            output = response.get("output", [])
            if not isinstance(output, list):
                raise ValueError("Hosted terminal output must contain objects")
            if len(output) > _MAX_RESPONSE_ITEMS:
                raise ValueError("Hosted response exceeds the native output-item limit")
            if not all(isinstance(item, dict) for item in output):
                raise ValueError("Hosted terminal output must contain objects")
            usage = _hosted_usage(response.get("usage"))
            self._account(
                {
                    "operation": "terminal",
                    "lineage": active.lineage,
                    **({"usage": usage} if usage is not None else {}),
                }
            )
            active.terminal_reported = True
            active.terminal = deepcopy(event)
            accounted = active.passive + Counter(
                _program_key(item) for item in active.queued if item.get("type") in _PROGRAM_ITEMS
            )
            for item in output:
                if item.get("type") in _PROGRAM_ITEMS:
                    key = _program_key(item)
                    if accounted[key]:
                        accounted[key] -= 1
                    else:
                        self._queue_item(active, item)
        finally:
            del self, active, event, response, output, usage

    def _queue_item(self, active: _Active, item: dict[str, Any]) -> None:
        if len(active.emitted) + active.passive_count + len(active.queued) >= _MAX_RESPONSE_ITEMS:
            raise ValueError("Hosted response exceeds the native output-item limit")
        active.queued.append(deepcopy(item))

    def _enqueue(self, event: dict[str, Any]) -> None:
        self._queue.append(_ResponsesWebSocketFrame(event))

    async def close(self) -> None:
        result = self._socket.close()
        if inspect.isawaitable(result):
            await result


_preserve_module_identity(globals(), "agents.models")
