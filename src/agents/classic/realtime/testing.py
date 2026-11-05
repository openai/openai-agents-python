"""Deterministic Realtime model transport for session tests."""

from __future__ import annotations

import asyncio
import copy
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TypeAlias, cast

from typing_extensions import Required, TypedDict

from ..models._trace import sanitize_url_for_trace
from .config import RealtimeSessionModelSettings
from .model import (
    RealtimeModel,
    RealtimeModelConfig,
    RealtimeModelListener,
    RealtimePlaybackTracker,
)
from .model_events import RealtimeModelErrorEvent, RealtimeModelEvent, RealtimeModelExceptionEvent
from .model_inputs import RealtimeModelSendEvent, RealtimeModelSendSessionUpdate


class RealtimeScriptError(Exception):
    """Base exception for an invalid or incompletely consumed Realtime script."""


class UnexpectedRealtimeSend(RealtimeScriptError):
    """Raised when an outbound event does not match the next scripted step."""

    def __init__(
        self,
        message: str,
        *,
        actual: RealtimeModelSendEvent,
        expected: RealtimeSendMatcher | None,
    ) -> None:
        super().__init__(message)
        self.actual = actual
        self.expected = expected


class UnconsumedRealtimeSteps(RealtimeScriptError):
    """Raised when a test finishes before consuming every configured send step."""

    def __init__(self, message: str, *, remaining_steps: int) -> None:
        super().__init__(message)
        self.remaining_steps = remaining_steps


RealtimeSendMatcher: TypeAlias = (
    RealtimeModelSendEvent | type[RealtimeModelSendEvent] | Callable[[RealtimeModelSendEvent], bool]
)


@dataclass(frozen=True)
class RealtimeStep:
    """One expected outbound event and the normalized inbound events it triggers."""

    expect: RealtimeSendMatcher
    emit: Sequence[RealtimeModelEvent] = field(default_factory=tuple)
    error: Exception | None = None

    def __post_init__(self) -> None:
        frozen_emit = tuple(self.emit)
        if frozen_emit and self.error is not None:
            raise ValueError("A RealtimeStep cannot define both emit events and an error.")
        object.__setattr__(self, "emit", frozen_emit)


class RealtimeConnectCall(TypedDict, total=False):
    """A credential-free snapshot of one Realtime connection call."""

    api_key_provided: Required[bool]
    headers_provided: Required[bool]
    url: str
    initial_model_settings: RealtimeSessionModelSettings
    playback_tracker: RealtimePlaybackTracker
    call_id: str


@dataclass
class _RealtimeDeliveryResult:
    done: asyncio.Future[BaseException | None]
    pending_deliveries: int = 1
    error: BaseException | None = None


@dataclass(frozen=True)
class _QueuedRealtimeDelivery:
    events: Sequence[RealtimeModelEvent]
    error: Exception | None
    result: _RealtimeDeliveryResult
    committed_close_calls: int


