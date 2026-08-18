from __future__ import annotations

import io
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import cast

from agents.sandbox.errors import (
    ExecNonZeroError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
)
from agents.sandbox.files import EntryKind, FileEntry
from agents.sandbox.manifest import Manifest
from agents.sandbox.sandboxes.unix_local import (
    UnixLocalSandboxClient,
    UnixLocalSandboxSessionState,
)
from agents.sandbox.session import SandboxSession
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.sandbox_session_state import SandboxSessionState
from agents.sandbox.snapshot import NoopSnapshot, SnapshotBase, SnapshotSpec
from agents.sandbox.types import ExecResult, Permissions, User


class FilesystemTestSandboxSession(BaseSandboxSession):
    """Host-filesystem test double with no process-execution implementation."""

    def __init__(self, state: UnixLocalSandboxSessionState) -> None:
        self.state = state
        self._running = False

    async def start(self) -> None:
        Path(self.state.manifest.root).mkdir(parents=True, exist_ok=True)
        self._running = True
        self.state.workspace_root_ready = True

    async def stop(self) -> None:
        self._running = False

    async def shutdown(self) -> None:
        self._running = False

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = timeout
        command_parts = tuple(str(part) for part in command)
        if len(command_parts) == 3 and command_parts[:2] in {("test", "-d"), ("test", "-f")}:
            path = Path(command_parts[2])
            exists = path.is_dir() if command_parts[1] == "-d" else path.is_file()
            return ExecResult(stdout=b"", stderr=b"", exit_code=0 if exists else 1)
        raise AssertionError(f"Unexpected filesystem test command: {command_parts!r}")

    @staticmethod
    def _reject_user(user: str | User | None) -> None:
        if user is not None:
            raise AssertionError(
                "FilesystemTestSandboxSession does not support user-scoped filesystem operations"
            )

    async def read(self, path: Path, *, user: str | User | None = None) -> io.IOBase:
        self._reject_user(user)
        workspace_path = self.normalize_path(path)
        try:
            return workspace_path.open("rb")
        except FileNotFoundError as error:
            raise WorkspaceReadNotFoundError(path=path, cause=error) from error
        except OSError as error:
            raise WorkspaceArchiveReadError(path=path, cause=error) from error

    async def write(
        self,
        path: Path,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        self._reject_user(user)
        workspace_path = self.normalize_path(path, for_write=True)
        try:
            workspace_path.parent.mkdir(parents=True, exist_ok=True)
            with workspace_path.open("wb") as stream:
                shutil.copyfileobj(data, stream)
        except OSError as error:
            raise WorkspaceArchiveWriteError(path=path, cause=error) from error

    async def ls(
        self,
        path: Path | str,
        *,
        user: str | User | None = None,
    ) -> list[FileEntry]:
        self._reject_user(user)
        workspace_path = self.normalize_path(path)
        try:
            with os.scandir(workspace_path) as entries:
                listed: list[FileEntry] = []
                for entry in entries:
                    stat_result = entry.stat(follow_symlinks=False)
                    if entry.is_symlink():
                        kind = EntryKind.SYMLINK
                    elif entry.is_dir(follow_symlinks=False):
                        kind = EntryKind.DIRECTORY
                    elif entry.is_file(follow_symlinks=False):
                        kind = EntryKind.FILE
                    else:
                        kind = EntryKind.OTHER
                    listed.append(
                        FileEntry(
                            path=entry.path,
                            permissions=Permissions.from_mode(stat_result.st_mode),
                            owner=str(stat_result.st_uid),
                            group=str(stat_result.st_gid),
                            size=stat_result.st_size,
                            kind=kind,
                        )
                    )
                return listed
        except OSError as error:
            raise ExecNonZeroError(
                ExecResult(stdout=b"", stderr=str(error).encode(), exit_code=1),
                command=("ls", "-la", "--", str(workspace_path)),
                cause=error,
            ) from error

    async def mkdir(
        self,
        path: Path | str,
        *,
        parents: bool = False,
        user: str | User | None = None,
    ) -> None:
        self._reject_user(user)
        self.normalize_path(path, for_write=True).mkdir(parents=parents, exist_ok=True)

    async def rm(
        self,
        path: Path | str,
        *,
        recursive: bool = False,
        user: str | User | None = None,
    ) -> None:
        self._reject_user(user)
        workspace_path = self.normalize_path(path, for_write=True)
        if workspace_path.is_dir() and not workspace_path.is_symlink():
            if recursive:
                shutil.rmtree(workspace_path)
            else:
                workspace_path.rmdir()
        else:
            workspace_path.unlink()

    async def running(self) -> bool:
        return self._running

    async def persist_workspace(self) -> io.IOBase:
        raise AssertionError("FilesystemTestSandboxSession does not support workspace persistence")

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        _ = data
        raise AssertionError("FilesystemTestSandboxSession does not support workspace hydration")


class FilesystemTestSandboxClient(UnixLocalSandboxClient):
    """Client test double that creates ``FilesystemTestSandboxSession`` instances."""

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: object | None = None,
    ) -> SandboxSession:
        _ = (snapshot, options)
        resolved_manifest = manifest if manifest is not None else Manifest()
        workspace_root_owned = resolved_manifest.root == Manifest().root
        if workspace_root_owned:
            workspace_root = tempfile.mkdtemp(prefix="filesystem-test-workspace-")
            resolved_manifest = resolved_manifest.model_copy(
                update={"root": workspace_root},
                deep=True,
            )
        state = UnixLocalSandboxSessionState(
            session_id=uuid.uuid4(),
            manifest=resolved_manifest,
            snapshot=NoopSnapshot(id=str(uuid.uuid4())),
            workspace_root_owned=workspace_root_owned,
        )
        return self._wrap_session(
            FilesystemTestSandboxSession(state=state),
            instrumentation=self._instrumentation,
        )

    async def delete(self, session: SandboxSession) -> SandboxSession:
        inner = cast(FilesystemTestSandboxSession, session._inner)
        if inner.state.workspace_root_owned:
            shutil.rmtree(inner.state.manifest.root, ignore_errors=True)
        return session

    async def resume(self, state: SandboxSessionState) -> SandboxSession:
        unix_state = cast(UnixLocalSandboxSessionState, state)
        return self._wrap_session(
            FilesystemTestSandboxSession(state=unix_state),
            instrumentation=self._instrumentation,
        )
