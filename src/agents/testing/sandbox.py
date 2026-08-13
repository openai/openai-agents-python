from __future__ import annotations

import abc
import copy
import inspect
import io
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, TypeAlias, cast, get_args

from typing_extensions import TypedDict

from ..editor import ApplyPatchOperation
from ..sandbox.apply_patch import PatchFormat
from ..sandbox.files import FileEntry
from ..sandbox.manifest import Manifest
from ..sandbox.session.base_sandbox_session import BaseSandboxSession
from ..sandbox.session.pty_types import PtyExecUpdate
from ..sandbox.session.sandbox_session_state import SandboxSessionState
from ..sandbox.snapshot import NoopSnapshot
from ..sandbox.types import ExecResult, User

SandboxMethod = Literal[
    "apply_patch",
    "exec",
    "ls",
    "mkdir",
    "pty_exec_start",
    "pty_write_stdin",
    "read",
    "rm",
    "write",
]
SandboxStepReason = Literal["invalid_input", "unknown_method", "invalid_matcher", "invalid_outcome"]

_SCRIPTABLE_METHODS: frozenset[str] = frozenset(get_args(SandboxMethod))
_PTY_METHODS: frozenset[SandboxMethod] = frozenset({"pty_exec_start", "pty_write_stdin"})
_UNSUPPORTED_OPTIONAL_METHODS: frozenset[str] = frozenset(
    {
        "extract",
        "hydrate_workspace",
        "persist_workspace",
        "resolve_exposed_port",
    }
)

_HIDDEN_LIFECYCLE_METHODS: frozenset[str] = frozenset({"pty_terminate_all"})

for _method_name in _SCRIPTABLE_METHODS | _UNSUPPORTED_OPTIONAL_METHODS | _HIDDEN_LIFECYCLE_METHODS:
    if not hasattr(BaseSandboxSession, _method_name):
        raise RuntimeError(f"Unknown BaseSandboxSession method: {_method_name}")


class SandboxScriptError(Exception):
    """Base exception for an invalid or incompletely consumed sandbox script."""