class ScriptedRealtimeModel(RealtimeModel):
    """An in-memory, listener-based Realtime transport with deterministic send steps."""

    def __init__(
        self,
        steps: Iterable[RealtimeStep] = (),
        *,
        connect_events: Iterable[RealtimeModelEvent] = (),
        connect_error: Exception | None = None,
        close_error: Exception | None = None,
        strict: bool = True,
    ) -> None:
        connect_event_values = tuple(connect_events)
        if connect_event_values and connect_error is not None:
            raise ValueError(
                "A ScriptedRealtimeModel cannot define both connect events and a connect error."
            )
        self._steps = [_snapshot_realtime_step(step) for step in steps]
        self._connect_events = tuple(_snapshot_model_event(event) for event in connect_event_values)
        self._connect_error = connect_error
        self._close_error = close_error
        self._strict = strict
        self._listeners: list[RealtimeModelListener] = []
        self._send_lock = asyncio.Lock()
        self._delivery_queue: deque[_QueuedRealtimeDelivery] = deque()
        self._delivery_worker: asyncio.Task[None] | None = None
        self._active_delivery_result: _RealtimeDeliveryResult | None = None
        self._connect_calls: list[RealtimeConnectCall] = []
        self._sent_events: list[RealtimeModelSendEvent] = []
        self.connected = False
        self.closed = False
        self.close_calls = 0

    @property
    def listeners(self) -> tuple[RealtimeModelListener, ...]:
        """Return the currently registered listeners."""
        return tuple(self._listeners)

    @property
    def connect_calls(self) -> tuple[RealtimeConnectCall, ...]:
        """Return detached snapshots of recorded connection calls."""
        return tuple(_clone_connect_call(call) for call in self._connect_calls)

    @property
    def sent_events(self) -> tuple[RealtimeModelSendEvent, ...]:
        """Return detached snapshots of recorded outbound events."""
        return tuple(self._snapshot_send_event(event) for event in self._sent_events)

    @property
    def remaining_steps(self) -> int:
        """Return the number of expected outbound sends that remain."""
        return len(self._steps)

    async def connect(self, options: RealtimeModelConfig) -> None:
        if self.connected or self._delivery_worker is not None:
            raise AssertionError("Already connected")
        self._connect_calls.append(_snapshot_connect_call(options))
        if self._connect_error is not None:
            raise self._connect_error
        self.connected = True
        self.closed = False
        try:
            async with self._send_lock:
                self._ensure_emit_allowed()
                event_snapshots = tuple(
                    _snapshot_model_event(event) for event in self._connect_events
                )
                result, reentrant = self._queue_delivery_locked(events=event_snapshots)
            await self._finish_queued_delivery(result, reentrant)
        except BaseException:
            self.connected = False
            self.closed = True
            raise

    def add_listener(self, listener: RealtimeModelListener) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_listener(self, listener: RealtimeModelListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    async def send_event(self, event: RealtimeModelSendEvent) -> None:
        result, reentrant = await self._commit_send(event)
        await self._finish_queued_delivery(result, reentrant)

    async def send_event_if(
        self,
        event: RealtimeModelSendEvent,
        send_if: Callable[[], bool],
    ) -> bool:
        async with self._send_lock:
            self._ensure_sendable()
            if not send_if():
                return False
            result, reentrant = self._commit_send_locked(event)
        await self._finish_queued_delivery(result, reentrant)
        return True

    async def emit(self, *events: RealtimeModelEvent) -> None:
        """Deliver normalized model events to all current listeners in order."""
        async with self._send_lock:
            self._ensure_emit_allowed()
            event_snapshots = tuple(_snapshot_model_event(event) for event in events)
            result, reentrant = self._queue_delivery_locked(events=event_snapshots)
        await self._finish_queued_delivery(result, reentrant)

    async def _broadcast_events(
        self,
        events: Sequence[RealtimeModelEvent],
        *,
        committed_close_calls: int,
    ) -> None:
        for event in events:
            listeners = tuple(self._listeners)
            for listener in listeners:
                if self.closed or self.close_calls != committed_close_calls:
                    return
                await listener.on_event(event)

    async def close(self) -> None:
        self.close_calls += 1
        if self.closed:
            return
        self.connected = False
        self.closed = True
        if self._close_error is not None:
            raise self._close_error

    def assert_complete(self) -> None:
        """Raise when expected outbound send steps remain unconsumed."""
        if self._steps:
            raise UnconsumedRealtimeSteps(
                f"{len(self._steps)} scripted Realtime step(s) were not consumed.",
                remaining_steps=len(self._steps),
            )

    async def _commit_send(
        self, event: RealtimeModelSendEvent
    ) -> tuple[_RealtimeDeliveryResult, bool]:
        async with self._send_lock:
            self._ensure_sendable()
            return self._commit_send_locked(event)

    def _commit_send_locked(
        self, event: RealtimeModelSendEvent
    ) -> tuple[_RealtimeDeliveryResult, bool]:
        event_snapshot = self._snapshot_send_event(event)
        step = self._pop_matching_step(event, actual_snapshot=event_snapshot)
        self._sent_events.append(event_snapshot)
        return self._queue_delivery_locked(
            events=step.emit if step is not None else (),
            error=step.error if step is not None else None,
        )

    def _queue_delivery_locked(
        self,
        *,
        events: Sequence[RealtimeModelEvent],
        error: Exception | None = None,
    ) -> tuple[_RealtimeDeliveryResult, bool]:
        current_task = asyncio.current_task()
        if current_task is None:
            raise RuntimeError("A scripted Realtime send requires an active asyncio task.")
        active_result = self._active_delivery_result
        if current_task is self._delivery_worker and active_result is not None:
            reentrant = True
            result = active_result
            result.pending_deliveries += 1
        else:
            reentrant = False
            result = _RealtimeDeliveryResult(done=asyncio.get_running_loop().create_future())
        self._delivery_queue.append(
            _QueuedRealtimeDelivery(
                events=tuple(events),
                error=error,
                result=result,
                committed_close_calls=self.close_calls,
            )
        )
        self._ensure_delivery_worker_locked()
        return result, reentrant

    async def _finish_queued_delivery(
        self,
        result: _RealtimeDeliveryResult,
        reentrant: bool,
    ) -> None:
        if reentrant:
            return
        error = await asyncio.shield(result.done)
        if error is not None:
            raise error

    def _ensure_delivery_worker_locked(self) -> None:
        if self._delivery_worker is None or self._delivery_worker.done():
            self._delivery_worker = asyncio.create_task(self._drain_committed_sends())

    async def _drain_committed_sends(self) -> None:
        while True:
            async with self._send_lock:
                if not self._delivery_queue:
                    self._active_delivery_result = None
                    self._delivery_worker = None
                    return
                delivery = self._delivery_queue.popleft()
                self._active_delivery_result = delivery.result
            error = await self._deliver_queued_events(delivery)
            result = delivery.result
            if error is not None and result.error is None:
                result.error = error
            result.pending_deliveries -= 1
            if result.pending_deliveries == 0 and not result.done.done():
                result.done.set_result(result.error)

    async def _deliver_queued_events(
        self, delivery: _QueuedRealtimeDelivery
    ) -> BaseException | None:
        try:
            if delivery.error is not None:
                raise delivery.error
            self._ensure_emit_allowed(delivery.committed_close_calls)
            await self._broadcast_events(
                delivery.events,
                committed_close_calls=delivery.committed_close_calls,
            )
        except BaseException as error:
            return error
        return None

    def _ensure_sendable(self) -> None:
        if not self.connected or self.closed:
            raise RealtimeScriptError(
                "Cannot send an event while the scripted model is disconnected."
            )

    def _ensure_emit_allowed(
        self,
        committed_close_calls: int | None = None,
    ) -> None:
        if (
            not self.connected
            or self.closed
            or (committed_close_calls is not None and self.close_calls != committed_close_calls)
        ):
            raise RealtimeScriptError(
                "Cannot emit events while the scripted model is disconnected."
            )

    def _pop_matching_step(
        self,
        event: RealtimeModelSendEvent,
        *,
        actual_snapshot: RealtimeModelSendEvent,
    ) -> RealtimeStep | None:
        if not self._steps:
            if not self._strict:
                return None
            raise UnexpectedRealtimeSend(
                "Unexpected Realtime send: no scripted steps remain.",
                actual=actual_snapshot,
                expected=None,
            )
        step = self._steps[0]
        if not _matches(step.expect, event):
            if not self._strict:
                return None
            raise UnexpectedRealtimeSend(
                "Unexpected Realtime send: event did not match the next scripted expectation.",
                actual=actual_snapshot,
                expected=_snapshot_realtime_expectation(step.expect),
            )
        return self._steps.pop(0)

    @staticmethod
    def _snapshot_send_event(event: RealtimeModelSendEvent) -> RealtimeModelSendEvent:
        return _snapshot_send_event(event)


def _snapshot_realtime_step(step: RealtimeStep) -> RealtimeStep:
    return RealtimeStep(
        expect=_snapshot_realtime_expectation(step.expect),
        emit=tuple(_snapshot_model_event(event) for event in step.emit),
        error=step.error,
    )


def _snapshot_realtime_expectation(expectation: RealtimeSendMatcher) -> RealtimeSendMatcher:
    if isinstance(expectation, type) or callable(expectation):
        return expectation
    return _snapshot_send_event(expectation)


def _snapshot_send_event(event: RealtimeModelSendEvent) -> RealtimeModelSendEvent:
    if isinstance(event, RealtimeModelSendSessionUpdate):
        return RealtimeModelSendSessionUpdate(
            session_settings=_snapshot_model_settings(event.session_settings)
        )
    return copy.deepcopy(event)


def _snapshot_model_event(event: RealtimeModelEvent) -> RealtimeModelEvent:
    if isinstance(event, RealtimeModelExceptionEvent):
        return RealtimeModelExceptionEvent(
            exception=event.exception,
            context=copy.deepcopy(event.context),
        )
    if isinstance(event, RealtimeModelErrorEvent) and isinstance(event.error, Exception):
        return RealtimeModelErrorEvent(error=event.error)
    return copy.deepcopy(event)


def _matches(expectation: RealtimeSendMatcher, event: RealtimeModelSendEvent) -> bool:
    if isinstance(expectation, type):
        return isinstance(event, expectation)
    if callable(expectation):
        return expectation(_snapshot_send_event(event))
    return event == expectation


def _snapshot_connect_call(options: RealtimeModelConfig) -> RealtimeConnectCall:
    snapshot: RealtimeConnectCall = {
        "api_key_provided": "api_key" in options,
        "headers_provided": "headers" in options,
    }
    if "url" in options:
        snapshot["url"] = sanitize_url_for_trace(options["url"])
    if "initial_model_settings" in options:
        snapshot["initial_model_settings"] = _snapshot_model_settings(
            options["initial_model_settings"]
        )
    if "playback_tracker" in options:
        snapshot["playback_tracker"] = options["playback_tracker"]
    if "call_id" in options:
        snapshot["call_id"] = options["call_id"]
    return snapshot


def _clone_connect_call(call: RealtimeConnectCall) -> RealtimeConnectCall:
    snapshot: RealtimeConnectCall = {
        "api_key_provided": call["api_key_provided"],
        "headers_provided": call["headers_provided"],
    }
    if "url" in call:
        snapshot["url"] = call["url"]
    if "initial_model_settings" in call:
        snapshot["initial_model_settings"] = _snapshot_model_settings(
            call["initial_model_settings"]
        )
    if "playback_tracker" in call:
        snapshot["playback_tracker"] = call["playback_tracker"]
    if "call_id" in call:
        snapshot["call_id"] = call["call_id"]
    return snapshot


def _snapshot_model_settings(
    settings: RealtimeSessionModelSettings,
) -> RealtimeSessionModelSettings:
    snapshot = cast(
        RealtimeSessionModelSettings,
        copy.deepcopy(
            {key: value for key, value in settings.items() if key not in {"tools", "handoffs"}}
        ),
    )
    if "tools" in settings:
        snapshot["tools"] = list(settings["tools"])
    if "handoffs" in settings:
        snapshot["handoffs"] = list(settings["handoffs"])
    return snapshot


__all__ = [
    "RealtimeConnectCall",
    "RealtimeScriptError",
    "RealtimeStep",
    "ScriptedRealtimeModel",
    "UnconsumedRealtimeSteps",
    "UnexpectedRealtimeSend",
]
