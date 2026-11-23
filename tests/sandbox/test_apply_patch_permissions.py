"""Alias moves must keep owner-only content private throughout staging."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path

import pytest

from agents.editor import ApplyPatchOperation
from agents.sandbox import Manifest
from agents.sandbox.errors import ApplyPatchDiffError

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Unix permissions required")


@pytest.mark.asyncio
@pytest.mark.parametrize("replaced", [False, True], ids=["private-partial", "foreign-replacement"])
async def test_alias_failed_creation_preserves_unproven_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replaced: bool
) -> None:
    import shutil

    from agents.sandbox.errors import WorkspaceArchiveWriteError
    from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

    source = tmp_path / "source.txt"
    destination = tmp_path / "alias.txt"
    source.write_text("old\n")
    source.chmod(0o600)
    destination.hardlink_to(source)
    session = await UnixLocalSandboxClient().create(manifest=Manifest(root=str(tmp_path)))

    # A stream failure after exclusive creation can coincide with another writer replacing it.
    def fail_copy(stream, out):
        stage = next(tmp_path.glob(".apply_patch-*.tmp"))
        if replaced:
            stage.unlink()
            stage.write_bytes(b"another writer")
        else:
            out.write(b"private partial")
        raise OSError("invented stream failure")

    monkeypatch.setattr(shutil, "copyfileobj", fail_copy)
    with pytest.raises(WorkspaceArchiveWriteError):
        await session.apply_patch(
            ApplyPatchOperation(
                type="update_file", path="source.txt", move_to="alias.txt", diff="@@\n-old\n+new\n"
            )
        )

    assert source.read_bytes() == destination.read_bytes() == b"old\n"
    assert stat.S_IMODE(source.stat().st_mode) == 0o600
    stages = list(tmp_path.glob(".apply_patch-*.tmp"))
    assert len(stages) == 1
    assert stages[0].read_bytes() == (b"another writer" if replaced else b"private partial")
    if not replaced:
        assert stat.S_IMODE(stages[0].stat().st_mode) == 0o600


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True], ids=["collision", "cancelled-collision"])
async def test_bound_user_alias_collision_preserves_foreign_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    import pwd
    import shutil
    import subprocess
    import threading

    from agents.sandbox.apply_patch import WorkspaceEditor
    from agents.sandbox.sandboxes._unix_local_file_ops import _EXISTING_TARGET_EXIT_CODE
    from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

    username = pwd.getpwuid(os.geteuid()).pw_name
    original_run = subprocess.run
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    collision_results: list[int] = []

    # The scripted session cannot preserve the real worker's collision result during cancellation.
    # Only sudo transport is replaced; events control completion of the shipped isolated worker.
    def run_worker(command, **kwargs):
        assert command[:4] == ["/usr/bin/sudo", "-u", username, "--"]
        if command[9] == "write_new_private":
            Path(command[10]).write_bytes(b"another writer")
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(timeout=10)
        result = original_run([sys.executable, *command[5:]], **kwargs)
        if command[9] == "write_new_private":
            collision_results.append(result.returncode)
        return result

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/sudo")
    monkeypatch.setattr(subprocess, "run", run_worker)
    source = tmp_path / "source.txt"
    destination = tmp_path / "alias.txt"
    source.write_text("old\n")
    source.chmod(0o600)
    destination.hardlink_to(source)
    session = await UnixLocalSandboxClient().create(manifest=Manifest(root=str(tmp_path)))

    async def check_read(path, *, user=None):
        assert os.access(path, os.R_OK)
        return path

    # Generic exec authorization is checked locally for the current test identity.
    async def check_remove(path, *, recursive=False, user=None):
        assert os.access(path.parent, os.W_OK | os.X_OK)
        return path

    monkeypatch.setattr(session._inner, "_check_read_with_exec", check_read)
    monkeypatch.setattr(session._inner, "_check_rm_with_exec", check_remove)
    task = asyncio.create_task(
        WorkspaceEditor(session, user=username).apply_operation(
            ApplyPatchOperation(
                type="update_file", path="source.txt", move_to="alias.txt", diff="@@\n-old\n+new\n"
            )
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        if cancelled:
            task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError if cancelled else FileExistsError):
            await task
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert collision_results == [_EXISTING_TARGET_EXIT_CODE]
    assert source.read_bytes() == destination.read_bytes() == b"old\n"
    assert stat.S_IMODE(source.stat().st_mode) == 0o600
    stages = list(tmp_path.glob(".apply_patch-*.tmp"))
    assert len(stages) == 1
    assert stages[0].read_bytes() == b"another writer"


@pytest.mark.asyncio
async def test_alias_move_failure_keeps_owner_only_content_private(tmp_path: Path) -> None:
    from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("invented private text\nold\n")
    source.chmod(0o600)
    destination.hardlink_to(source)
    session = await UnixLocalSandboxClient().create(manifest=Manifest(root=str(tmp_path)))
    previous_umask = os.umask(0o022)
    try:
        with pytest.raises(ApplyPatchDiffError):
            await session.apply_patch(
                ApplyPatchOperation(
                    type="update_file",
                    path="source.txt",
                    move_to="destination.txt",
                    diff="@@\n invented private text\n-old\n+new\n",
                )
            )
    finally:
        os.umask(previous_umask)

    assert source.read_text() == "invented private text\nold\n"
    assert stat.S_IMODE(source.stat().st_mode) == 0o600
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".apply_patch-*"))


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_publication", [False, True])
async def test_alias_staging_is_private_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_publication: bool
) -> None:
    from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

    source = tmp_path / "notes.txt"
    destination = tmp_path / "alias.txt"
    source.write_text("invented private text\nold\n")
    source.chmod(0o600)
    destination.hardlink_to(source)
    session = await UnixLocalSandboxClient().create(manifest=Manifest(root=str(tmp_path)))
    original_open = os.open
    original_move = session._inner.mv
    created_private = False
    inspected_staging = False

    # Real syscalls are retained so this checks creation permissions before any chmod or write.
    def inspect_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal created_private
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if str(path).startswith(".apply_patch-") and flags & os.O_CREAT:
            created_private = True
            assert flags & os.O_EXCL
            assert stat.S_IMODE(os.fstat(fd).st_mode) == 0o600
        return fd

    async def inspect_move(staging, destination, *, user=None):
        nonlocal inspected_staging
        inspected_staging = True
        assert staging.read_text() == "invented private text\nnew\n"
        assert stat.S_IMODE(staging.stat().st_mode) == 0o600
        assert source.read_text() == "invented private text\nold\n"
        if fail_publication:
            raise ConnectionError("invented publication failure")
        await original_move(staging, destination, user=user)

    monkeypatch.setattr(os, "open", inspect_open)
    monkeypatch.setattr(session._inner, "mv", inspect_move)
    previous_umask = os.umask(0o022)
    try:
        operation = ApplyPatchOperation(
            type="update_file",
            path="notes.txt",
            move_to="alias.txt",
            diff="@@\n invented private text\n-old\n+new\n",
        )
        if fail_publication:
            with pytest.raises(ConnectionError, match="invented publication failure"):
                await session.apply_patch(operation)
        else:
            with pytest.raises(ApplyPatchDiffError):
                await session.apply_patch(operation)
    finally:
        os.umask(previous_umask)

    assert created_private and inspected_staging
    assert source.read_text() == "invented private text\nold\n"
    assert destination.read_text() == (
        "invented private text\nold\n" if fail_publication else "invented private text\nnew\n"
    )
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert stat.S_IMODE(source.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".apply_patch-*"))


@pytest.mark.asyncio
async def test_bound_user_alias_move_keeps_content_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pwd
    import shutil
    import subprocess

    from agents.sandbox.apply_patch import WorkspaceEditor
    from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

    username = pwd.getpwuid(os.geteuid()).pw_name
    original_run = subprocess.run

    # Only sudo is replaced; the shipped isolated worker runs under the current test identity.
    def run_worker(command, **kwargs):
        assert command[:4] == ["/usr/bin/sudo", "-u", username, "--"]
        return original_run([sys.executable, *command[5:]], **kwargs)

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/sudo")
    monkeypatch.setattr(subprocess, "run", run_worker)
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("invented private text\nold\n")
    source.chmod(0o600)
    destination.hardlink_to(source)
    session = await UnixLocalSandboxClient().create(manifest=Manifest(root=str(tmp_path)))

    # Generic sandbox exec is outside the worker boundary; check this identity's access locally.
    async def check_read(path, *, user=None):
        assert os.access(path, os.R_OK)
        return path

    monkeypatch.setattr(session._inner, "_check_read_with_exec", check_read)
    previous_umask = os.umask(0o022)
    try:
        with pytest.raises(ApplyPatchDiffError):
            await WorkspaceEditor(session, user=username).apply_operation(
                ApplyPatchOperation(
                    type="update_file",
                    path="source.txt",
                    move_to="destination.txt",
                    diff="@@\n invented private text\n-old\n+new\n",
                )
            )
    finally:
        os.umask(previous_umask)

    assert source.read_text() == "invented private text\nold\n"
    assert stat.S_IMODE(source.stat().st_mode) == 0o600
    assert destination.read_text() == "invented private text\nnew\n"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".apply_patch-*"))


@pytest.mark.asyncio
async def test_docker_alias_move_rejects_before_writing_or_removing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock, Mock

    from agents.sandbox.sandboxes.docker import DockerSandboxSession, DockerSandboxSessionState
    from agents.sandbox.session.sandbox_session import SandboxSession
    from agents.sandbox.snapshot import NoopSnapshot

    source = tmp_path / "source.txt"
    alias = tmp_path / "alias.txt"
    source.write_bytes(b"old\n")
    source.chmod(0o600)
    alias.hardlink_to(source)
    backend = DockerSandboxSession(
        docker_client=Mock(),
        container=Mock(),
        state=DockerSandboxSessionState(
            manifest=Manifest(root=str(tmp_path)),
            snapshot=NoopSnapshot(id="snapshot"),
            image="test",
            container_id="test",
        ),
    )

    # Use local reads and file identity but keep Docker's staging decision.
    async def read(path, *, user=None):
        return path.open("rb")

    async def same_file(left, right, **kwargs):
        return left.samefile(right)

    async def reject_mutation(*args, **kwargs):
        pytest.fail("Docker alias staging attempted a filesystem mutation")

    monkeypatch.setattr(backend, "read", read)
    monkeypatch.setattr(backend, "same_file", same_file)
    monkeypatch.setattr(
        backend, "_validate_path_access", AsyncMock(side_effect=backend.normalize_path)
    )
    for operation in ("mkdir", "write", "_stream_into_exec", "mv", "rm"):
        monkeypatch.setattr(backend, operation, reject_mutation)

    with pytest.raises(NotImplementedError, match="owner-only alias staging"):
        await SandboxSession(backend).apply_patch(
            ApplyPatchOperation(
                type="update_file", path="source.txt", move_to="alias.txt", diff="@@\n-old\n+new\n"
            )
        )

    assert source.samefile(alias)
    assert source.read_bytes() == b"old\n"
    assert stat.S_IMODE(source.stat().st_mode) == 0o600
    assert set(tmp_path.iterdir()) == {source, alias}


@pytest.mark.asyncio
async def test_real_parent_alias_update_preserves_protection_and_normal_creation(
    tmp_path: Path,
) -> None:
    from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real, target_is_directory=True)
    source = real / "notes.txt"
    source.write_text("old\n")
    source.chmod(0o600)
    session = await UnixLocalSandboxClient().create(manifest=Manifest(root=str(tmp_path)))
    previous_umask = os.umask(0o022)
    try:
        await session.apply_patch(
            ApplyPatchOperation(
                type="update_file",
                path="real/notes.txt",
                move_to="alias/notes.txt",
                diff="@@\n-old\n+new\n",
            )
        )
        await session.apply_patch(
            ApplyPatchOperation(type="create_file", path="ordinary.txt", diff="+ordinary\n")
        )
    finally:
        os.umask(previous_umask)

    assert source.read_text() == "new\n"
    assert stat.S_IMODE(source.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "ordinary.txt").stat().st_mode) == 0o644
