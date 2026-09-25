from __future__ import annotations

import abc
import asyncio
import io
import logging
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Literal, NoReturn, Protocol, runtime_checkable
from urllib.request import Request, urlopen

from ..errors import WorkspaceArchiveReadError, WorkspaceReadNotFoundError
from .base_sandbox_session import BaseSandboxSession
from .events import EventPayloadPolicy, SandboxSessionEvent
from .utils import event_to_json_line

logger = logging.getLogger(__name__)

DeliveryMode = Literal["sync", "async", "best_effort"]
OnErrorPolicy = Literal["raise", "log", "ignore"]


def _unwrap_session_wrapper(session: BaseSandboxSession) -> BaseSandboxSession:
    """
    Defensive unwrapping: if a sink is accidentally bound to a SandboxSession wrapper,
    unwrap to the underlying session to avoid recursive event loops.
    """

    # Avoid importing session.sandbox_session.SandboxSession here
    # (would create a dependency cycle).
    cls = type(session)
    if not (
        cls.__name__ == "SandboxSession"
        and cls.__module__ == "agents.sandbox.session.sandbox_session"
    ):
        return session
    inner = getattr(session, "_inner", None)
    return inner if isinstance(inner, BaseSandboxSession) else session


class EventSink(abc.ABC):
    """Consumes SandboxSessionEvent objects (e.g., callback, file outbox, proxy HTTP)."""

    name: str | None = None
    mode: DeliveryMode
    on_error: OnErrorPolicy
    payload_policy: EventPayloadPolicy | None

    @abc.abstractmethod
    async def handle(self, event: SandboxSessionEvent) -> None: ...


@runtime_checkable
class SandboxSessionBoundSink(Protocol):
    """Optional interface for sinks that need access to the underlying SandboxSession."""

    def bind(self, session: BaseSandboxSession) -> None: ...


class CallbackSink(EventSink):
    """Deliver events to a user-provided callable.

    Supports sync or async callables.
    """

    def __init__(
        self,
        callback: Callable[[SandboxSessionEvent, BaseSandboxSession], object],
        *,
        mode: DeliveryMode = "sync",
        on_error: OnErrorPolicy = "raise",
        payload_policy: EventPayloadPolicy | None = None,
        name: str | None = None,
    ) -> None:
        self._callback = callback
        self.mode = mode
        self.on_error = on_error
        self.payload_policy = payload_policy
        self._session: BaseSandboxSession | None = None
        self.name = name

    def bind(self, session: BaseSandboxSession) -> None:
        self._session = _unwrap_session_wrapper(session)

    async def handle(self, event: SandboxSessionEvent) -> None:
        if self._session is None:
            raise RuntimeError(
                "CallbackSink requires a bound session; use SandboxSession / "
                "a sandbox client with instrumentation (or call bind(session))."
            )
        out = self._callback(event, self._session)
        if asyncio.iscoroutine(out):
            await out


