from __future__ import annotations

import io
import ipaddress
import time
import uuid
from collections.abc import Callable, Coroutine
from contextlib import nullcontext
from functools import wraps
from pathlib import Path
from typing import Any, TypeVar, cast

from ...run_config import SandboxArchiveLimits, SandboxConcurrencyLimits
from ...tracing import Span, custom_span, get_current_trace
from ..errors import OpName, SandboxError
from ..files import FileEntry
from ..types import ExecResult, ExposedPortEndpoint, User
from .base_sandbox_session import BaseSandboxSession
from .dependencies import Dependencies
from .events import SandboxSessionFinishEvent, SandboxSessionStartEvent
from .manager import Instrumentation
from .pty_types import PtyExecUpdate
from .sandbox_session_state import SandboxSessionState
from .sinks import ChainedSink, SandboxSessionBoundSink
from .utils import (
    _best_effort_stream_len,
)

T = TypeVar("T")
F = TypeVar("F", bound=Callable[..., Coroutine[object, object, object]])


def instrumented_op(
    op: OpName,
    *,
    data: Callable[..., dict[str, object] | None] | None = None,
    finish_data: (
        Callable[[dict[str, object] | None, object], dict[str, object] | None] | None
    ) = None,
    ok: Callable[[object], bool] | None = None,
    outputs: Callable[[object], tuple[bytes | None, bytes | None]] | None = None,
) -> Callable[[F], F]:
    """Decorator to emit SandboxSessionEvents around a SandboxSession operation."""

    def _decorator(fn: F) -> F:
        @wraps(fn)
        async def _wrapped(self: SandboxSession, *args: object, **kwargs: object) -> object:
            start_data = data(self, *args, **kwargs) if data is not None else None
            finish_cb: Callable[[object], dict[str, object]] | None
            if finish_data is None:
                finish_cb = None
            else:
                fd = finish_data

                def _finish_cb(res: object) -> dict[str, object]:
                    return dict(fd(start_data, res) or {})

                finish_cb = _finish_cb

            return await self._annotate(
                op=op,
                start_data=start_data,
                run=lambda: fn(self, *args, **kwargs),
                finish_data=finish_cb,
                ok=ok,
                outputs=outputs,
            )

        return cast(F, _wrapped)

    return _decorator


def _exec_start_data(
    _self: SandboxSession,
    *command: str | Path,
    timeout: float | None = None,
    shell: bool | list[str] = True,
    user: str | User | None = None,
) -> dict[str, object]:
    user_value: str | None
    if isinstance(user, User):
        user_value = user.name
    else:
        user_value = user
    return {
        "command": [str(c) for c in command],
        "timeout_s": timeout,
        "shell": shell,
        "user": user_value,
    }


def _exec_finish_data(start_data: dict[str, object] | None, result: object) -> dict[str, object]:
    out = dict(start_data or {})
    exit_code = cast(ExecResult, result).exit_code
    out["exit_code"] = exit_code
    out["process.exit.code"] = exit_code
    return out


def _read_start_data(
    self: SandboxSession,
    path: Path,
    *,
    user: str | User | None = None,
) -> dict[str, object]:
    _ = self
    user_value = user.name if isinstance(user, User) else user
    return {"path": str(path), "user": user_value}


def _write_start_data(
    self: SandboxSession,
    path: Path,
    data: io.IOBase,
    *,
    user: str | User | None = None,
) -> dict[str, object]:
    user_value = user.name if isinstance(user, User) else user
    out: dict[str, object] = {"path": str(path), "user": user_value}
    n = _best_effort_stream_len(data)
    if n is not None:
        out["bytes"] = n
    return out


def _running_finish_data(
    _start_data: dict[str, object] | None,
    result: object,
) -> dict[str, object]:
    return {"alive": bool(result)}


def _resolve_exposed_port_start_data(_self: SandboxSession, port: int) -> dict[str, object]:
    return {"port": port}


def _resolve_exposed_port_finish_data(
    _start_data: dict[str, object] | None,
    result: object,
) -> dict[str, object]:
    endpoint = cast(ExposedPortEndpoint, result)
    out: dict[str, object] = {"server.port": endpoint.port}
    normalized_host = endpoint.host.strip().lower()
    if normalized_host in {"localhost", "::1"}:
        out["server.address"] = endpoint.host
    else:
        try:
            if ipaddress.ip_address(normalized_host).is_loopback:
                out["server.address"] = endpoint.host
        except ValueError:
            pass
    return out


