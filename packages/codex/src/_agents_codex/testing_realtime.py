"""Deterministic canonical-frame I/O for the real native realtime owner.

Recorded frames are explicit test data. Use synthetic prompts and payloads.
Connection snapshots never retain URLs, headers, or credentials.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ._public import preserve_module_identity as _preserve_module_identity
from .realtime_adapter import RealtimeAdapterContext

RealtimeFrame = dict[str, Any]
RealtimeMatcher = str | RealtimeFrame | Callable[[RealtimeFrame], bool]
RealtimeEmission = Iterable[RealtimeFrame] | Callable[[RealtimeFrame], Iterable[RealtimeFrame]]


@dataclass(frozen=True)
class RealtimeStep:
    """An expected outbound type, exact frame, or predicate and its incoming frames."""

    expect: RealtimeMatcher
    emit: RealtimeEmission = ()
    error: Exception | None = None


@dataclass(frozen=True)
class RealtimeConnectCall:
    model: str | None
    headers_provided: bool


class ScriptedRealtimeAdapter:
    """Single-connection native transport fixture; Core still interprets all frames.

    ``emit`` may derive an ACK from the outbound frame, for example
    ``RealtimeStep("session.update", lambda f: [{"type": "session.updated",
    "session": {**f["session"], "id": "fixture"}}])``.
    The incoming iterator stays open until close, so script exhaustion cannot
    race the native owner's processing of the last frame. Call assert_complete
    after closing the native session. This fixture never contacts a provider.
    """

    def __init__(
        self,
        steps: Iterable[RealtimeStep] = (),
        *,
        connect_events: Iterable[RealtimeFrame] = (),
        connect_error: Exception | None = None,
        close_error: Exception | None = None,
        strict: bool = True,
    ) -> None:
        self._steps = [
            RealtimeStep(
                deepcopy(step.expect) if isinstance(step.expect, dict) else step.expect,
                step.emit if callable(step.emit) else tuple(deepcopy(list(step.emit))),
                step.error,
            )
            for step in steps
        ]
        self._index = 0
        self._initial = deepcopy(list(connect_events))
        self._connect_error = connect_error
        self._close_error = close_error
        self._strict = strict
        self._sent: list[RealtimeFrame] = []
        self._calls: list[RealtimeConnectCall] = []
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._closed = False
        self._failure: AssertionError | None = None

    @property
    def sent_events(self) -> tuple[RealtimeFrame, ...]:
        return tuple(deepcopy(self._sent))

    @property
    def connect_calls(self) -> tuple[RealtimeConnectCall, ...]:
        return tuple(self._calls)

    @property
    def remaining_steps(self) -> int:
        return len(self._steps) - self._index

    async def connect(self, context: RealtimeAdapterContext) -> ScriptedRealtimeAdapter:
        if self._calls or self._closed:
            raise RuntimeError("Scripted realtime adapter supports one connection")
        self._calls.append(RealtimeConnectCall(context.model, bool(context.headers)))
        if self._connect_error is not None:
            raise self._connect_error
        self.emit(*self._initial)
        return self

    @property
    def incoming(self) -> AsyncIterator[str]:
        return self._incoming()

    async def _incoming(self) -> AsyncIterator[str]:
        while (frame := await self._queue.get()) is not None:
            yield frame

    def emit(self, *frames: RealtimeFrame) -> None:
        """Inject canonical provider frames without interpreting them."""
        if self._closed:
            raise RuntimeError("Scripted realtime adapter is closed")
        encoded = [json.dumps(frame, allow_nan=False) for frame in frames]
        for frame in encoded:
            self._queue.put_nowait(frame)

    async def send(self, frame: str) -> None:
        if self._closed or not self._calls:
            raise RuntimeError("Scripted realtime adapter is not connected")
        actual = json.loads(frame)
        if not isinstance(actual, dict):
            raise TypeError("Expected a canonical JSON object")
        self._sent.append(deepcopy(actual))
        step = self._steps[self._index] if self.remaining_steps else None
        matched = step is not None and (
            step.expect(deepcopy(actual))
            if callable(step.expect)
            else actual.get("type") == step.expect
            if isinstance(step.expect, str)
            else actual == step.expect
        )
        if not matched:
            if not self._strict:
                return
            self._failure = AssertionError(f"Unexpected realtime send at step {self._index}")
            raise self._failure
        assert step is not None
        self._index += 1
        emitted = step.emit(deepcopy(actual)) if callable(step.emit) else step.emit
        self.emit(*emitted)
        if step.error is not None:
            raise step.error

    async def aclose(self) -> None:
        if self._close_error is not None:
            error, self._close_error = self._close_error, None
            raise error
        if not self._closed:
            self._closed = True
            self._queue.put_nowait(None)

    def assert_complete(self) -> None:
        """Fail for unexpected sends even if the native transport consumed the error."""
        if self._failure is not None:
            raise self._failure
        if self.remaining_steps:
            raise AssertionError(f"{self.remaining_steps} unconsumed realtime steps")


_preserve_module_identity(globals(), "agents.testing")
