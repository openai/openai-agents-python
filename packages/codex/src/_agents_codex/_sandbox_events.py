"""Native lifecycle policy over the shared sandbox event dispatcher."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from copy import copy
from uuid import UUID
from weakref import WeakValueDictionary, ref

from agents.classic.sandbox.errors import OpName
from agents.classic.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.classic.sandbox.session.events import (
    EventPayloadPolicy,
    SandboxSessionEvent,
    SandboxSessionFinishEvent,
)
from agents.classic.sandbox.session.manager import Instrumentation as _Instrumentation
from agents.classic.sandbox.session.sandbox_session import SandboxSession
from agents.classic.sandbox.session.sinks import (
    ChainedSink,
    EventSink,
    HttpProxySink,
    JsonlOutboxSink,
    SandboxSessionBoundSink,
    WorkspaceJsonlSink,
)


class _BoundSink(EventSink):
    def __init__(self, source: EventSink) -> None:
        self.source = source
        self.mode = source.mode
        self.on_error = source.on_error
        self.payload_policy = source.payload_policy
        self.name = source.name
        self.sessions: dict[UUID, EventSink] = {}

    def bind(self, session: BaseSandboxSession) -> None:
        if isinstance(self.source, WorkspaceJsonlSink):
            sink: EventSink = WorkspaceJsonlSink(
                workspace_relpath=self.source.workspace_relpath,
                ephemeral=self.source.ephemeral,
                mode=self.mode,
                on_error=self.on_error,
                payload_policy=self.payload_policy,
                flush_every=self.source._flush_every,
                max_bytes=self.source.max_bytes,
            )
        else:
            sink = copy(self.source)
        assert isinstance(sink, SandboxSessionBoundSink)
        sink.bind(session)
        self.sessions[session.state.session_id] = sink

    async def handle(self, event: SandboxSessionEvent) -> None:
        sink = self.sessions.get(event.session_id)
        if sink is not None:
            await sink.handle(event)


class _PhysicalThreadSink(EventSink):
    def __init__(self, source: HttpProxySink | JsonlOutboxSink) -> None:
        self.source = source
        self.mode = source.mode
        self.on_error = source.on_error
        self.payload_policy = source.payload_policy
        self.name = source.name

    async def handle(self, event: SandboxSessionEvent) -> None:
        # Retain the existing dispatch slot until the blocking I/O has joined.
        # Resource cleanup cancels optional delivery without awaiting this join.
        physical = asyncio.create_task(self.source.handle(event))
        cancelled = False
        while not physical.done():
            try:
                await asyncio.shield(physical)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        if cancelled:
            if not physical.cancelled():
                physical.exception()
            raise asyncio.CancelledError
        physical.result()


class _SinkChain(ChainedSink):
    def __init__(self, source: ChainedSink) -> None:
        self.source = source
        prepared = [_prepare_sink(sink) for sink in source.sinks]
        super().__init__(
            *(
                leaf
                for sink in prepared
                for leaf in (sink.sinks if isinstance(sink, ChainedSink) else [sink])
            )
        )


def _prepare_sink(sink: EventSink) -> EventSink:
    if isinstance(sink, ChainedSink):
        return _SinkChain(sink)
    if isinstance(sink, SandboxSessionBoundSink):
        return _BoundSink(sink)
    if isinstance(sink, (HttpProxySink, JsonlOutboxSink)):
        return _PhysicalThreadSink(sink)
    return sink


def _retire_binding(sink: EventSink, session_id: UUID | None) -> None:
    if isinstance(sink, _BoundSink):
        if session_id is None:
            sink.sessions.clear()
        else:
            sink.sessions.pop(session_id, None)
    elif isinstance(sink, ChainedSink):
        for child in sink.sinks:
            _retire_binding(child, session_id)


class Instrumentation(_Instrumentation):
    """Bind native sandbox headers to their acquired provider resource.

    Provider constructors have been accepting this configuration. Managed
    native environments have also been forwarding builtin exec, stdin and image
    operation headers from the admitted runtime. Provider output and write lengths
    have been requiring explicit payload opt-ins. Paths, arguments and provider
    exception text have been staying excluded. Core operation output has been
    staying unavailable. Capture has been staying within 64 KiB per stream.
    Callbacks have been receiving policy-limited text with raw byte fields excluded.

    Optional delivery has been dropping events at its finite capacity and
    retaining physical HTTP sends until completion. Resource cleanup has been
    cancelling optional delivery without joining observers. Explicit synchronous
    sinks and ordered chains have been retaining awaited delivery and their
    configured error policy.
    """

    def __init__(
        self,
        *,
        sinks: Sequence[EventSink] | None = None,
        payload_policy: EventPayloadPolicy | None = None,
        payload_policy_by_op: dict[OpName, EventPayloadPolicy] | None = None,
        max_pending_events: int = 64,
        delivery_timeout_s: float = 5.0,
    ) -> None:
        if max_pending_events is None or delivery_timeout_s is None:
            raise ValueError("Native lifecycle delivery requires finite limits")
        native_policy = EventPayloadPolicy(include_write_len=False)
        if payload_policy is not None:
            native_policy = native_policy.model_copy(update=self._overrides(payload_policy))
        super().__init__(
            sinks=[_prepare_sink(sink) for sink in sinks or ()],
            payload_policy=native_policy,
            payload_policy_by_op=payload_policy_by_op,
            max_pending_events=max_pending_events,
            delivery_timeout_s=delivery_timeout_s,
        )

        self._resources: WeakValueDictionary[UUID, BaseSandboxSession] = WeakValueDictionary()

    @property
    def sinks(self) -> list[EventSink]:
        return [
            sink.source if isinstance(sink, (_BoundSink, _SinkChain, _PhysicalThreadSink)) else sink
            for sink in self._sinks
        ]

    def _bind_session(self, session: BaseSandboxSession) -> None:
        super()._bind_session(session)
        self._resources[session.state.session_id] = session

    def add_sink(self, sink: EventSink) -> None:
        prepared = _prepare_sink(sink)
        for resource in tuple(self._resources.values()):
            selected = prepared.sinks if isinstance(prepared, ChainedSink) else [prepared]
            for child in selected:
                if isinstance(child, SandboxSessionBoundSink):
                    child.bind(resource)
        super().add_sink(prepared)

    def remove_sink(self, sink: EventSink) -> bool:
        for current in self._sinks:
            source = (
                current.source
                if isinstance(current, (_BoundSink, _SinkChain, _PhysicalThreadSink))
                else current
            )
            if source is sink or current is sink:
                removed = super().remove_sink(current)
                _retire_binding(current, None)
                return removed
        return False

    async def emit(self, event: SandboxSessionEvent) -> None:
        if (
            event.op
            not in (
                "start",
                "stop",
                "shutdown",
                "exec",
                "write_stdin",
                "view_image",
                "read",
                "write",
                "running",
                "resolve_exposed_port",
                "persist_workspace",
                "hydrate_workspace",
            )
            or not self.sinks
        ):
            return
        safe = event.model_copy(update={"data": {}})
        if safe.op == "write":
            length = event.data.get("bytes")
            if type(length) is int and 0 <= length <= 2**53 - 1:
                safe.data = {"bytes": length}
        if safe.op == "resolve_exposed_port":
            key = "server.port" if safe.phase == "finish" else "port"
            port = event.data.get(key)
            if type(port) is int and 1 <= port <= 65535:
                safe.data[key] = port
        if isinstance(safe, SandboxSessionFinishEvent):
            safe.error_type = "SandboxOperationError" if not safe.ok else None
            safe.error_message = None
            safe.error_code = None
            safe.error_retryable = None
            safe.stdout = safe.stderr = None
            safe.stdout_bytes = safe.stderr_bytes = None
            if safe.op == "running":
                alive = event.data.get("alive")
                if type(alive) is bool:
                    safe.data["alive"] = alive
            if safe.op == "exec":
                code = event.data.get("process.exit.code")
                if type(code) is int and -(2**31) <= code < 2**32:
                    safe.data.update({"exit_code": code, "process.exit.code": code})
            if safe.op in ("exec", "write_stdin"):
                for stream in ("stdout", "stderr"):
                    raw = getattr(event, stream + "_bytes")
                    text = getattr(event, stream)
                    if raw is not None:
                        raw = raw[: 64 * 1024]
                    elif text is not None:
                        raw = text[: 64 * 1024].encode("utf-8", errors="replace")[: 64 * 1024]
                    setattr(safe, stream + "_bytes", raw)
        await super().emit(safe)

    def _apply_policy(
        self, event: SandboxSessionEvent, policy: EventPayloadPolicy
    ) -> SandboxSessionEvent:
        projected = super()._apply_policy(event, policy)
        if isinstance(projected, SandboxSessionFinishEvent):
            projected.stdout_bytes = projected.stderr_bytes = None
        return projected

    async def _finish_session(self, session_id: UUID) -> None:
        self._resources.pop(session_id, None)
        for task, (_, owner) in tuple(self._tasks.items()):
            if owner == session_id:
                task.cancel()
        for sink in self._sinks:
            _retire_binding(sink, session_id)


class _OperationObserver:
    """One weak provider lease supplies resource identity for native-only facts."""

    def __init__(self, session: SandboxSession) -> None:
        self._session = ref(session)
        self.active = True

    async def emit(self, message: dict[str, object]) -> None:
        session = self._session()
        if not self.active or session is None:
            return
        op = message.get("operation")
        if op not in ("exec", "write_stdin", "view_image"):
            return
        identifiers = tuple(message.get(key) for key in ("thread", "turn_id", "call_id"))
        if any(
            not isinstance(value, str) or not value or len(value) > 256 for value in identifiers
        ):
            return
        span = "native:" + ":".join(str(value) for value in identifiers)
        if message.get("phase") == "start":
            await session._emit_start_event(op=op, span_id=span, parent_span_id=None, trace_id=None)
        elif message.get("phase") == "finish":
            duration = message.get("duration_ms")
            ok = message.get("ok")
            if (
                type(ok) is not bool
                or not isinstance(duration, (float, int))
                or isinstance(duration, bool)
                or not math.isfinite(duration)
                or duration < 0
            ):
                return
            await session._emit_finish_event(
                op=op,
                span_id=span,
                parent_span_id=None,
                trace_id=None,
                duration_ms=float(duration),
                ok=ok,
                exc=None,
                data=None,
                stdout=None,
                stderr=None,
            )
