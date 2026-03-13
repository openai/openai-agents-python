from __future__ import annotations

import io
import time
import uuid
from collections.abc import Coroutine
from contextvars import Token
from functools import wraps
from pathlib import Path
from typing import Callable, TypeVar, cast

from ..errors import OpName, UniversalComputerError
from ..types import ExecResult, User
from .base_sandbox_session import BaseSandboxSession
from .dependencies import Dependencies
from .events import UCFinishEvent, UCStartEvent
from .manager import Instrumentation
from .sandbox_session_state import SandboxSessionState
from .sinks import ChainedSink, SandboxSessionBoundSink
from .utils import (
    _best_effort_stream_len,
    current_span_id,
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
    """Decorator to emit UCEvents around a SandboxSession operation."""

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
    out["exit_code"] = cast(ExecResult, result).exit_code
    return out


def _write_start_data(self: SandboxSession, path: Path, data: io.IOBase) -> dict[str, object]:
    out: dict[str, object] = {"path": str(path)}
    n = _best_effort_stream_len(data)
    if n is not None:
        out["bytes"] = n
    return out


def _running_finish_data(
    _start_data: dict[str, object] | None,
    result: object,
) -> dict[str, object]:
    return {"alive": bool(result)}


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
    """A SandboxSession wrapper that emits UCEvent objects around core operations."""

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

    async def _aclose_dependencies(self) -> None:
        await self._inner._aclose_dependencies()

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
        span_id: uuid.UUID,
        parent_span_id: uuid.UUID | None,
        data: dict[str, object] | None = None,
    ) -> None:
        await self._instrumentation.emit(
            UCStartEvent(
                session_id=self.state.session_id,
                seq=self._next_seq(),
                op=op,
                span_id=span_id,
                parent_span_id=parent_span_id,
                data=data or {},
            )
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
        span_id = uuid.uuid4()
        parent = current_span_id.get()
        token = current_span_id.set(span_id)

        try:
            await self._emit_start_event(
                op=op, span_id=span_id, parent_span_id=parent, data=start_data
            )
        except Exception:
            current_span_id.reset(token)
            raise

        t0 = time.monotonic()
        try:
            value = await run()
        except Exception as e:
            await self._emit_finish_event(
                op=op,
                span_id=span_id,
                parent_span_id=parent,
                start_t=t0,
                token=token,
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
        await self._emit_finish_event(
            op=op,
            span_id=span_id,
            parent_span_id=parent,
            start_t=t0,
            token=token,
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
        span_id: uuid.UUID,
        parent_span_id: uuid.UUID | None,
        start_t: float,
        token: Token[uuid.UUID | None],
        ok: bool,
        exc: BaseException | None,
        data: dict[str, object] | None,
        stdout: bytes | None,
        stderr: bytes | None,
    ) -> None:
        duration_ms = (time.monotonic() - start_t) * 1000.0
        event = UCFinishEvent(
            session_id=self.state.session_id,
            seq=self._next_seq(),
            op=op,
            span_id=span_id,
            parent_span_id=parent_span_id,
            data=data or {},
            ok=ok,
            duration_ms=duration_ms,
        )

        if exc is not None:
            event.error_type = type(exc).__name__
            event.error_message = str(exc)
            if isinstance(exc, UniversalComputerError):
                event.error_code = exc.error_code

        # Preserve raw bytes so Instrumentation can apply per-op/per-sink policies later.
        # Decoding here would force one global formatting decision before sink-specific redaction
        # and truncation rules have a chance to run.
        event.stdout_bytes = stdout
        event.stderr_bytes = stderr

        try:
            await self._instrumentation.emit(event)
        finally:
            current_span_id.reset(token)

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

    @instrumented_op("read", data=lambda _self, path: {"path": str(path)})
    async def read(self, path: Path) -> io.IOBase:
        return await self._inner.read(path)

    @instrumented_op("write", data=_write_start_data)
    async def write(self, path: Path, data: io.IOBase) -> None:
        await self._inner.write(path, data)

    @instrumented_op(
        "running",
        finish_data=_running_finish_data,
        ok=lambda _alive: True,
    )
    async def running(self) -> bool:
        return await self._inner.running()

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