class InvalidSandboxStep(SandboxScriptError):
    """Raised when a sandbox step is invalid at factory construction time."""

    def __init__(
        self,
        message: str,
        *,
        reason: SandboxStepReason,
        input_index: int,
        method: str | None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.input_index = input_index
        self.method = method


class UnexpectedSandboxCall(SandboxScriptError):
    """Raised when a call does not match the next configured sandbox step."""

    def __init__(
        self,
        message: str,
        *,
        call: SandboxCall,
        call_index: int,
        expected_method: str | None,
        remaining_steps: int,
    ) -> None:
        super().__init__(message)
        self.call = call
        self.call_index = call_index
        self.actual_method: str = call.method
        self.expected_method = expected_method
        self.remaining_steps = remaining_steps


class SandboxCallMatcherError(SandboxScriptError):
    """Raised when a sandbox step matcher rejects its call."""

    def __init__(self, message: str, *, call: SandboxCall, call_index: int) -> None:
        super().__init__(message)
        self.call = call
        self.call_index = call_index
        self.method: str = call.method


class UnconsumedSandboxSteps(SandboxScriptError):
    """Raised when configured sandbox steps remain unconsumed."""

    def __init__(
        self,
        message: str,
        *,
        remaining_steps: int,
        pending_methods: tuple[str, ...],
    ) -> None:
        super().__init__(message)
        self.remaining_steps = remaining_steps
        self.pending_methods = pending_methods


@dataclass(frozen=True)
class SandboxCall:
    """A detached invocation-time sandbox call snapshot."""

    call_index: int
    method: str
    args: tuple[Any, ...]
    kwargs: Mapping[str, Any]


SandboxMatcher: TypeAlias = Callable[[SandboxCall], bool | None]
SandboxResponder: TypeAlias = Callable[[SandboxCall], Any | Awaitable[Any]]


class SandboxStepSpec(TypedDict, total=False):
    """Dictionary form of one FIFO scripted sandbox call."""

    method: SandboxMethod
    match: SandboxMatcher
    result: Any
    responder: SandboxResponder
    error: Exception


_STEP_FIELDS = frozenset(SandboxStepSpec.__annotations__)


@dataclass(frozen=True)
class _SandboxStep:
    method: SandboxMethod
    match: SandboxMatcher | None
    outcome: Literal["result", "responder", "error"]
    value: Any


def _snapshot_value(value: Any) -> Any:
    if isinstance(value, io.BytesIO):
        if value.closed:
            raise TypeError("Cannot snapshot a closed sandbox byte stream.")
        byte_snapshot = io.BytesIO(value.getvalue())
        byte_snapshot.seek(value.tell())
        return byte_snapshot
    if isinstance(value, io.StringIO):
        if value.closed:
            raise TypeError("Cannot snapshot a closed sandbox text stream.")
        text_snapshot = io.StringIO(value.getvalue())
        text_snapshot.seek(value.tell())
        return text_snapshot
    if isinstance(value, io.IOBase):
        raise TypeError("Sandbox stream snapshots support only io.BytesIO and io.StringIO.")
    if callable(value):
        return value
    if isinstance(value, tuple):
        return tuple(_snapshot_value(item) for item in value)
    if isinstance(value, list):
        return [_snapshot_value(item) for item in value]
    if isinstance(value, dict):
        return {_snapshot_value(key): _snapshot_value(item) for key, item in value.items()}
    if isinstance(value, set):
        return {_snapshot_value(item) for item in value}
    return copy.deepcopy(value)


def _snapshot_call(call: SandboxCall) -> SandboxCall:
    return SandboxCall(
        call_index=call.call_index,
        method=call.method,
        args=tuple(_snapshot_value(call.args)),
        kwargs=MappingProxyType(
            {name: _snapshot_value(value) for name, value in call.kwargs.items()}
        ),
    )


def _invalid_step(
    *,
    reason: SandboxStepReason,
    input_index: int,
    method: str | None,
    detail: str,
) -> InvalidSandboxStep:
    return InvalidSandboxStep(
        f"Scripted sandbox step #{input_index + 1} {detail}.",
        reason=reason,
        input_index=input_index,
        method=method,
    )


def _normalize_step(input: object, input_index: int) -> _SandboxStep:
    if not isinstance(input, Mapping):
        raise _invalid_step(
            reason="invalid_input",
            input_index=input_index,
            method=None,
            detail="must be a mapping",
        )

    method = input.get("method")
    if any(field not in _STEP_FIELDS for field in input):
        raise _invalid_step(
            reason="invalid_input",
            input_index=input_index,
            method=method if isinstance(method, str) else None,
            detail="contains unsupported fields",
        )
    if not isinstance(method, str) or method not in _SCRIPTABLE_METHODS:
        raise _invalid_step(
            reason="unknown_method",
            input_index=input_index,
            method=method if isinstance(method, str) else None,
            detail="has an unknown method",
        )

    matcher = input.get("match")
    if isinstance(matcher, io.IOBase):
        raise _invalid_step(
            reason="invalid_matcher",
            input_index=input_index,
            method=method,
            detail=f"for {method} must use a non-stream callable matcher",
        )
    if matcher is not None and not callable(matcher):
        raise _invalid_step(
            reason="invalid_matcher",
            input_index=input_index,
            method=method,
            detail=f"for {method} must use a callable matcher",
        )

    outcomes = [name for name in ("result", "responder", "error") if name in input]
    if len(outcomes) != 1:
        raise _invalid_step(
            reason="invalid_outcome",
            input_index=input_index,
            method=method,
            detail=f"for {method} must define exactly one outcome",
        )
    outcome = cast(Literal["result", "responder", "error"], outcomes[0])
    value = input[outcome]
    if outcome == "responder" and isinstance(value, io.IOBase):
        raise _invalid_step(
            reason="invalid_outcome",
            input_index=input_index,
            method=method,
            detail=f"for {method} must use a non-stream callable responder",
        )
    if outcome == "responder" and not callable(value):
        raise _invalid_step(
            reason="invalid_outcome",
            input_index=input_index,
            method=method,
            detail=f"for {method} must use a callable responder",
        )
    if outcome == "error" and not isinstance(value, Exception):
        raise _invalid_step(
            reason="invalid_outcome",
            input_index=input_index,
            method=method,
            detail=f"for {method} must use an Exception",
        )
    if outcome == "result":
        try:
            value = _snapshot_value(value)
        except TypeError as error:
            raise _invalid_step(
                reason="invalid_outcome",
                input_index=input_index,
                method=method,
                detail=f"for {method} contains a result that cannot be snapshotted",
            ) from error
    return _SandboxStep(
        method=cast(SandboxMethod, method),
        match=cast(SandboxMatcher | None, matcher),
        outcome=outcome,
        value=value,
    )


class ScriptedSandboxSession(BaseSandboxSession, abc.ABC):
    """The typed result interface for ``scripted_sandbox_session``."""

    @property
    @abc.abstractmethod
    def calls(self) -> tuple[SandboxCall, ...]:
        """Return detached call-history snapshots in invocation order."""

    @property
    @abc.abstractmethod
    def remaining_steps(self) -> int:
        """Return the number of configured calls that remain."""

    @abc.abstractmethod
    def assert_complete(self) -> None:
        """Raise when configured sandbox calls remain unconsumed."""


class _ScriptedSandboxSession(ScriptedSandboxSession):
    def __init__(self, steps: Sequence[_SandboxStep], *, manifest: Manifest | None) -> None:
        self.state: SandboxSessionState = SandboxSessionState(
            type="scripted",
            snapshot=NoopSnapshot(id="scripted"),
            manifest=copy.deepcopy(manifest) if manifest is not None else Manifest(),
        )
        self._steps = list(steps)
        configured_methods = {step.method for step in steps}
        if configured_methods & _PTY_METHODS:
            configured_methods.update(_PTY_METHODS)
        self._configured_methods = frozenset(configured_methods)
        self._calls: list[SandboxCall] = []
        self._running = False

    def __getattribute__(self, name: str) -> Any:
        if name in _SCRIPTABLE_METHODS:
            configured = object.__getattribute__(self, "_configured_methods")
            if name not in configured:
                raise AttributeError(name)
        if name in _UNSUPPORTED_OPTIONAL_METHODS | _HIDDEN_LIFECYCLE_METHODS:
            raise AttributeError(name)
        return super().__getattribute__(name)

    def __dir__(self) -> list[str]:
        configured = self._configured_methods
        return sorted(
            name
            for name in super().__dir__()
            if name not in _UNSUPPORTED_OPTIONAL_METHODS | _HIDDEN_LIFECYCLE_METHODS
            and (name not in _SCRIPTABLE_METHODS or name in configured)
        )

    @property
    def calls(self) -> tuple[SandboxCall, ...]:
        """Return detached call-history snapshots in invocation order."""
        return tuple(_snapshot_call(call) for call in self._calls)

    @property
    def remaining_steps(self) -> int:
        """Return the number of configured calls that remain."""
        return len(self._steps)

    def assert_complete(self) -> None:
        """Raise when configured sandbox calls remain unconsumed."""
        if self._steps:
            pending_methods = tuple(step.method for step in self._steps)
            raise UnconsumedSandboxSteps(
                f"Scripted sandbox session has {len(self._steps)} unconsumed step(s).",
                remaining_steps=len(self._steps),
                pending_methods=pending_methods,
            )

    async def _invoke(
        self, method: SandboxMethod, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> Any:
        call = SandboxCall(
            call_index=len(self._calls),
            method=method,
            args=tuple(_snapshot_value(args)),
            kwargs=MappingProxyType(
                {name: _snapshot_value(value) for name, value in kwargs.items()}
            ),
        )
        call_index = call.call_index
        self._calls.append(call)

        if not self._steps:
            raise UnexpectedSandboxCall(
                f"Unexpected sandbox {method} call #{call_index + 1}: no scripted steps remain.",
                call=_snapshot_call(call),
                call_index=call_index,
                expected_method=None,
                remaining_steps=0,
            )

        step = self._steps[0]
        if step.method != method:
            raise UnexpectedSandboxCall(
                f"Unexpected sandbox {method} call #{call_index + 1}; expected {step.method}.",
                call=_snapshot_call(call),
                call_index=call_index,
                expected_method=step.method,
                remaining_steps=len(self._steps),
            )

        matcher_call = _snapshot_call(call)
        if step.match is not None and step.match(matcher_call) is False:
            raise SandboxCallMatcherError(
                f"Sandbox matcher rejected {method} call #{call_index + 1}.",
                call=_snapshot_call(call),
                call_index=call_index,
            )
        self._steps.pop(0)
        if step.outcome == "error":
            raise step.value
        if step.outcome == "responder":
            result = step.value(_snapshot_call(call))
            if inspect.isawaitable(result):
                result = await result
            return result
        return step.value

    async def start(self) -> None:
        self._running = True
        self.state.workspace_root_ready = True

    async def stop(self) -> None:
        self._running = False

    async def _before_shutdown(self) -> None:
        return

    async def running(self) -> bool:
        return self._running

    def supports_pty(self) -> bool:
        return _PTY_METHODS.issubset(self._configured_methods)

    async def exec(
        self,
        *command: str | Path,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: str | User | None = None,
    ) -> ExecResult:
        return cast(
            ExecResult,
            await self._invoke(
                "exec",
                command,
                {"timeout": timeout, "shell": shell, "user": user},
            ),
        )

    async def read(self, path: Path, *, user: str | User | None = None) -> io.IOBase:
        return cast(io.IOBase, await self._invoke("read", (path,), {"user": user}))

    async def write(
        self,
        path: Path,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        await self._invoke("write", (path, data), {"user": user})

    async def ls(
        self,
        path: Path | str,
        *,
        user: str | User | None = None,
    ) -> list[FileEntry]:
        return cast(list[FileEntry], await self._invoke("ls", (path,), {"user": user}))

    async def rm(
        self,
        path: Path | str,
        *,
        recursive: bool = False,
        user: str | User | None = None,
    ) -> None:
        await self._invoke("rm", (path,), {"recursive": recursive, "user": user})

    async def mkdir(
        self,
        path: Path | str,
        *,
        parents: bool = False,
        user: str | User | None = None,
    ) -> None:
        await self._invoke("mkdir", (path,), {"parents": parents, "user": user})

    async def apply_patch(
        self,
        operations: ApplyPatchOperation
        | dict[str, object]
        | list[ApplyPatchOperation | dict[str, object]],
        *,
        patch_format: PatchFormat | Literal["v4a"] = "v4a",
    ) -> str:
        return cast(
            str,
            await self._invoke(
                "apply_patch",
                (operations,),
                {"patch_format": patch_format},
            ),
        )

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = (command, timeout)
        raise NotImplementedError

    async def persist_workspace(self) -> io.IOBase:
        raise NotImplementedError

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        _ = data
        raise NotImplementedError

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
        return cast(
            PtyExecUpdate,
            await self._invoke(
                "pty_exec_start",
                command,
                {
                    "timeout": timeout,
                    "shell": shell,
                    "user": user,
                    "tty": tty,
                    "yield_time_s": yield_time_s,
                    "max_output_tokens": max_output_tokens,
                },
            ),
        )

    async def pty_write_stdin(
        self,
        *,
        session_id: int,
        chars: str,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        return cast(
            PtyExecUpdate,
            await self._invoke(
                "pty_write_stdin",
                (),
                {
                    "session_id": session_id,
                    "chars": chars,
                    "yield_time_s": yield_time_s,
                    "max_output_tokens": max_output_tokens,
                },
            ),
        )


def scripted_sandbox_session(
    steps: Iterable[SandboxStepSpec | Mapping[str, Any]] = (),
    *,
    manifest: Manifest | None = None,
) -> ScriptedSandboxSession:
    """Create a deterministic provider-free sandbox session for agent workflow tests.

    Each FIFO step defines ``method`` plus exactly one of ``result``, ``responder``, or ``error``.
    An optional ``match`` callable receives a detached ``SandboxCall``. The returned object is the
    session itself, so pass it directly to ``SandboxRunConfig(session=session)``. Only configured
    model-facing methods are visible. The two PTY methods are exposed together when either one is
    configured because they form one advertised session capability. Use a custom
    ``BaseSandboxSession`` or a real provider for lifecycle, persistence, mount, or broader
    filesystem behavior.
    """
    normalized = [_normalize_step(step, index) for index, step in enumerate(steps)]
    return _ScriptedSandboxSession(normalized, manifest=manifest)


__all__ = [
    "InvalidSandboxStep",
    "SandboxCall",
    "SandboxCallMatcherError",
    "SandboxScriptError",
    "SandboxStepSpec",
    "ScriptedSandboxSession",
    "UnconsumedSandboxSteps",
    "UnexpectedSandboxCall",
    "scripted_sandbox_session",
]
