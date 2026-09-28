"""Caller-visible no-clobber and partial-write recovery for UnixLocal Add File."""

from __future__ import annotations

import errno
import io
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from agents.editor import ApplyPatchOperation
from agents.sandbox.apply_patch import WorkspaceEditor
from agents.sandbox.errors import ApplyPatchDiffError, WorkspaceArchiveWriteError
from agents.sandbox.manifest import Manifest
from agents.sandbox.session import SandboxSession
from agents.sandbox.snapshot import NoopSnapshot

if TYPE_CHECKING or sys.platform != "win32":
    from agents.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxSession,
        UnixLocalSandboxSessionState,
    )

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Unix only")


@pytest.fixture(params=["direct", "wrapped", "bound-user"])
def editor(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> WorkspaceEditor:
    session = UnixLocalSandboxSession(
        state=UnixLocalSandboxSessionState(
            manifest=Manifest(root=str(tmp_path)), snapshot=NoopSnapshot(id="create-test")
        )
    )
    if request.param == "direct":
        return WorkspaceEditor(session)
    if request.param == "wrapped":
        return WorkspaceEditor(SandboxSession(session))

    # Execute the exact shipped worker body under the current test identity. Only the
    # sudo/process boundary is replaced; traversal, exclusive open and copy are real.
    def worker(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert command[:4] == ["/usr/bin/sudo", "-u", "example-user", "--"]
        assert command[4:8] == ["python3", "-I", "-S", "-c"]
        assert command[9] == "write_new"
        assert kwargs["env"] == {"PATH": os.defpath}
        assert kwargs["cwd"] == "/"
        namespace: dict[str, Any] = {"__name__": "test_worker"}
        exec(compile(command[8], "<user-file-worker>", "exec"), namespace)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(sys, "argv", ["-c", *command[9:]])
            patch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(kwargs["input"])))
            try:
                namespace["_main"]()
            except SystemExit as exc:
                return subprocess.CompletedProcess(command, int(exc.code), b"", b"")
            except OSError as exc:
                return subprocess.CompletedProcess(command, 1, b"", str(exc).encode())
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/sudo")
    monkeypatch.setattr(subprocess, "run", worker)
    return WorkspaceEditor(SandboxSession(session), user="example-user")


@pytest.mark.asyncio
async def test_create_preserves_a_creator_between_validation_and_open(
    editor: WorkspaceEditor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "notes.txt"
    original_open = os.open
    intervened = False

    def intervening_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal intervened
        if path == "notes.txt" and flags & os.O_CREAT:
            assert not target.exists()
            intervened = True
            target.write_bytes(b"other creator\n")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", intervening_open)
    with pytest.raises(ApplyPatchDiffError, match="already exists"):
        await editor.apply_operation(
            ApplyPatchOperation(type="create_file", path="notes.txt", diff="+replacement\n")
        )
    assert intervened
    assert target.read_bytes() == b"other creator\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_target", [False, True])
async def test_failed_create_requires_inspection_and_never_deletes_a_replacement(
    editor: WorkspaceEditor,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replace_target: bool,
) -> None:
    target = tmp_path / "notes.txt"

    def fail_copy(source: Any, destination: Any) -> None:
        destination.write(b"partial")
        destination.flush()
        if replace_target:
            target.unlink()
            target.write_bytes(b"new owner\n")
        raise OSError(errno.ENOSPC, "synthetic full filesystem")

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "copyfileobj", fail_copy)
        with pytest.raises(WorkspaceArchiveWriteError, match="Inspect the destination") as failure:
            await editor.apply_operation(
                ApplyPatchOperation(type="create_file", path="notes.txt", diff="+complete\n")
            )
    assert failure.value.retryable is False
    assert target.read_bytes() == (b"new owner\n" if replace_target else b"partial")
    with pytest.raises(ApplyPatchDiffError, match="already exists"):
        await editor.apply_operation(
            ApplyPatchOperation(type="create_file", path="notes.txt", diff="+complete\n")
        )
    assert target.read_bytes() == (b"new owner\n" if replace_target else b"partial")
    if not replace_target:
        # The caller has inspected and deliberately removed its incomplete result.
        target.unlink()
        result = await editor.apply_operation(
            ApplyPatchOperation(type="create_file", path="notes.txt", diff="+complete\n")
        )
        assert result.output == "Created notes.txt"
        assert target.read_bytes() == b"complete"


@pytest.mark.asyncio
async def test_create_writes_nested_file_and_rejects_dangling_leaf(
    editor: WorkspaceEditor, tmp_path: Path
) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "real", target_is_directory=True)
    result = await editor.apply_operation(
        ApplyPatchOperation(type="create_file", path="alias/nested/new.txt", diff="+contents\n")
    )
    assert result.output == "Created alias/nested/new.txt"
    assert (tmp_path / "real/nested/new.txt").read_bytes() == b"contents"
    (tmp_path / "alias/link.txt").symlink_to(tmp_path / "missing.txt")
    with pytest.raises(ApplyPatchDiffError, match="already exists"):
        await editor.apply_operation(
            ApplyPatchOperation(type="create_file", path="alias/link.txt", diff="+contents\n")
        )
    assert not (tmp_path / "missing.txt").exists()
    assert (tmp_path / "alias/link.txt").is_symlink()


@pytest.mark.asyncio
async def test_create_close_failure_reports_incomplete_contents(
    editor: WorkspaceEditor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_fdopen = os.fdopen

    class FailingClose:
        def __init__(self, stream: Any):
            self.stream = stream

        def __enter__(self) -> Any:
            return self.stream

        def write(self, data: bytes) -> int:
            return self.stream.write(data)

        def __exit__(self, *args: Any) -> None:
            self.stream.close()
            raise OSError(errno.ENOSPC, "synthetic flush failure")

    monkeypatch.setattr(os, "fdopen", lambda *a, **kw: FailingClose(original_fdopen(*a, **kw)))
    with pytest.raises(WorkspaceArchiveWriteError, match="Inspect the destination") as failure:
        await editor.apply_operation(
            ApplyPatchOperation(type="create_file", path="notes.txt", diff="+payload\n")
        )
    assert failure.value.retryable is False
    assert (tmp_path / "notes.txt").read_bytes() == b"payload"
