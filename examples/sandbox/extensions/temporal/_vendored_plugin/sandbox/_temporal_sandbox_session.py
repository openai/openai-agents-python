# vendored pre-release code; type errors are misreported due to patching
# mypy: ignore-errors
"""Temporal-aware sandbox session that routes all I/O through Temporal activities."""

from __future__ import annotations

import io
from pathlib import Path

from temporalio import workflow
from temporalio.contrib.openai_agents.sandbox._temporal_activity_models import (
    ExecArgs,
    ExecResult as ExecResultModel,
    HydrateWorkspaceArgs,
    PersistWorkspaceArgs,
    PersistWorkspaceResult,
    PtyExecStartArgs,
    PtyExecUpdateResult,
    PtyWriteStdinArgs,
    ReadArgs,
    ReadResult,
    RunningArgs,
    RunningResult,
    StartArgs,
    StopArgs,
    VoidResult,
    WriteArgs,
)
from temporalio.workflow import ActivityConfig

from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.pty_types import PtyExecUpdate
from agents.sandbox.session.sandbox_session_state import SandboxSessionState
from agents.sandbox.types import ExecResult, User


class TemporalSandboxSession(BaseSandboxSession):
    """A BaseSandboxSession that routes all I/O through Temporal activities.

    This class is fully stateless with respect to the physical sandbox -- it
    holds only the serializable ``SandboxSessionState`` and a ``supports_pty``
    flag (both provided by the worker-side ``SessionResult``).

    Activity names are prefixed with the provider ``name`` so that dispatches
    reach the correct sandbox backend's activities on the worker.

    Each activity receives a single Pydantic model instance. Because the Temporal
    client is configured with ``pydantic_data_converter``, all fields are
    serialized and deserialized automatically.
    """

    def __init__(
        self,
        name: str,
        config: ActivityConfig,
        state: SandboxSessionState,
        supports_pty_flag: bool = True,
    ) -> None:
        self._name = name
        self._config = config
        self._state = state
        self._supports_pty = supports_pty_flag

    @property
    def state(self) -> SandboxSessionState:
        return self._state

    @state.setter
    def state(self, value: SandboxSessionState) -> None:
        self._state = value

    async def exec(
        self,
        *command: str | Path,
        timeout: float | None = None,
        shell: bool | list[str] = True,
        user: str | User | None = None,
    ) -> ExecResult:
        result: ExecResultModel = await workflow.execute_activity(
            f"{self._name}-sandbox_session_exec",
            arg=ExecArgs(
                state=self.state,
                command=[str(c) for c in command],
                timeout=timeout,
                shell=shell,
                user=user,
            ),
            result_type=ExecResultModel,
            **self._config,
        )
        return ExecResult(stdout=result.stdout, stderr=result.stderr, exit_code=result.exit_code)

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        raise NotImplementedError("TemporalSandboxSession overrides exec() directly")

    async def read(self, path: Path) -> io.IOBase:
        result: ReadResult = await workflow.execute_activity(
            f"{self._name}-sandbox_session_read",
            arg=ReadArgs(state=self.state, path=str(path)),
            result_type=ReadResult,
            **self._config,
        )
        return io.BytesIO(result.data)

    async def write(self, path: Path, data: io.IOBase) -> None:
        _: VoidResult = await workflow.execute_activity(
            f"{self._name}-sandbox_session_write",
            arg=WriteArgs(state=self.state, path=str(path), data=data.read()),
            result_type=VoidResult,
            **self._config,
        )

    async def running(self) -> bool:
        result: RunningResult = await workflow.execute_activity(
            f"{self._name}-sandbox_session_running",
            arg=RunningArgs(state=self.state),
            result_type=RunningResult,
            **self._config,
        )
        return result.is_running

    async def shutdown(self) -> None:
        _: VoidResult = await workflow.execute_activity(
            f"{self._name}-sandbox_session_shutdown",
            arg=StopArgs(state=self.state),
            result_type=VoidResult,
            **self._config,
        )

    async def persist_workspace(self) -> io.IOBase:
        result: PersistWorkspaceResult = await workflow.execute_activity(
            f"{self._name}-sandbox_session_persist_workspace",
            arg=PersistWorkspaceArgs(state=self.state),
            result_type=PersistWorkspaceResult,
            **self._config,
        )
        return io.BytesIO(result.data)

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        _: VoidResult = await workflow.execute_activity(
            f"{self._name}-sandbox_session_hydrate_workspace",
            arg=HydrateWorkspaceArgs(state=self.state, data=data.read()),
            result_type=VoidResult,
            **self._config,
        )

    def supports_pty(self) -> bool:
        return self._supports_pty

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
        result: PtyExecUpdateResult = await workflow.execute_activity(
            f"{self._name}-sandbox_session_pty_exec_start",
            arg=PtyExecStartArgs(
                state=self.state,
                command=[str(c) for c in command],
                timeout=timeout,
                shell=shell,
                user=user,
                tty=tty,
                yield_time_s=yield_time_s,
                max_output_tokens=max_output_tokens,
            ),
            result_type=PtyExecUpdateResult,
            **self._config,
        )
        return PtyExecUpdate(
            process_id=result.process_id,
            output=result.output,
            exit_code=result.exit_code,
            original_token_count=result.original_token_count,
        )

    async def pty_write_stdin(
        self,
        *,
        session_id: int,
        chars: str,
        yield_time_s: float | None = None,
        max_output_tokens: int | None = None,
    ) -> PtyExecUpdate:
        result: PtyExecUpdateResult = await workflow.execute_activity(
            f"{self._name}-sandbox_session_pty_write_stdin",
            arg=PtyWriteStdinArgs(
                state=self.state,
                session_id=session_id,
                chars=chars,
                yield_time_s=yield_time_s,
                max_output_tokens=max_output_tokens,
            ),
            result_type=PtyExecUpdateResult,
            **self._config,
        )
        return PtyExecUpdate(
            process_id=result.process_id,
            output=result.output,
            exit_code=result.exit_code,
            original_token_count=result.original_token_count,
        )

    async def start(self) -> None:
        _: VoidResult = await workflow.execute_activity(
            f"{self._name}-sandbox_session_start",
            arg=StartArgs(state=self.state),
            result_type=VoidResult,
            **self._config,
        )

    async def stop(self) -> None:
        _: VoidResult = await workflow.execute_activity(
            f"{self._name}-sandbox_session_stop",
            arg=StopArgs(state=self.state),
            result_type=VoidResult,
            **self._config,
        )
