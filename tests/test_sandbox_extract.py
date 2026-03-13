from __future__ import annotations

import io
import os
import tarfile
import zipfile
from pathlib import Path

import pytest

from agents.sandbox.errors import InvalidManifestPathError, WorkspaceArchiveWriteError
from agents.sandbox.files import EntryKind, FileEntry
from agents.sandbox.manifest import Manifest
from agents.sandbox.sandboxes.unix_local import (
    UnixLocalSandboxSession,
    UnixLocalSandboxSessionState,
)
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.snapshot import NoopSnapshot
from agents.sandbox.types import ExecResult, Permissions


def _build_session(tmp_path: Path) -> UnixLocalSandboxSession:
    state = UnixLocalSandboxSessionState(
        manifest=Manifest(root=str(tmp_path / "workspace")),
        snapshot=NoopSnapshot(id="noop"),
    )
    return UnixLocalSandboxSession.from_state(state)


class _CountingExtractSession(BaseSandboxSession):
    def __init__(self, workspace_root: Path) -> None:
        self.state = UnixLocalSandboxSessionState(
            manifest=Manifest(root=str(workspace_root)),
            snapshot=NoopSnapshot(id="noop"),
        )
        self.ls_calls: list[Path] = []

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = (command, timeout)
        raise AssertionError("exec() should not be called in this test")

    async def read(self, path: Path) -> io.IOBase:
        return self.normalize_path(path).open("rb")

    async def write(self, path: Path, data: io.IOBase) -> None:
        workspace_path = self.normalize_path(path)
        workspace_path.parent.mkdir(parents=True, exist_ok=True)
        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        workspace_path.write_bytes(payload)

    async def running(self) -> bool:
        return True

    async def persist_workspace(self) -> io.IOBase:
        return io.BytesIO()

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        _ = data

    async def shutdown(self) -> None:
        return

    async def mkdir(self, path: Path | str, *, parents: bool = False) -> None:
        self.normalize_path(path).mkdir(parents=parents, exist_ok=True)

    async def ls(self, path: Path | str) -> list[FileEntry]:
        directory = self.normalize_path(path)
        self.ls_calls.append(directory)
        if not directory.exists():
            raise AssertionError(f"ls() called for missing directory: {directory}")

        entries: list[FileEntry] = []
        for child in directory.iterdir():
            if child.is_symlink():
                kind = EntryKind.SYMLINK
            elif child.is_dir():
                kind = EntryKind.DIRECTORY
            else:
                kind = EntryKind.FILE
            entries.append(
                FileEntry(
                    path=str(child),
                    permissions=Permissions(),
                    owner="root",
                    group="root",
                    size=0,
                    kind=kind,
                )
            )
        return entries


def _tar_bytes(*, members: dict[str, bytes]) -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as archive:
        for name, payload in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    buf.seek(0)
    return buf


def _zip_bytes(*, members: dict[str, bytes]) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    buf.seek(0)
    return buf


@pytest.mark.asyncio
async def test_extract_tar_writes_archive_and_unpacks_contents(tmp_path: Path) -> None:
    session = _build_session(tmp_path)
    await session.start()
    try:
        await session.extract(
            "bundle.tar",
            _tar_bytes(members={"nested/hello.txt": b"hello from tar"}),
        )
    finally:
        await session.shutdown()

    workspace = Path(session.state.manifest.root)
    assert (workspace / "bundle.tar").is_file()
    assert (workspace / "nested" / "hello.txt").read_text(encoding="utf-8") == "hello from tar"


@pytest.mark.asyncio
async def test_extract_zip_writes_archive_and_unpacks_contents(tmp_path: Path) -> None:
    session = _build_session(tmp_path)
    await session.start()
    try:
        await session.extract(
            "bundle.zip",
            _zip_bytes(members={"nested/hello.txt": b"hello from zip"}),
        )
    finally:
        await session.shutdown()

    workspace = Path(session.state.manifest.root)
    assert (workspace / "bundle.zip").is_file()
    assert (workspace / "nested" / "hello.txt").read_text(encoding="utf-8") == "hello from zip"


