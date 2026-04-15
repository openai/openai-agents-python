# vendored pre-release code; type errors are misreported due to patching
# mypy: ignore-errors
"""Worker-side Temporal activities for sandbox lifecycle and I/O operations."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.contrib.openai_agents.sandbox._temporal_activity_models import (
    CreateSessionArgs,
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
    ResumeSessionArgs,
    RunningArgs,
    RunningResult,
    SessionResult,
    StartArgs,
    StopArgs,
    VoidResult,
    WriteArgs,
    _HasState,
)

from agents.sandbox.session.sandbox_client import BaseSandboxClient
from agents.sandbox.session.sandbox_session import SandboxSession


class TemporalSandboxActivities:
    """Class-based activity set registered on the Temporal worker.

    Holds a ``BaseSandboxClient`` as a dependency and caches open sessions by
    ``session_id`` to avoid reconnecting on every activity invocation within the
    same worker process. The cache is cleared on ``sandbox_stop``. If the worker
    restarts, ``_client.resume(state)`` re-establishes the connection on the
    next activity invocation.

    Each activity receives a single Pydantic arg model; ``pydantic_data_converter``
    handles deserialization automatically.

    Activity names are prefixed with the provider ``name`` so that multiple
    sandbox backends can coexist on a single worker (e.g.
    ``"daytona-sandbox_exec"``, ``"local-sandbox_exec"``).
    """

    def __init__(self, name: str, client: BaseSandboxClient) -> None:  # type: ignore[type-arg]
        self._name = name
        self._client = client
        self._sessions: dict[str, SandboxSession] = {}

    async def _session(self, args: _HasState) -> SandboxSession:
        key = str(args.state.session_id)
        if key not in self._sessions:
            self._sessions[key] = await self._client.resume(args.state)
        return self._sessions[key]

    def all(self) -> list[Any]:
        """Return all activity callables for registration with a Temporal ``Worker``.

        Each activity is a closure that captures ``self`` and is decorated with
        a provider-prefixed name so that multiple ``TemporalSandboxActivities``
        instances (one per sandbox backend) can be registered on the same worker.
        """
        prefix = self._name

        # -- Client-level operations (lifecycle) --

        @activity.defn(name=f"{prefix}-sandbox_client_create")
        async def create_session(args: CreateSessionArgs) -> SessionResult:
            session = await self._client.create(
                snapshot=args.snapshot_spec,
                manifest=args.manifest,
                options=args.client_options,
            )
            self._sessions[str(session.state.session_id)] = session
            return SessionResult(state=session.state, supports_pty=session.supports_pty())

        @activity.defn(name=f"{prefix}-sandbox_client_resume")
        async def resume_session(args: ResumeSessionArgs) -> SessionResult:
            session = await self._client.resume(args.state)
            self._sessions[str(session.state.session_id)] = session
            return SessionResult(state=session.state, supports_pty=session.supports_pty())

        @activity.defn(name=f"{prefix}-sandbox_client_delete")
        async def delete_session(args: StopArgs) -> VoidResult:
            session = await self._session(args)
            await self._client.delete(session)
            return VoidResult()

        # -- Session-level operations (I/O and lifecycle) --

        @activity.defn(name=f"{prefix}-sandbox_session_exec")
        async def exec_(args: ExecArgs) -> ExecResultModel:
            result = await (await self._session(args)).exec(
                *args.command,
                timeout=args.timeout,
                shell=args.shell,
                user=args.user,
            )
            return ExecResultModel(
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
            )

        @activity.defn(name=f"{prefix}-sandbox_session_read")
        async def read(args: ReadArgs) -> ReadResult:
            handle = await (await self._session(args)).read(Path(args.path))
            return ReadResult(data=handle.read())

        @activity.defn(name=f"{prefix}-sandbox_session_write")
        async def write(args: WriteArgs) -> VoidResult:
            await (await self._session(args)).write(Path(args.path), io.BytesIO(args.data))
            return VoidResult()

        @activity.defn(name=f"{prefix}-sandbox_session_running")
        async def running(args: RunningArgs) -> RunningResult:
            return RunningResult(is_running=await (await self._session(args)).running())

        @activity.defn(name=f"{prefix}-sandbox_session_persist_workspace")
        async def persist_workspace(
            args: PersistWorkspaceArgs,
        ) -> PersistWorkspaceResult:
            stream = await (await self._session(args)).persist_workspace()
            return PersistWorkspaceResult(data=stream.read())

        @activity.defn(name=f"{prefix}-sandbox_session_hydrate_workspace")
        async def hydrate_workspace(args: HydrateWorkspaceArgs) -> VoidResult:
            await (await self._session(args)).hydrate_workspace(io.BytesIO(args.data))
            return VoidResult()

        @activity.defn(name=f"{prefix}-sandbox_session_pty_exec_start")
        async def pty_exec_start(args: PtyExecStartArgs) -> PtyExecUpdateResult:
            update = await (await self._session(args)).pty_exec_start(
                *args.command,
                timeout=args.timeout,
                shell=args.shell,
                user=args.user,
                tty=args.tty,
                yield_time_s=args.yield_time_s,
                max_output_tokens=args.max_output_tokens,
            )
            return PtyExecUpdateResult(
                process_id=update.process_id,
                output=update.output,
                exit_code=update.exit_code,
                original_token_count=update.original_token_count,
            )

        @activity.defn(name=f"{prefix}-sandbox_session_pty_write_stdin")
        async def pty_write_stdin(args: PtyWriteStdinArgs) -> PtyExecUpdateResult:
            update = await (await self._session(args)).pty_write_stdin(
                session_id=args.session_id,
                chars=args.chars,
                yield_time_s=args.yield_time_s,
                max_output_tokens=args.max_output_tokens,
            )
            return PtyExecUpdateResult(
                process_id=update.process_id,
                output=update.output,
                exit_code=update.exit_code,
                original_token_count=update.original_token_count,
            )

        @activity.defn(name=f"{prefix}-sandbox_session_start")
        async def start(args: StartArgs) -> VoidResult:
            await (await self._session(args)).start()
            return VoidResult()

        @activity.defn(name=f"{prefix}-sandbox_session_stop")
        async def session_stop(args: StopArgs) -> VoidResult:
            await (await self._session(args)).stop()
            return VoidResult()

        @activity.defn(name=f"{prefix}-sandbox_session_shutdown")
        async def session_shutdown(args: StopArgs) -> VoidResult:
            key = str(args.state.session_id)
            session = self._sessions.get(key)
            if session is not None:
                await session.shutdown()
                del self._sessions[key]
            return VoidResult()

        return [
            create_session,
            resume_session,
            delete_session,
            exec_,
            read,
            write,
            running,
            persist_workspace,
            hydrate_workspace,
            pty_exec_start,
            pty_write_stdin,
            start,
            session_stop,
            session_shutdown,
        ]
