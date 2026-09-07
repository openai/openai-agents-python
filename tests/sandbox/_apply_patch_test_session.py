from __future__ import annotations

import io
import unicodedata
import uuid
from pathlib import Path, PurePosixPath
from typing import cast

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
        self.mv_calls: list[tuple[Path, Path]] = []
        self.directories: set[Path] = set()

    def _stored_path(self, path: Path | str) -> Path:
        """Return the key this store holds `path` under.

        A store that folds names overrides this. Here every distinct spelling is a distinct
        file, which is what a case-sensitive filesystem does.
        """
        return self.normalize_path(path)

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

    async def mv(
        self,
        source: Path | str,
        destination: Path | str,
        *,
        user: str | User | None = None,
    ) -> None:
        _ = user
        if self.normalize_path(destination) in self.directories:
            # A real `mv` would move the source inside this directory and report success.
            raise IsADirectoryError(self.normalize_path(destination))
        stored_source = self._stored_path(source)
        if stored_source not in self.files:
            raise FileNotFoundError(stored_source)
        payload = self.files.pop(stored_source)
        # Look the destination up after removing the source, so a case-only rename does not
        # find the entry it is renaming. A real `mv` replaces whatever is at the destination
        # and stores the name it was given, which is how a case-only rename changes the case.
        self.files.pop(self._stored_path(destination), None)
        normalized_destination = self.normalize_path(destination)
        self.files[normalized_destination] = payload
        self.mv_calls.append((stored_source, normalized_destination))

    async def same_file(
        self,
        left: Path | str,
        right: Path | str,
        *,
        user: str | User | None = None,
    ) -> bool:
        _ = user
        return self._stored_path(left) == self._stored_path(right)


class PosixHostApplyPatchSession(ApplyPatchSession):
    """An apply_patch session whose workspace paths compare case-sensitively on every host.

    Linux and macOS hosts compare sandbox paths case-sensitively, while a Windows host folds
    case in `Path` comparisons. `PurePosixPath` keeps the host comparison case-sensitive
    everywhere so case-only rename coverage does not depend on the operating system that runs
    the tests.
    """

    def normalize_path(self, path: Path | str, *, for_write: bool = False) -> Path:
        normalized = super().normalize_path(path, for_write=for_write)
        return cast(Path, PurePosixPath(normalized.as_posix()))


class CaseFoldingApplyPatchSession(PosixHostApplyPatchSession):
    """A case-sensitive host over a sandbox filesystem that folds path case.

    APFS, NTFS, and Docker bind mounts backed by either store `notes.txt` and `Notes.txt` as
    one file, and they preserve the case of the name that created the file. Lookups here fold
    case so an existing entry keeps its stored name when it is written again.
    """

    def _stored_path(self, path: Path | str) -> Path:
        normalized = self.normalize_path(path)
        folded = normalized.as_posix().casefold()
        for stored in self.files:
            if stored.as_posix().casefold() == folded:
                return stored
        return normalized

    async def read(self, path: Path, *, user: str | User | None = None) -> io.BytesIO:
        return await super().read(self._stored_path(path), user=user)

    async def write(
        self,
        path: Path,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        await super().write(self._stored_path(path), data, user=user)

    async def rm(
        self,
        path: Path | str,
        *,
        recursive: bool = False,
        user: str | User | None = None,
    ) -> None:
        await super().rm(self._stored_path(path), recursive=recursive, user=user)


class NormalizationFoldingApplyPatchSession(PosixHostApplyPatchSession):
    """A case-sensitive host over a filesystem that folds case and Unicode normalization.

    This is APFS. It stores one entry for the decomposed and the composed spelling of the same
    accented name, as well as for `notes.txt` and `Notes.txt`. `str.casefold` answers the first
    pair wrong, which is one reason the fix may not ask a string whether two paths are one file.
    """

    def _stored_path(self, path: Path | str) -> Path:
        normalized = self.normalize_path(path)
        folded = unicodedata.normalize("NFC", normalized.as_posix()).casefold()
        for stored in self.files:
            if unicodedata.normalize("NFC", stored.as_posix()).casefold() == folded:
                return stored
        return normalized

    async def read(self, path: Path, *, user: str | User | None = None) -> io.BytesIO:
        return await ApplyPatchSession.read(self, self._stored_path(path), user=user)

    async def write(
        self,
        path: Path,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        await ApplyPatchSession.write(self, self._stored_path(path), data, user=user)

    async def rm(
        self,
        path: Path | str,
        *,
        recursive: bool = False,
        user: str | User | None = None,
    ) -> None:
        await ApplyPatchSession.rm(self, self._stored_path(path), recursive=recursive, user=user)


class WriteFailureApplyPatchSession(CaseFoldingApplyPatchSession):
    """A case-folding session whose first write fails, as a dropped sandbox connection would."""

    def __init__(self, manifest: Manifest | None = None) -> None:
        super().__init__(manifest)
        self.fail_next_write = True

    async def write(
        self,
        path: Path,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        if self.fail_next_write:
            self.fail_next_write = False
            raise ConnectionError("sandbox write failed")
        await super().write(path, data, user=user)


class ConcurrentWriterApplyPatchSession(PosixHostApplyPatchSession):
    """A case-sensitive session where another writer takes the source path and the move fails.

    The rename is committed by a move. This session lets the staging write land, then has a
    second writer put its own file at the source path, then fails the move. The operation
    cannot succeed from here. What it must not do is put the original text back over the file
    that other writer just created.
    """

    def __init__(self, manifest: Manifest | None = None) -> None:
        super().__init__(manifest)
        self.concurrent_source: Path | None = None
        self.concurrent_text = "written by someone else\n"

    async def mv(
        self,
        source: Path | str,
        destination: Path | str,
        *,
        user: str | User | None = None,
    ) -> None:
        if self.concurrent_source is not None:
            self.files[self.normalize_path(self.concurrent_source)] = self.concurrent_text.encode(
                "utf-8"
            )
        raise ConnectionError("sandbox move failed")


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
        self.mv_users: list[str | None] = []
        self.same_file_users: list[str | None] = []

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

    async def mv(
        self,
        source: Path | str,
        destination: Path | str,
        *,
        user: str | User | None = None,
    ) -> None:
        self.mv_users.append(self._user_name(user))
        await super().mv(source, destination)

    async def same_file(
        self,
        left: Path | str,
        right: Path | str,
        *,
        user: str | User | None = None,
    ) -> bool:
        self.same_file_users.append(self._user_name(user))
        return await super().same_file(left, right)
