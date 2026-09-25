from __future__ import annotations

import asyncio
import io
import os
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agents.editor import ApplyPatchOperation
from agents.run_config import SandboxRunConfig
from agents.sandbox.apply_patch import WorkspaceEditor
from agents.sandbox.capabilities import Capability
from agents.sandbox.errors import (
    ExecNonZeroError,
    InvalidManifestPathError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
)
from agents.sandbox.manifest import Manifest, SandboxPathGrant
from agents.sandbox.runtime_session_manager import SandboxRuntimeSessionManager
from agents.sandbox.sandbox_agent import SandboxAgent
from agents.sandbox.snapshot import NoopSnapshot

if TYPE_CHECKING or sys.platform != "win32":
    from agents.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxSession,
        UnixLocalSandboxSessionState,
    )

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(sys.platform == "win32", reason="Unix only")]


def _session(root: Path, *, grants: tuple[SandboxPathGrant, ...] = ()) -> UnixLocalSandboxSession:
    return UnixLocalSandboxSession(
        state=UnixLocalSandboxSessionState(
            manifest=Manifest(root=str(root), extra_path_grants=grants),
            snapshot=NoopSnapshot(id="file-io"),
        )
    )


async def _operate(session: UnixLocalSandboxSession, operation: str, path: Path) -> object:
    if operation == "read":
        with await session.read(path) as stream:
            return stream.read()
    if operation == "write":
        await session.write(path, io.BytesIO(b"new"))
    elif operation == "mkdir":
        await session.mkdir(path, parents=True)
    elif operation == "ls":
        return await session.ls(path)
    elif operation == "patch":
        return await WorkspaceEditor(session).apply_operation(
            ApplyPatchOperation(type="create_file", path=str(path), diff="+new\n")
        )
    elif operation in {"rm", "rmtree"}:
        await session.rm(path, recursive=operation == "rmtree")
    return None


@pytest.mark.parametrize("operation", ["read", "write", "mkdir", "ls", "rm", "rmtree", "patch"])
async def test_parent_swap_after_validation_cannot_access_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    parent = workspace / "parent"
    parent.mkdir(parents=True)
    outside.mkdir()
    directory = operation in {"mkdir", "ls", "rmtree"}
    for root in (parent, outside):
        if directory:
            (root / "target").mkdir()
            (root / "target" / "sentinel").write_bytes(b"original content")
        else:
            (root / "target").write_bytes(b"original content")
    session = _session(workspace)
    normalize = session.normalize_path
    swapped = False

    def swap(path: Path | str, *, for_write: bool = False) -> Path:
        nonlocal swapped
        result = normalize(path, for_write=for_write)
        if not swapped and (operation != "patch" or (for_write and result.name == "target")):
            swapped = True
            parent.rename(workspace / "original")
            parent.symlink_to(outside, target_is_directory=True)
        return result

    # Suspend at the check/use boundary without replacing the actual OS file operations.
    monkeypatch.setattr(session, "normalize_path", swap)
    with pytest.raises(
        (
            OSError,
            ExecNonZeroError,
            WorkspaceArchiveReadError,
            WorkspaceArchiveWriteError,
            InvalidManifestPathError,
        )
    ):
        await _operate(session, operation, Path("parent/target"))
    assert swapped
    sentinel = outside / "target" / "sentinel" if directory else outside / "target"
    assert sentinel.read_bytes() == b"original content"


@pytest.mark.parametrize("operation", ["read", "write", "mkdir", "ls", "rm", "rmtree"])
async def test_leaf_swap_does_not_follow_new_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    target = workspace / "target"
    directory = operation in {"mkdir", "ls", "rmtree"}
    for path in (target, outside):
        if directory:
            path.mkdir()
            (path / "sentinel").write_bytes(b"original content")
        else:
            path.write_bytes(b"original content")
    session = _session(workspace)
    normalize = session.normalize_path

    def swap(path: Path | str, *, for_write: bool = False) -> Path:
        result = normalize(path, for_write=for_write)
        target.rename(workspace / "original")
        target.symlink_to(outside, target_is_directory=directory)
        return result

    monkeypatch.setattr(session, "normalize_path", swap)
    if operation in {"rm", "rmtree"}:
        await _operate(session, operation, Path("target"))
    else:
        with pytest.raises(
            (OSError, ExecNonZeroError, WorkspaceArchiveReadError, WorkspaceArchiveWriteError)
        ):
            await _operate(session, operation, Path("target"))
    sentinel = outside / "sentinel" if directory else outside
    assert sentinel.read_bytes() == b"original content"