def _new_audit_span_id() -> str:
    return f"sandbox_op_{uuid.uuid4().hex}"


def _supports_trace_spans() -> bool:
    current_trace = get_current_trace()
    return current_trace is not None and current_trace.export() is not None


def _audit_trace_ids(trace_span: Span[Any] | None) -> tuple[str, str | None, str | None]:
    if trace_span is None or trace_span.export() is None:
        return _new_audit_span_id(), None, None
    return trace_span.span_id, trace_span.parent_id, trace_span.trace_id


def _snapshot_tar_path(self: SandboxSession) -> str | None:
    """
    Best-effort path to the persisted workspace tar on the *host*.

    Today Snapshot is a LocalSnapshot whose persist() writes `<base_path>/<id>.tar`.
    We keep this best-effort (instead of importing LocalSnapshot) to avoid coupling.
    """

    snap = getattr(self.state, "snapshot", None)
    base_path = getattr(snap, "base_path", None)
    snap_id = getattr(snap, "id", None)
    if isinstance(base_path, Path) and isinstance(snap_id, str) and snap_id:
        return str(Path(str(base_path / snap_id) + ".tar"))
    return None


def _persist_start_data(self: SandboxSession) -> dict[str, object]:
    out: dict[str, object] = {"workspace_root": str(self.state.manifest.root)}
    tar_path = _snapshot_tar_path(self)
    if tar_path is not None:
        out["tar_path"] = tar_path
    return out


def _persist_finish_data(
    start_data: dict[str, object] | None,
    result: object,
) -> dict[str, object]:
    out = dict(start_data or {})
    n = _best_effort_stream_len(cast(io.IOBase, result))
    if n is not None:
        out["bytes"] = n
    return out


def _hydrate_start_data(self: SandboxSession, data: io.IOBase) -> dict[str, object]:
    out: dict[str, object] = {"untar_dir": str(self.state.manifest.root)}
    n = _best_effort_stream_len(data)
    if n is not None:
        out["bytes"] = n
    return out


