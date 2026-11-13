"""Provider beta response boundaries over the native run-owned socket adapter."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from traceback import clear_frames
from typing import Any, cast
from weakref import WeakKeyDictionary

from ._compat import cancellation_requested
from ._public import preserve_module_identity as _preserve_module_identity
from ._responses_websocket import (
    ResponsesSocketFactory,
    ResponsesSocketOptions,
    ResponsesWebSocketModel,
    ResponsesWebSocketOptions,
    _connect,
    _decode_socket_frame,
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


@dataclass
class _Active:
    create: dict[str, Any]
    lineage: str
    terminal_reported: bool = False
    continuation: dict[str, Any] | None = None
    injection_input: list[dict[str, Any]] | None = None
    fallback_input: list[dict[str, Any]] | None = None
    acknowledged_outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    template: dict[str, Any] | None = None
    response_id: str | None = None
    pending: set[str] = field(default_factory=set)
    emitted: set[str] = field(default_factory=set)
    queued: list[dict[str, Any]] = field(default_factory=list)
    injecting: set[str] | None = None
    terminal: dict[str, Any] | None = None
    size: int = 0


@dataclass
class _Run:
    active: _Active | None = None
    request: ModelRequest | None = None
    account: Callable[[dict[str, Any]], str | None] | None = None


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
            selected = deepcopy(options)
            headers = selected["headers"]
            betas: list[str] = []
            for key in tuple(headers):
                if key.lower() == "openai-beta":
                    betas.extend(
                        value.strip() for value in headers.pop(key).split(",") if value.strip()
                    )
            headers["OpenAI-Beta"] = ",".join(dict.fromkeys([*betas, "responses_multi_agent=v1"]))
            return _Socket(
                await factory(request, selected),
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
            clear_frames(error.__traceback__)
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
                terminal_output = completed.get("output", [])
                for item in terminal_output:
                    replay.append(item)
                    if item.get("type") == "function_call":
                        result = acknowledged.pop(item.get("call_id"), None)
                        if result is not None:
                            replay.append(result)
                next_frame["input"] = [*replay, *acknowledged.values(), *outputs]
                original_previous = active.create.get("previous_response_id")
                if isinstance(original_previous, str):
                    next_frame["previous_response_id"] = original_previous
                else:
                    next_frame.pop("previous_response_id", None)
            await self._create(next_frame, active.emitted)
        finally:
            del self, active, frame, outputs, completed, next_frame, replay
            del acknowledged, terminal_output, item, result

    async def send(self, payload: str) -> None:
        frame = active = outputs = ids = None
        try:
            frame = json.loads(payload)
            frame.pop("stream", None)
            frame.pop("betas", None)
            active = self._run.active
            if active is None:
                await self._create(frame)
                return
            outputs = [
                item
                for item in frame.get("input", [])
                if isinstance(item, dict)
                and item.get("type") == "function_call_output"
                and item.get("call_id") in active.pending
            ]
            ids = {item["call_id"] for item in outputs}
            if not active.response_id or not ids or active.pending - ids:
                raise ValueError("Hosted response requires the matching local function outputs")
            if active.terminal is not None:
                await self._fallback(active, frame, outputs)
                return
            active.size += len(json.dumps(outputs, allow_nan=False).encode("utf-8"))
            if active.size > _MAX_RESPONSE_BYTES:
                raise ValueError("Hosted response exceeds the native retained-byte limit")
            active.continuation = frame
            active.injection_input = outputs
            active.injecting = ids
            await self._send(
                {"type": "response.inject", "response_id": active.response_id, "input": outputs}
            )
        finally:
            del self, payload, frame, active, outputs, ids

    async def recv(self) -> _ResponsesWebSocketFrame:
        payload = event = response = output = saved = item = active = calls = supported = None
        try:
            while not self._queue:
                payload = await self._socket.recv()
                active = self._run.active
                if active is None:
                    raise RuntimeError("Hosted response has no active owner")
                active.size += len(
                    payload if isinstance(payload, bytes) else payload.encode("utf-8")
                )
                if active.size > _MAX_RESPONSE_BYTES:
                    raise ValueError("Hosted response exceeds the native retained-byte limit")
                event = _decode_socket_frame(payload, self._max_size)
                if "response" in event:
                    _provider_object(
                        event["response"], "Hosted Responses response must be an object"
                    )
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
                    if (
                        isinstance(item, dict)
                        and item.get("type") == "function_call"
                        and isinstance(item.get("call_id"), str)
                        and item["call_id"] not in active.emitted
                        and item["call_id"] not in active.pending
                    ):
                        if len(active.emitted) + len(active.queued) >= _MAX_RESPONSE_ITEMS:
                            raise ValueError("Hosted response exceeds the native output-item limit")
                        active.pending.add(item["call_id"])
                        active.queued.append(deepcopy(item))
                elif kind == "response.inject.created":
                    if active.injecting is not None:
                        active.acknowledged_outputs.update(
                            (result["call_id"], result) for result in active.injection_input or ()
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
                if (
                    active.terminal is not None
                    and active.fallback_input
                    and active.injecting is None
                ):
                    if active.continuation is None:
                        raise ValueError("Hosted fallback requires its original continuation")
                    await self._fallback(active, active.continuation, active.fallback_input)
                    continue
                if active.queued and active.injecting is None:
                    if active.template is None or active.response_id is None:
                        raise ValueError("Hosted function call preceded response creation")
                    calls, active.queued = active.queued, []
                    active.emitted.update(item["call_id"] for item in calls)
                    for index, item in enumerate(calls):
                        self._enqueue(
                            {
                                "type": "response.output_item.done",
                                "output_index": index,
                                "item": item,
                            }
                        )
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
                elif active.terminal is not None and active.injecting is None:
                    saved = active.terminal
                    response = saved["response"]
                    supported = [
                        item
                        for item in response.get("output", [])
                        if _stable(item)
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
            del payload, event, response, output, saved, item, active, calls, supported, self

    def _enqueue(self, event: dict[str, Any]) -> None:
        self._queue.append(_ResponsesWebSocketFrame(event))

    async def close(self) -> None:
        result = self._socket.close()
        if inspect.isawaitable(result):
            await result


_preserve_module_identity(globals(), "agents.models")
