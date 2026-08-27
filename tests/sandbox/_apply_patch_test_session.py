from __future__ import annotations

import io
import uuid
from pathlib import Path

from agents.sandbox import Manifest
from agents.sandbox.errors import WorkspaceReadNotFoundError
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.snapshot import NoopSnapshot
from agents.sandbox.types import ExecResult, User
from tests.utils.factories import TestSessionState


class ApplyPatchSession(BaseSandboxSession):
    def __init__(self, manifest: Manifest | None = None) -> None:
        self.state = TestSessionState(
            manifest=manifest or Manifest(root="/workspace"),
            snapshot=NoopSnapshot(id=str(uuid.uuid4())),
        )
        self.files: dict[Path, bytes] = {}
        self.mkdir_calls: list[tuple[Path, bool]] = []
        self.rm_calls: list[tuple[Path, bool]] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def running(self) -> bool:
        return True

    async def read(self, path: Path, *, user: str | User | None = None) -> io.BytesIO:
        _ = user
        normalized = self.normalize_path(path)
        if normalized not in self.files:
            raise FileNotFoundError(normalized)
        return io.BytesIO(self.files[normalized])

    async def write(
        self,
        path: Path,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        _ = user
        normalized = self.normalize_path(path)
        payload = data.read()
        if isinstance(payload, str):
            self.files[normalized] = payload.encode("utf-8")
        else:
            self.files[normalized] = bytes(payload)

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = (command, timeout)
        raise AssertionError("_exec_internal() should not be called")

    async def persist_workspace(self) -> io.IOBase:
        return io.BytesIO()

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        _ = data

    async def mkdir(
        self,
        path: Path | str,
        *,
        parents: bool = False,
        user: str | User | None = None,
    ) -> None:
        _ = user
        normalized = self.normalize_path(path)
        self.mkdir_calls.append((normalized, parents))

    async def rm(
        self,
        path: Path | str,
        *,
        recursive: bool = False,
        user: str | User | None = None,
    ) -> None:
        _ = user
        normalized = self.normalize_path(path)
        self.rm_calls.append((normalized, recursive))
        self.files.pop(normalized, None)


class ProviderNotFoundApplyPatchSession(ApplyPatchSession):
    async def read(self, path: Path, *, user: str | User | None = None) -> io.BytesIO:
        try:
            return await super().read(path, user=user)
        except FileNotFoundError as exc:
            workspace_path = self.normalize_path(path).relative_to("/")
            raise WorkspaceReadNotFoundError(
                path=Path("/provider/private/root") / workspace_path
            ) from exc


class UserRecordingApplyPatchSession(ApplyPatchSession):
    def __init__(self, manifest: Manifest | None = None) -> None:
        super().__init__(manifest)
        self.read_users: list[str | None] = []
        self.write_users: list[str | None] = []
        self.mkdir_users: list[str | None] = []
        self.rm_users: list[str | None] = []

    @staticmethod
    def _user_name(user: str | User | None) -> str | None:
        return user.name if isinstance(user, User) else user

    async def read(self, path: Path, *, user: str | User | None = None) -> io.BytesIO:
        self.read_users.append(self._user_name(user))
        return await super().read(path)

    async def write(
        self,
        path: Path,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        self.write_users.append(self._user_name(user))
        await super().write(path, data)

    async def mkdir(
        self,
        path: Path | str,
        *,
        parents: bool = False,
        user: str | User | None = None,
    ) -> None:
        self.mkdir_users.append(self._user_name(user))
        await super().mkdir(path, parents=parents)

    async def rm(
        self,
        path: Path | str,
        *,
        recursive: bool = False,
        user: str | User | None = None,
    ) -> None:
        self.rm_users.append(self._user_name(user))
        await super().rm(path, recursive=recursive)
