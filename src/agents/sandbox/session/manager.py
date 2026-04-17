from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from ..errors import OpName
from .events import EventPayloadPolicy, SandboxSessionEvent, SandboxSessionFinishEvent
from .sinks import ChainedSink, EventSink
from .utils import _safe_decode

logger = logging.getLogger(__name__)


class Instrumentation:
    """Deliver sandbox audit events to configured sinks with per-sink payload policies."""

    def __init__(
        self,
        *,
        sinks: Sequence[EventSink] | None = None,
        payload_policy: EventPayloadPolicy | None = None,
        payload_policy_by_op: dict[OpName, EventPayloadPolicy] | None = None,
    ) -> None:
        self._sinks: list[EventSink] = list(sinks or [])
        self.payload_policy = payload_policy or EventPayloadPolicy()
        self.payload_policy_by_op = payload_policy_by_op or {}
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def sinks(self) -> list[EventSink]:
        return list(self._sinks)

    def add_sink(self, sink: EventSink) -> None:
        self._sinks.append(sink)

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
        await asyncio.gather(*pending, return_exceptions=True)

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
            except Exception:
                self._handle_sink_error(sink, event)
        elif sink.mode == "async":
            if sink.on_error == "raise":
                await _run()
                return

            async def _task() -> None:
                try:
                    await _run()
                except Exception:
                    self._handle_sink_error(sink, event)

            task = asyncio.create_task(_task())
            # Track background deliveries so the task is kept alive and can be discarded once done.
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif sink.mode == "best_effort":

            async def _task() -> None:
                try:
                    await _run()
                except Exception:
                    self._handle_sink_error(sink, event, force_no_raise=True)

            task = asyncio.create_task(_task())
            # Same bookkeeping as async mode, but failures are always swallowed after logging.
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        else:
            raise AssertionError(f"unknown sink.mode: {sink.mode!r}")

    async def _deliver_chained(self, sink: EventSink, event: SandboxSessionEvent) -> None:
        """
        Deliver an event to a sink as part of a ChainedSink group.

        The ChainedSink contract is "run in order", which implies later sinks should not
        observe side effects before earlier sinks complete. To uphold that, we always
        await completion here (ignoring sink.mode scheduling).
        """
        try:
            await sink.handle(event)
        except Exception:
            force_no_raise = sink.mode == "best_effort"
            self._handle_sink_error(sink, event, force_no_raise=force_no_raise)

    def _handle_sink_error(
        self, sink: EventSink, event: SandboxSessionEvent, *, force_no_raise: bool = False
    ) -> None:
        if force_no_raise or sink.on_error in ("log", "ignore"):
            if sink.on_error == "log":
                logger.exception("sandbox event sink failed (ignored): %s", type(sink).__name__)
            return
        raise RuntimeError(
            "sandbox event sink failed: "
            f"{type(sink).__name__} while handling event {event.event_id}"
        )