async def test_safe_symlinks_grants_and_listing_paths_remain_supported(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    (readonly / "file").write_bytes(b"read only")
    alias = tmp_path / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    session = _session(
        alias,
        grants=(
            SandboxPathGrant(path=str(allowed)),
            SandboxPathGrant(path=str(readonly), read_only=True),
        ),
    )
    (workspace / "internal").symlink_to(workspace / "real", target_is_directory=True)
    (workspace / "external").symlink_to(allowed, target_is_directory=True)
    stream = io.BytesIO(b"bytes\x00\xff")
    await session.write(Path("internal/nested/file"), stream)
    assert not stream.closed
    assert (workspace / "real/nested/file").read_bytes() == b"bytes\x00\xff"
    await session.write(Path("external/file"), io.BytesIO(b"allowed"))
    with await session.read(Path("external/file")) as handle:
        assert handle.read() == b"allowed"
    with await session.read(readonly / "file") as handle:
        assert handle.read() == b"read only"
    with pytest.raises(WorkspaceArchiveWriteError):
        await session.write(readonly / "file", io.BytesIO(b"denied"))
    listed = await session.ls(Path("internal/nested"))
    assert [entry.path for entry in listed] == [str(workspace / "real/nested/file")]
    await session.rm(Path("internal"), recursive=True)
    assert not (workspace / "real").exists()
    assert (allowed / "file").read_bytes() == b"allowed"


async def test_root_replacement_cannot_reauthorize_an_outside_directory(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_bytes(b"original content")
    session = _session(root)
    root.rename(tmp_path / "original")
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(InvalidManifestPathError):
        await session.write(Path("file"), io.BytesIO(b"new"))
    assert (outside / "file").read_bytes() == b"original content"


async def test_open_read_handle_survives_parent_replacement(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file").write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_bytes(b"original content")
    session = _session(root)
    with await session.read(Path("file")) as handle:
        root.rename(tmp_path / "original")
        root.symlink_to(outside, target_is_directory=True)
        assert handle.read() == b"inside"


async def test_copy_failure_closes_output_without_closing_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(tmp_path)
    opened: list[int] = []
    real_open = os.open

    def record_open(
        path: str | os.PathLike[str], flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        opened.append(fd)
        return fd

    class BrokenInput(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            raise RuntimeError("broken input")

    monkeypatch.setattr(os, "open", record_open)
    data = BrokenInput(b"data")
    with pytest.raises(RuntimeError, match="broken input"):
        await session.write(Path("file"), data)
    assert not data.closed
    for fd in set(opened):
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize("operation", ["read", "write"])
async def test_open_parent_stays_pinned_when_its_name_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    workspace = tmp_path / "workspace"
    parent = workspace / "parent"
    parent.mkdir(parents=True)
    (parent / "file").write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_bytes(b"original content")
    session = _session(workspace)
    real_open = os.open
    swapped = False

    def swap_after_open(
        path: str | os.PathLike[str], flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        nonlocal swapped
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == "parent" and dir_fd is not None:
            parent.rename(workspace / "original")
            parent.symlink_to(outside, target_is_directory=True)
            swapped = True
        return fd

    monkeypatch.setattr(os, "open", swap_after_open)
    result = await _operate(session, operation, Path("parent/file"))
    assert swapped
    assert (outside / "file").read_bytes() == b"original content"
    if operation == "read":
        assert result == b"inside"
    else:
        assert (workspace / "original/file").read_bytes() == b"new"


async def test_search_only_parent_does_not_require_directory_listing_permission(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "file").write_bytes(b"inside")
    session = _session(tmp_path)
    parent.chmod(0o111)
    try:
        with await session.read(Path("parent/file")) as handle:
            assert handle.read() == b"inside"
        await session.write(Path("parent/file"), io.BytesIO(b"new"))
        await session.mkdir(Path("parent"))
    finally:
        parent.chmod(0o700)
    assert (parent / "file").read_bytes() == b"new"


async def test_replacing_grant_alias_does_not_expand_authority(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    granted = tmp_path / "granted"
    granted.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file").write_bytes(b"original content")
    alias = tmp_path / "alias"
    alias.symlink_to(granted, target_is_directory=True)
    session = _session(workspace, grants=(SandboxPathGrant(path=str(alias)),))
    alias.unlink()
    alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(InvalidManifestPathError):
        await session.write(alias / "file", io.BytesIO(b"new"))
    assert (outside / "file").read_bytes() == b"original content"


async def test_mkdir_existing_directory_does_not_require_search_permission(tmp_path: Path) -> None:
    target = tmp_path / "locked"
    target.mkdir()
    session = _session(tmp_path)
    target.chmod(0)
    try:
        await session.mkdir(Path("locked"))
    finally:
        target.chmod(0o700)
    assert target.is_dir()


async def test_injected_session_uses_current_grants_without_rebinding_aliases(
    tmp_path: Path,
) -> None:
    class ConfigureGrants(Capability):
        type: str = "configure_grants"
        grants: tuple[SandboxPathGrant, ...]

        def process_manifest(self, manifest: Manifest) -> Manifest:
            return manifest.model_copy(update={"extra_path_grants": self.grants})

    workspace = tmp_path / "workspace"
    granted = tmp_path / "granted"
    added = tmp_path / "added"
    outside = tmp_path / "outside"
    for directory in (workspace, granted, added, outside):
        directory.mkdir()
    (outside / "file").write_bytes(b"original content")
    alias = tmp_path / "alias"
    alias.symlink_to(granted, target_is_directory=True)
    session = _session(workspace, grants=(SandboxPathGrant(path=str(alias), read_only=True),))
    agent = SandboxAgent(name="File grant lifecycle")
    manager = SandboxRuntimeSessionManager(
        starting_agent=agent, sandbox_config=SandboxRunConfig(session=session), run_state=None
    )

    async def configure(*grants: SandboxPathGrant) -> None:
        # Exercise the owner of capability updates without starting native process confinement.
        resources = await manager._create_resources(
            agent=agent, capabilities=[ConfigureGrants(grants=grants)], is_resumed_state=False
        )
        assert resources.session is session

    await configure(SandboxPathGrant(path=str(alias)), SandboxPathGrant(path=str(added)))
    await session.write(alias / "file", io.BytesIO(b"upgraded"))
    await session.mkdir(added / "directory")
    await session.write(added / "directory/file", io.BytesIO(b"added"))
    with await session.read(added / "directory/file") as stream:
        assert stream.read() == b"added"
    assert (granted / "file").read_bytes() == b"upgraded"
    await session.rm(added / "directory", recursive=True)

    alias.unlink()
    alias.symlink_to(outside, target_is_directory=True)
    await configure(
        SandboxPathGrant(path=str(alias)), SandboxPathGrant(path=str(added), read_only=True)
    )
    with pytest.raises(InvalidManifestPathError):
        await session.write(alias / "file", io.BytesIO(b"new"))
    with pytest.raises(WorkspaceArchiveWriteError):
        await session.write(added / "file", io.BytesIO(b"new"))
    assert (outside / "file").read_bytes() == b"original content"

    await configure()
    with pytest.raises(InvalidManifestPathError):
        await session.ls(added)


async def test_recursive_removal_keeps_worker_owned_until_cancelled_io_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    child = root / "child"
    child.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"original content")
    (child / "external").symlink_to(outside, target_is_directory=True)
    session = _session(root)
    started = threading.Event()
    release = threading.Event()
    real_scandir = os.scandir
    opened: list[int] = []

    def paused_scandir(path: int):
        opened.append(path)
        started.set()
        if not release.wait(timeout=5):
            raise RuntimeError("worker was not released")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", paused_scandir)
    task = asyncio.create_task(session.rm(Path("child"), recursive=True))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not child.exists()
    assert (outside / "sentinel").read_bytes() == b"original content"
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)
