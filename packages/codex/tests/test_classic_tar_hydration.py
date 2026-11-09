"""UnixLocal archive hydration stays inside its configured workspace."""

from __future__ import annotations

import io
import os
import stat
import sys
import tarfile
from pathlib import Path

import pytest

from agents.classic.sandbox import Manifest
from agents.classic.sandbox.errors import WorkspaceArchiveWriteError
from agents.classic.sandbox.snapshot import NoopSnapshot
from agents.classic.sandbox.util import safe_extract_tarfile

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="UnixLocal requires Unix")

if sys.platform != "win32":
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxSession,
        UnixLocalSandboxSessionState,
    )


def _session(root: Path) -> UnixLocalSandboxSession:
    return UnixLocalSandboxSession(
        state=UnixLocalSandboxSessionState(
            manifest=Manifest(root=str(root)), snapshot=NoopSnapshot(id="tar-hydration-test")
        )
    )


def _archive(*members: tuple[tarfile.TarInfo, bytes | None]) -> io.BytesIO:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        for member, payload in members:
            archive.addfile(member, io.BytesIO(payload) if payload is not None else None)
    data.seek(0)
    return data


@pytest.mark.parametrize("kind", ["file", "directory", "symlink"])
async def test_hydration_parent_swap_cannot_change_outside_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    root = tmp_path / "workspace"
    parent = root / "mutable"
    parent.mkdir(parents=True)
    (parent / "sentinel").write_bytes(b"old workspace file")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"outside original")

    member = tarfile.TarInfo("mutable/sentinel")
    payload: bytes | None = None
    if kind == "file":
        payload = b"archive file"
        member.size = len(payload)
    elif kind == "directory":
        member.type = tarfile.DIRTYPE
    else:
        member.type = tarfile.SYMTYPE
        member.linkname = "target"

    real_unlink = os.unlink
    swapped = False

    def swap_before_unlink(path: str | Path, *, dir_fd: int | None = None) -> None:
        nonlocal swapped
        if not swapped and Path(path).name == "sentinel":
            swapped = True
            parent.rename(root / "moved")
            parent.symlink_to(outside, target_is_directory=True)
        real_unlink(path, dir_fd=dir_fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", swap_before_unlink)
        await _session(root).hydrate_workspace(_archive((member, payload)))

    assert swapped
    assert sentinel.read_bytes() == b"outside original"
    restored = root / "moved" / "sentinel"
    if kind == "file":
        assert restored.read_bytes() == b"archive file"
    elif kind == "directory":
        assert restored.is_dir()
    else:
        assert restored.is_symlink()
        assert os.readlink(restored) == "target"


async def test_hydration_restores_nested_files_modes_and_relative_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "replace.txt").write_bytes(b"old")
    directory = tarfile.TarInfo("nested")
    directory.type = tarfile.DIRTYPE
    executable = tarfile.TarInfo("nested/run.sh")
    executable.mode = 0o755
    executable.size = len(b"#!/bin/sh\n")
    replacement = tarfile.TarInfo("replace.txt")
    replacement.size = len(b"new")
    link = tarfile.TarInfo("nested/run-link")
    link.type = tarfile.SYMTYPE
    link.linkname = "run.sh"

    await _session(root).hydrate_workspace(
        _archive(
            (directory, None),
            (executable, b"#!/bin/sh\n"),
            (replacement, b"new"),
            (link, None),
        )
    )

    assert (root / "replace.txt").read_bytes() == b"new"
    assert (root / "nested" / "run.sh").read_bytes() == b"#!/bin/sh\n"
    assert stat.S_IMODE((root / "nested" / "run.sh").stat().st_mode) == 0o755
    assert (root / "nested" / "run-link").is_symlink()
    assert os.readlink(root / "nested" / "run-link") == "run.sh"


async def test_hydration_uses_configured_root_after_alias_replacement(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    alias = tmp_path / "workspace-alias"
    alias.symlink_to(root, target_is_directory=True)
    session = _session(alias)
    outside = tmp_path / "outside"
    outside.mkdir()
    alias.unlink()
    alias.symlink_to(outside, target_is_directory=True)

    member = tarfile.TarInfo("restored.txt")
    member.size = len(b"restored")
    await session.hydrate_workspace(_archive((member, b"restored")))

    assert (root / "restored.txt").read_bytes() == b"restored"
    assert not (outside / "restored.txt").exists()


async def test_hydration_rejects_replaced_root_ancestor(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    root = parent / "workspace"
    root.mkdir(parents=True)
    session = _session(root)
    outside = tmp_path / "outside"
    (outside / "workspace").mkdir(parents=True)
    parent.rename(tmp_path / "moved")
    parent.symlink_to(outside, target_is_directory=True)

    member = tarfile.TarInfo("restored.txt")
    member.size = len(b"archive")
    with pytest.raises(WorkspaceArchiveWriteError):
        await session.hydrate_workspace(_archive((member, b"archive")))

    assert not (outside / "workspace" / "restored.txt").exists()


@pytest.mark.parametrize("existing_root", [False, True])
def test_direct_tar_extraction_resolves_trusted_symlinked_ancestor(
    tmp_path: Path, existing_root: bool
) -> None:
    target = tmp_path / "real"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    root = target / "workspace"
    if existing_root:
        root.mkdir()
    member = tarfile.TarInfo("restored.txt")
    member.size = len(b"archive")

    with tarfile.open(fileobj=_archive((member, b"archive")), mode="r:*") as archive:
        safe_extract_tarfile(archive, root=alias / "workspace")

    assert (root / "restored.txt").read_bytes() == b"archive"