class JsonlOutboxSink(EventSink):
    """Append events to a JSONL file on the host filesystem."""

    def __init__(
        self,
        path: Path,
        *,
        mode: DeliveryMode = "best_effort",
        on_error: OnErrorPolicy = "log",
        payload_policy: EventPayloadPolicy | None = None,
    ) -> None:
        self.path = path
        self.mode = mode
        self.on_error = on_error
        self.payload_policy = payload_policy

    async def handle(self, event: SandboxSessionEvent) -> None:
        line = event_to_json_line(event)
        await asyncio.to_thread(self._append_line, line)

    def _append_line(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fcntl_mod: ModuleType | None
        try:
            import fcntl as fcntl_mod
        except Exception:
            # Not available on all platforms (e.g. Windows)
            fcntl_mod = None

        with self.path.open("a", encoding="utf-8") as f:
            if fcntl_mod is not None:
                try:
                    fcntl_mod.flock(f.fileno(), fcntl_mod.LOCK_EX)
                except Exception:
                    pass
            f.write(line)
            f.flush()
            if fcntl_mod is not None:
                try:
                    # Nice to have release here; the OS releases the lock
                    # automatically when the file is closed.
                    fcntl_mod.flock(f.fileno(), fcntl_mod.LOCK_UN)
                except Exception:
                    pass


class WorkspaceJsonlSink(EventSink):
    """
    Append events to a JSONL file inside the session workspace (under manifest.root).

    This sink reads and replaces the outbox through the session's file APIs.
    With a finite `max_bytes`, SDK backends limit acquisition from the source.
    Legacy custom backends use their existing `read()` API; data acquired inside that method
    is outside this sink's size limit. Backends own read deadlines and cleanup;
    this sink does not impose an additional timeout. Workspace logs are
    workload-modifiable and are not authoritative audit records. Use
    `JsonlOutboxSink` or `HttpProxySink` for storage outside the workspace.
    """

    def __init__(
        self,
        *,
        workspace_relpath: Path = Path("logs/events-{session_id}.jsonl"),
        ephemeral: bool = False,
        mode: DeliveryMode = "best_effort",
        on_error: OnErrorPolicy = "log",
        payload_policy: EventPayloadPolicy | None = None,
        flush_every: int = 1,
        max_bytes: int | None = None,
    ) -> None:
        """
        Args:
            workspace_relpath: Relative path under the session workspace root.
                This also supports lightweight templating which is expanded on `bind()`:
                - `"{session_id}"` (UUID string, e.g. "550e8400-e29b-41d4-a716-446655440000")
                - `"{session_id_hex}"` (UUID hex, e.g. "550e8400e29b41d4a716446655440000")

                Example:
                    Path("logs/events-{session_id}.jsonl")
            max_bytes: Optional maximum replacement-file and pending-buffer size in bytes.
                The default `None` preserves unlimited delivery and whole-file reads.
                Set a positive budget to bound acquisition of workload-modifiable logs.
                Exceeding either budget clears pending events
                and permanently stops this sink, reporting one error through
                `on_error`. Subsequent events are ignored, including after rebind.
                Create a new sink for a new outbox or with a larger finite budget,
                or use a host/HTTP sink for longer-lived logs. No file is truncated
                or rotated when the budget is exceeded.
        """
        if max_bytes is not None and max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = max_bytes
        self._disabled = False
        self.workspace_relpath = workspace_relpath
        self.ephemeral = ephemeral
        self.mode = mode
        self.on_error = on_error
        self.payload_policy = payload_policy
        self._session: BaseSandboxSession | None = None
        self._resolved_workspace_relpath: Path | None = None
        self._buf = bytearray()
        self._seen = 0
        self._lock = asyncio.Lock()
        self._flush_every = max(1, int(flush_every))

    def _resolve_relpath(self) -> Path:
        rel = self.workspace_relpath
        if self._session is None:
            return rel
        template = str(rel)
        try:
            rendered = template.format(
                session_id=self._session.state.session_id,
                session_id_hex=self._session.state.session_id.hex,
            )
        except Exception:
            # If formatting fails for any reason, fall back to the literal path.
            rendered = template
        return Path(rendered)

    def bind(self, session: BaseSandboxSession) -> None:
        self._session = _unwrap_session_wrapper(session)
        self._resolved_workspace_relpath = self._resolve_relpath()
        if self.ephemeral:
            relpath = self._resolved_workspace_relpath or self.workspace_relpath
            self._session.register_persist_workspace_skip_path(relpath)

    def _buffer_event(self, event: SandboxSessionEvent) -> bool:
        line = event_to_json_line(event).encode("utf-8")
        if self.max_bytes is not None and len(self._buf) + len(line) > self.max_bytes:
            self._stop_at_limit()
        self._buf.extend(line)
        self._seen += 1

        if self._seen % self._flush_every == 0:
            return True
        if event.op == "persist_workspace" and event.phase == "start":
            return True
        if event.op == "stop":
            return True
        if event.op == "shutdown" and event.phase == "start":
            return True
        if event.op == "shutdown" and event.phase == "finish":
            return False

        return False

    async def _can_flush_to_workspace(self) -> bool:
        if self._session is None:
            return False

        # `SandboxSession.start()` emits the `start` event before the underlying sandbox
        # is fully running, so writes may still fail during early startup or late teardown.
        try:
            return await self._session.running()
        except Exception:
            return False

    async def _flush_buffer(self) -> None:
        if self._session is None or not self._buf:
            return

        relpath = self._resolved_workspace_relpath or self.workspace_relpath
        existing = await self._read_existing_outbox(relpath)
        pending = bytes(self._buf)
        if self.max_bytes is not None and len(existing) + len(pending) > self.max_bytes:
            self._stop_at_limit()
        await self._session.write(relpath, io.BytesIO(existing + pending))
        self._buf.clear()

    def _stop_at_limit(self) -> NoReturn:
        self._disabled = True
        self._buf.clear()
        raise RuntimeError(
            f"WorkspaceJsonlSink exceeded max_bytes={self.max_bytes}; delivery stopped. "
            "Use a new outbox and sink, or JsonlOutboxSink/HttpProxySink."
        )

    async def _read_existing_outbox(self, relpath: Path) -> bytes:
        if self._session is None:
            return b""

        try:
            if self.max_bytes is None:
                existing = await self._session.read(relpath)
                try:
                    payload = existing.read()
                finally:
                    existing.close()
                return payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
            return await self._session.read_bounded(relpath, max_bytes=self.max_bytes + 1)
        except (FileNotFoundError, WorkspaceReadNotFoundError):
            return b""
        except WorkspaceArchiveReadError as error:
            if error.context.get("reason") != "bounded_read_wire_limit":
                raise
        self._stop_at_limit()

    async def handle(self, event: SandboxSessionEvent) -> None:
        # If unbound (e.g., audit event emission used without a SandboxSession wrapper),
        # no-op.
        if self._session is None:
            return

        async with self._lock:
            if self._disabled:
                return
            if not self._buffer_event(event):
                return

            if not await self._can_flush_to_workspace():
                return

            await self._flush_buffer()


class HttpProxySink(EventSink):
    """POST events as JSON to a proxy endpoint (local daemon or remote service)."""

    def __init__(
        self,
        endpoint: str,
        *,
        headers: dict[str, str] | None = None,
        timeout_s: float = 5.0,
        spool_path: Path | None = None,
        mode: DeliveryMode = "best_effort",
        on_error: OnErrorPolicy = "log",
        payload_policy: EventPayloadPolicy | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.headers = dict(headers or {})
        self.timeout_s = timeout_s
        self.spool_path = spool_path
        self.mode = mode
        self.on_error = on_error
        self.payload_policy = payload_policy

    async def handle(self, event: SandboxSessionEvent) -> None:
        payload = event.model_dump_json().encode("utf-8")
        spool_line = event_to_json_line(event) if self.spool_path is not None else None
        await asyncio.to_thread(self._post, payload, spool_line)

    def _post(self, body: bytes, spool_line: str | None) -> None:
        # TODO: thinking about using proxy instead of direct http call
        req = Request(
            self.endpoint,
            data=body,
            headers={"content-type": "application/json", **self.headers},
            method="POST",
        )
        try:
            with urlopen(req, timeout=self.timeout_s) as resp:
                _ = resp.read(1)  # ensure request completes
        except OSError as e:
            if spool_line is not None and self.spool_path is not None:
                try:
                    self.spool_path.parent.mkdir(parents=True, exist_ok=True)
                    with self.spool_path.open("a", encoding="utf-8") as f:
                        f.write(spool_line)
                        f.flush()
                except Exception:
                    pass
            raise RuntimeError(f"http proxy sink POST failed: {e}") from e


class ChainedSink(EventSink):
    """
    Groups multiple sinks that should run in order.

    Note: Instrumentation unwraps this group and applies per-op/per-sink
    payload policies to each inner sink individually (so grouping does not disable
    per-sink policy behavior).
    """

    def __init__(self, *sinks: EventSink) -> None:
        self.sinks = list(sinks)
        # These are not used directly when Instrumentation unwraps the
        # group, but keep the object conforming to EventSink.
        self.mode = "sync"
        self.on_error = "raise"
        self.payload_policy = None

    async def handle(self, event: SandboxSessionEvent) -> None:
        # Fallback behavior if used directly (without Instrumentation unwrapping).
        for sink in self.sinks:
            await sink.handle(event)