class SandboxSession(BaseSandboxSession):
    """Wrap sandbox operations in audit events and SDK tracing spans when tracing is active."""

    _inner: BaseSandboxSession
    _instrumentation: Instrumentation
    _seq: int

    def __init__(
        self,
        inner: BaseSandboxSession,
        *,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
    ) -> None:
        self._inner = inner
        self._inner.set_dependencies(dependencies)
        self._instrumentation = instrumentation or Instrumentation()
        self._seq = 0

        self._bind_session_to_sinks()

    def _bind_session_to_sinks(self) -> None:
        # Bind sinks to the *inner* session to avoid recursive instrumentation loops.
        for sink in self._instrumentation.sinks:
            sinks: list[object]
            if isinstance(sink, ChainedSink):
                sinks = list(sink.sinks)
            else:
                sinks = [sink]
            for s in sinks:
                if isinstance(s, SandboxSessionBoundSink):
                    s.bind(self._inner)

    @property
    def state(self) -> SandboxSessionState:
        return self._inner.state

    @state.setter
    def state(self, value: SandboxSessionState) -> None:  # pragma: no cover
        self._inner.state = value

    @property
    def dependencies(self) -> Dependencies:
        return self._inner.dependencies

    def set_dependencies(self, dependencies: Dependencies | None) -> None:
        self._inner.set_dependencies(dependencies)

    async def _aclose_dependencies(self) -> None:
        await self._inner._aclose_dependencies()

    def _set_concurrency_limits(self, limits: SandboxConcurrencyLimits) -> None:
        super()._set_concurrency_limits(limits)
        self._inner._set_concurrency_limits(limits)

    def _set_archive_limits(self, limits: SandboxArchiveLimits | None) -> None:
        super()._set_archive_limits(limits)
        self._inner._set_archive_limits(limits)

    def normalize_path(self, path: Path | str, *, for_write: bool = False) -> Path:
        return self._inner.normalize_path(path, for_write=for_write)

    def supports_pty(self) -> bool:
        return self._inner.supports_pty()

    async def aclose(self) -> None:
        try:
            await super().aclose()
        finally:
            await self._instrumentation.flush()

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def _emit_start_event(
        self,
        *,
        op: OpName,
        span_id: str,
        parent_span_id: str | None,
        trace_id: str | None,
        data: dict[str, object] | None = None,
    ) -> None:
        await self._instrumentation.emit(
            SandboxSessionStartEvent(
                session_id=self.state.session_id,
                seq=self._next_seq(),
                op=op,
                span_id=span_id,
                parent_span_id=parent_span_id,
                trace_id=trace_id,
                data=data or {},
            )
        )

    def _trace_span_data(self, *, op: OpName) -> dict[str, object]:
        return {
            "sandbox.backend": type(self._inner).__module__.rsplit(".", 1)[-1],
            "sandbox.operation": op,
            "sandbox.session.id": str(self.state.session_id),
            "session_id": str(self.state.session_id),
        }

    def _apply_trace_finish_data(
        self,
        *,
        span: Span[Any] | None,
        op: OpName,
        ok: bool,
        data: dict[str, object] | None,
        exc: BaseException | None,
    ) -> None:
        if span is None:
            return

        trace_data = span.span_data.data
        trace_data.update(self._trace_span_data(op=op))
        if data is not None:
            if "alive" in data:
                trace_data["alive"] = data["alive"]
            if "exit_code" in data:
                trace_data["exit_code"] = data["exit_code"]
            if "process.exit.code" in data:
                trace_data["process.exit.code"] = data["process.exit.code"]
            if "server.port" in data:
                trace_data["server.port"] = data["server.port"]
            if "server.address" in data:
                trace_data["server.address"] = data["server.address"]
        if exc is not None:
            trace_data["error.type"] = type(exc).__name__
            trace_data["error_type"] = type(exc).__name__
            error_data: dict[str, object] = {"operation": op}
            if isinstance(exc, SandboxError):
                trace_data["error_code"] = exc.error_code
                error_data["error_code"] = exc.error_code
            span.set_error({"message": type(exc).__name__, "data": error_data})
            return
        if not ok:
            if op == "exec":
                trace_data["error.type"] = "ExecNonZeroError"
            error_data = {"operation": op}
            if data is not None and "exit_code" in data:
                error_data["exit_code"] = data["exit_code"]
            span.set_error(
                {
                    "message": "Sandbox operation returned an unsuccessful result.",
                    "data": error_data,
                }
            )

    async def _annotate(
        self,
        *,
        op: OpName,
        start_data: dict[str, object] | None,
        run: Callable[[], Coroutine[object, object, T]],
        finish_data: Callable[[T], dict[str, object]] | None = None,
        ok: Callable[[T], bool] | None = None,
        outputs: Callable[[T], tuple[bytes | None, bytes | None]] | None = None,
    ) -> T:
        span_cm = (
            custom_span(
                name=f"sandbox.{op}",
                data=self._trace_span_data(op=op),
            )
            if _supports_trace_spans()
            else nullcontext(None)
        )
        with span_cm as trace_span:
            span_id, parent_span_id, trace_id = _audit_trace_ids(trace_span)

            await self._emit_start_event(
                op=op,
                span_id=span_id,
                parent_span_id=parent_span_id,
                trace_id=trace_id,
                data=start_data,
            )

            t0 = time.monotonic()
            try:
                value = await run()
            except Exception as e:
                duration_ms = (time.monotonic() - t0) * 1000.0
                self._apply_trace_finish_data(
                    span=trace_span,
                    op=op,
                    ok=False,
                    data=start_data,
                    exc=e,
                )
                await self._emit_finish_event(
                    op=op,
                    span_id=span_id,
                    parent_span_id=parent_span_id,
                    trace_id=trace_id,
                    duration_ms=duration_ms,
                    ok=False,
                    exc=e,
                    data=start_data,
                    stdout=None,
                    stderr=None,
                )
                raise

            data_finish = finish_data(value) if finish_data is not None else start_data
            ok_value = ok(value) if ok is not None else True
            stdout, stderr = outputs(value) if outputs is not None else (None, None)
            duration_ms = (time.monotonic() - t0) * 1000.0
            self._apply_trace_finish_data(
                span=trace_span,
                op=op,
                ok=ok_value,
                data=data_finish,
                exc=None,
            )
            await self._emit_finish_event(
                op=op,
                span_id=span_id,
                parent_span_id=parent_span_id,
                trace_id=trace_id,
                duration_ms=duration_ms,
                ok=ok_value,
                exc=None,
                data=data_finish,
                stdout=stdout,
                stderr=stderr,
            )
            return value

    async def _emit_finish_event(
        self,
        *,
        op: OpName,
        span_id: str,
        parent_span_id: str | None,
        trace_id: str | None,
        duration_ms: float,
        ok: bool,
        exc: BaseException | None,
        data: dict[str, object] | None,
        stdout: bytes | None,
        stderr: bytes | None,
    ) -> None:
        event = SandboxSessionFinishEvent(
            session_id=self.state.session_id,
            seq=self._next_seq(),
            op=op,
            span_id=span_id,
            parent_span_id=parent_span_id,
            trace_id=trace_id,
            data=data or {},
            ok=ok,
            duration_ms=duration_ms,
        )

        if exc is not None:
            event.error_type = type(exc).__name__
            event.error_message = str(exc)
            if isinstance(exc, SandboxError):
                event.error_code = exc.error_code

        # Preserve raw bytes so Instrumentation can apply per-op/per-sink policies later.
        # Decoding here would force one global formatting decision before sink-specific redaction
        # and truncation rules have a chance to run.
        event.stdout_bytes = stdout
        event.stderr_bytes = stderr

        await self._instrumentation.emit(event)

    @instrumented_op("start")
    async def start(self) -> None:
        await self._inner.start()

    @instrumented_op("stop")
    async def stop(self) -> None:
        await self._inner.stop()

    @instrumented_op("shutdown")
    async def shutdown(self) -> None:
        await self._inner.shutdown()

    @instrumented_op(
        "exec",
        data=_exec_start_data,
        finish_data=_exec_finish_data,
        ok=lambda result: cast(ExecResult, result).ok(),
        outputs=lambda result: (
            cast(ExecResult, result).stdout,
            cast(ExecResult, result).stderr,
        ),
    )
    async def exec(
        self,
        *command: str | Path,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: str | User | None = None,
    ) -> ExecResult:
        return await self._inner.exec(*command, timeout=timeout, shell=shell, user=user)

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        raise NotImplementedError("this should never be invoked")

    async def _resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        _ = port
        raise NotImplementedError("this should never be invoked")

    async def pty_exec_start(
        self,
        *command: str | Path,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: str | User | None = None,
        tty: bool = False,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        return await self._inner.pty_exec_start(
            *command,
            timeout=timeout,
            shell=shell,
            user=user,
            tty=tty,
            yield_time_s=yield_time_s,
            max_output_tokens=max_output_tokens,
        )

    async def pty_write_stdin(
        self,
        *,
        session_id: int,
        chars: str,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        return await self._inner.pty_write_stdin(
            session_id=session_id,
            chars=chars,
            yield_time_s=yield_time_s,
            max_output_tokens=max_output_tokens,
        )

    async def pty_terminate_all(self) -> None:
        await self._inner.pty_terminate_all()

    async def _validate_path_access(self, path: Path | str, *, for_write: bool = False) -> Path:
        return await self._inner._validate_path_access(path, for_write=for_write)

    async def ls(
        self,
        path: Path | str,
        *,
        user: str | User | None = None,
    ) -> list[FileEntry]:
        return await self._inner.ls(path, user=user)

    async def rm(
        self,
        path: Path | str,
        *,
        recursive: bool = False,
        user: str | User | None = None,
    ) -> None:
        await self._inner.rm(path, recursive=recursive, user=user)

    async def mkdir(
        self,
        path: Path | str,
        *,
        parents: bool = False,
        user: str | User | None = None,
    ) -> None:
        await self._inner.mkdir(path, parents=parents, user=user)

    @instrumented_op("read", data=_read_start_data)
    async def read(self, path: Path, *, user: str | User | None = None) -> io.IOBase:
        return await self._inner.read(path, user=user)

    @instrumented_op("write", data=_write_start_data)
    async def write(
        self,
        path: Path,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        await self._inner.write(path, data, user=user)

    @instrumented_op(
        "running",
        finish_data=_running_finish_data,
        ok=lambda _alive: True,
    )
    async def running(self) -> bool:
        return await self._inner.running()

    @instrumented_op(
        "resolve_exposed_port",
        data=_resolve_exposed_port_start_data,
        finish_data=_resolve_exposed_port_finish_data,
        ok=lambda _result: True,
    )
    async def resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        return await self._inner.resolve_exposed_port(port)

    @instrumented_op(
        "persist_workspace",
        data=_persist_start_data,
        finish_data=_persist_finish_data,
    )
    async def persist_workspace(self) -> io.IOBase:
        return await self._inner.persist_workspace()

    @instrumented_op(
        "hydrate_workspace",
        data=_hydrate_start_data,
    )
    async def hydrate_workspace(self, data: io.IOBase) -> None:
        await self._inner.hydrate_workspace(data)
