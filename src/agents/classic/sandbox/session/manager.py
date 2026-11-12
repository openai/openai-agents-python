from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Sequence
from typing import TYPE_CHECKING
from uuid import UUID

from ...logger import log_tool_action_error
from ..errors import OpName
from .events import EventPayloadPolicy, SandboxSessionEvent, SandboxSessionFinishEvent
from .sinks import ChainedSink, EventSink, SandboxSessionBoundSink
from .utils import _safe_decode

if TYPE_CHECKING:
    from .base_sandbox_session import BaseSandboxSession

logger = logging.getLogger(__name__)


class Instrumentation:
    """Deliver sandbox audit events to configured sinks with per-sink payload policies."""

    def __init__(
        self,
        *,
        sinks: Sequence[EventSink] | None = None,
        payload_policy: EventPayloadPolicy | None = None,
        payload_policy_by_op: dict[OpName, EventPayloadPolicy] | None = None,
        max_pending_events: int | None = None,
        delivery_timeout_s: float | None = None,
    ) -> None:
        if max_pending_events is not None and (
            type(max_pending_events) is not int or max_pending_events <= 0
        ):
            raise ValueError("max_pending_events must be a positive integer or None")
        if delivery_timeout_s is not None and (
            not math.isfinite(delivery_timeout_s) or delivery_timeout_s <= 0
        ):
            raise ValueError("delivery_timeout_s must be positive and finite or None")
        self.max_pending_events = max_pending_events
        self.delivery_timeout_s = delivery_timeout_s
        self._sinks: list[EventSink] = list(sinks or [])
        self.payload_policy = payload_policy if payload_policy is not None else EventPayloadPolicy()
        self.payload_policy_by_op = dict(payload_policy_by_op or {})
        self._tasks: dict[asyncio.Task[None], tuple[EventSink, UUID]] = {}

    @property
    def sinks(self) -> list[EventSink]:
        return list(self._sinks)

    def add_sink(self, sink: EventSink) -> None:
        self._sinks.append(sink)

    def remove_sink(self, sink: EventSink) -> bool:
        retained = [current for current in self._sinks if current is not sink]
        if len(retained) == len(self._sinks):
            return False
        self._sinks = retained
        for task, (owner, _) in tuple(self._tasks.items()):
            if owner is sink:
                task.cancel()
        return True

    def _bind_session(self, session: BaseSandboxSession) -> None:
        for sink in self._sinks:
            selected = sink.sinks if isinstance(sink, ChainedSink) else [sink]
            for child in selected:
                if isinstance(child, SandboxSessionBoundSink):
                    child.bind(session)

    async def _finish_session(self, session_id: UUID) -> None:
        await self.flush()

    async def emit(self, event: SandboxSessionEvent) -> None:
        for sink in self._sinks:
            if isinstance(sink, ChainedSink):
                for inner in sink.sinks:
                    policy = self._policy_for(event.op, inner)
                    per_sink_event = self._apply_policy(event, policy)
                    # ChainedSink promises in-order delivery; ensure each sink completes
                    # before moving on, regardless of inner sink.mode.
                    await self._deliver_chained(inner, per_sink_event)
            else:
                policy = self._policy_for(event.op, sink)
                per_sink_event = self._apply_policy(event, policy)
                await self._deliver(sink, per_sink_event)

    async def flush(self) -> None:
        pending = tuple(self._tasks)
        if not pending:
            return
        if self.delivery_timeout_s is None:
            await asyncio.gather(*pending, return_exceptions=True)
        else:
            _, remaining = await asyncio.wait(pending, timeout=self.delivery_timeout_s)
            for task in remaining:
                task.cancel()

    def _policy_for(self, op: OpName, sink: EventSink) -> EventPayloadPolicy:
        # Merge semantics: default -> per-op overrides -> per-sink overrides.
        effective = self.payload_policy.model_copy(deep=True)

        op_policy = self.payload_policy_by_op.get(op)
        if op_policy is not None:
            effective = effective.model_copy(update=self._overrides(op_policy))

        sink_policy = getattr(sink, "payload_policy", None)
        if sink_policy is not None:
            effective = effective.model_copy(update=self._overrides(sink_policy))

        return effective

    def _overrides(self, policy: EventPayloadPolicy) -> dict[str, object]:
        # Only override fields explicitly set by the user.
        return {name: getattr(policy, name) for name in policy.model_fields_set}

    def _apply_policy(
        self, event: SandboxSessionEvent, policy: EventPayloadPolicy
    ) -> SandboxSessionEvent:
        # Clone per sink so we can redact/augment fields without affecting other sinks.
        out = event.model_copy(deep=True)

        # Generic stream-length metadata redaction.
        if not policy.include_write_len and "bytes" in out.data:
            out.data.pop("bytes", None)

        # Exec output redaction/formatting.
        if isinstance(out, SandboxSessionFinishEvent):
            if not policy.include_exec_output:
                out.stdout = None
                out.stderr = None
                out.stdout_bytes = None
                out.stderr_bytes = None
            else:
                if out.stdout_bytes is not None:
                    out.stdout = _safe_decode(out.stdout_bytes, max_chars=policy.max_stdout_chars)
                if out.stderr_bytes is not None:
                    out.stderr = _safe_decode(out.stderr_bytes, max_chars=policy.max_stderr_chars)

        return out

    async def _deliver(self, sink: EventSink, event: SandboxSessionEvent) -> None:
        async def _run() -> None:
            await sink.handle(event)

        if sink.mode == "sync":
            try:
                await _run()
            except Exception as exc:
                self._handle_sink_error(sink, event, exc)
        elif sink.mode == "async":
            if sink.on_error == "raise":
                await _run()
                return
            self._schedule(sink, event, force_no_raise=False)
        elif sink.mode == "best_effort":
            self._schedule(sink, event, force_no_raise=True)
        else:
            raise AssertionError(f"unknown sink.mode: {sink.mode!r}")

    def _schedule(
        self, sink: EventSink, event: SandboxSessionEvent, *, force_no_raise: bool
    ) -> None:
        if self.max_pending_events is not None and len(self._tasks) >= self.max_pending_events:
            return

        async def deliver() -> None:
            try:
                await sink.handle(event)
            except Exception as exc:
                self._handle_sink_error(sink, event, exc, force_no_raise=force_no_raise)

        task = asyncio.create_task(deliver())
        self._tasks[task] = (sink, event.session_id)
        timer = (
            asyncio.get_running_loop().call_later(self.delivery_timeout_s, task.cancel)
            if self.delivery_timeout_s is not None
            else None
        )

        def finished(completed: asyncio.Task[None]) -> None:
            self._tasks.pop(completed, None)
            if timer is not None:
                timer.cancel()

        task.add_done_callback(finished)

    async def _deliver_chained(self, sink: EventSink, event: SandboxSessionEvent) -> None:
        """
        Deliver an event to a sink as part of a ChainedSink group.

        The ChainedSink contract is "run in order", which implies later sinks should not
        observe side effects before earlier sinks complete. To uphold that, we always
        await completion here (ignoring sink.mode scheduling).
        """
        try:
            await sink.handle(event)
        except Exception as exc:
            force_no_raise = sink.mode == "best_effort"
            self._handle_sink_error(sink, event, exc, force_no_raise=force_no_raise)

    def _handle_sink_error(
        self,
        sink: EventSink,
        event: SandboxSessionEvent,
        exc: Exception,
        *,
        force_no_raise: bool = False,
    ) -> None:
        if force_no_raise or sink.on_error in ("log", "ignore"):
            if sink.on_error == "log":
                log_tool_action_error(
                    logger,
                    "Sandbox event sink failed (ignored)",
                    exc,
                    diagnostic_extra=lambda: {"sink_type": type(sink).__name__},
                )
            return
        raise RuntimeError(
            "sandbox event sink failed: "
            f"{type(sink).__name__} while handling event {event.event_id}"
        )