class _NoSeekableZipStream(io.IOBase):
    def __init__(self, payload: bytes) -> None:
        self._buffer = io.BytesIO(payload)

    def tell(self) -> int:
        return self._buffer.tell()

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        return self._buffer.seek(offset, whence)

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)


def test_zipfile_compatible_stream_wraps_streams_without_seekable() -> None:
    raw_stream = _NoSeekableZipStream(_zip_bytes(members={"file.txt": b"hello"}).getvalue())

    compatible = BaseSandboxSession._zipfile_compatible_stream(raw_stream)

    with zipfile.ZipFile(compatible) as archive:
        assert archive.read("file.txt") == b"hello"


@pytest.mark.asyncio
async def test_extract_tar_rejects_symlinked_parent_paths(tmp_path: Path) -> None:
    session = _build_session(tmp_path)
    await session.start()
    try:
        workspace = Path(session.state.manifest.root)
        outside = tmp_path / "outside"
        outside.mkdir()
        os.symlink(outside, workspace / "link", target_is_directory=True)

        with pytest.raises(WorkspaceArchiveWriteError) as exc_info:
            await session.extract(
                "bundle.tar",
                _tar_bytes(members={"link/hello.txt": b"hello from tar"}),
            )

        assert exc_info.value.context["member"] == "link/hello.txt"
        assert exc_info.value.context["reason"] == "symlink in parent path: link"
        assert not (outside / "hello.txt").exists()
    finally:
        await session.shutdown()


@pytest.mark.asyncio
async def test_extract_zip_rejects_symlinked_parent_paths(tmp_path: Path) -> None:
    session = _build_session(tmp_path)
    await session.start()
    try:
        workspace = Path(session.state.manifest.root)
        outside = tmp_path / "outside"
        outside.mkdir()
        os.symlink(outside, workspace / "link", target_is_directory=True)

        with pytest.raises(WorkspaceArchiveWriteError) as exc_info:
            await session.extract(
                "bundle.zip",
                _zip_bytes(members={"link/hello.txt": b"hello from zip"}),
            )

        assert exc_info.value.context["member"] == "link/hello.txt"
        assert exc_info.value.context["reason"] == "symlink in parent path: link"
        assert not (outside / "hello.txt").exists()
    finally:
        await session.shutdown()


@pytest.mark.asyncio
async def test_extract_tar_reuses_directory_listings_during_symlink_checks(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _CountingExtractSession(workspace)

    await session.extract(
        "bundle.tar",
        _tar_bytes(
            members={
                "nested/one.txt": b"one",
                "nested/two.txt": b"two",
            }
        ),
    )

    assert (workspace / "nested" / "one.txt").read_text(encoding="utf-8") == "one"
    assert (workspace / "nested" / "two.txt").read_text(encoding="utf-8") == "two"
    assert session.ls_calls == [
        workspace,
        workspace / "nested",
    ]


@pytest.mark.asyncio
async def test_unix_local_helpers_reject_paths_outside_workspace_root(tmp_path: Path) -> None:
    session = _build_session(tmp_path)
    await session.start()
    try:
        with pytest.raises(InvalidManifestPathError, match="must not escape root"):
            await session.ls("../outside")
        with pytest.raises(InvalidManifestPathError, match="must not escape root"):
            await session.mkdir("../outside", parents=True)
        with pytest.raises(InvalidManifestPathError, match="must not escape root"):
            await session.rm("../outside")
        with pytest.raises(InvalidManifestPathError, match="must be relative"):
            await session.extract("/tmp/bundle.tar", _tar_bytes(members={"a.txt": b"a"}))
    finally:
        await session.shutdown()


@pytest.mark.asyncio
async def test_unix_local_helpers_reject_symlink_escape_paths(tmp_path: Path) -> None:
    session = _build_session(tmp_path)
    await session.start()
    try:
        workspace = Path(session.state.manifest.root)
        outside = tmp_path / "outside"
        outside.mkdir()
        os.symlink(outside, workspace / "link", target_is_directory=True)

        with pytest.raises(InvalidManifestPathError, match="must not escape root"):
            await session.mkdir("link/nested", parents=True)
        with pytest.raises(InvalidManifestPathError, match="must not escape root"):
            await session.ls("link")
    finally:
        await session.shutdown()
