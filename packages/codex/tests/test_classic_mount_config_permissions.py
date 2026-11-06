"""UnixLocal mount credentials remain private throughout creation and replacement."""

import io
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="UnixLocal requires Unix")

if sys.platform != "win32":
    from agents.classic.sandbox import Manifest
    from agents.classic.sandbox.entries.mounts.patterns import _write_sensitive_config_file
    from agents.classic.sandbox.errors import WorkspaceArchiveWriteError
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxSession,
        UnixLocalSandboxSessionState,
    )
    from agents.classic.sandbox.session.sandbox_session import SandboxSession
    from agents.classic.sandbox.snapshot import NoopSnapshot


@pytest.fixture(params=[False, True], ids=["backend", "public-wrapper"])
def local_session(tmp_path: Path, request: pytest.FixtureRequest):
    session = UnixLocalSandboxSession(
        state=UnixLocalSandboxSessionState(
            manifest=Manifest(root=str(tmp_path)), snapshot=NoopSnapshot(id="mount-config-test")
        )
    )
    return SandboxSession(session) if request.param else session


@pytest.mark.parametrize("mask", [0o022, 0o000, 0o777])
async def test_sensitive_config_is_private_before_first_payload_byte(
    local_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mask: int
) -> None:
    target = tmp_path / "config" / "synthetic.conf"
    target.parent.mkdir()
    original_copy = shutil.copyfileobj
    observed = []

    def inspect_copy(source, output):
        files = [path for path in target.parent.iterdir() if path.is_file()]
        observed.extend(stat.S_IMODE(path.stat().st_mode) for path in files)
        assert observed and all(mode == 0o600 for mode in observed)
        original_copy(source, output)

    monkeypatch.setattr("shutil.copyfileobj", inspect_copy)
    previous = os.umask(mask)
    try:
        await _write_sensitive_config_file(local_session, target, b"synthetic-secret")
    finally:
        os.umask(previous)
    assert target.read_bytes() == b"synthetic-secret"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(target.parent.iterdir()) == [target]


async def test_sensitive_replacement_does_not_update_retained_public_descriptor(
    local_session, tmp_path: Path
) -> None:
    target = tmp_path / "synthetic.conf"
    target.write_bytes(b"old-public-content")
    target.chmod(0o644)
    alias = tmp_path / "old-hardlink"
    os.link(target, alias)
    with target.open("rb") as retained:
        await _write_sensitive_config_file(local_session, target, b"new-synthetic-secret")
        assert retained.read() == b"old-public-content"
    assert alias.read_bytes() == b"old-public-content"
    assert target.read_bytes() == b"new-synthetic-secret"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
async def test_sensitive_config_rejects_unsafe_existing_destination(
    local_session, tmp_path: Path, kind: str
) -> None:
    target = tmp_path / "synthetic.conf"
    original = tmp_path / "original"
    original.write_bytes(b"unchanged")
    if kind == "symlink":
        target.symlink_to(original)
    elif kind == "directory":
        target.mkdir()
    else:
        os.mkfifo(target)
    with pytest.raises(WorkspaceArchiveWriteError):
        await _write_sensitive_config_file(local_session, target, b"synthetic-secret")
    assert original.read_bytes() == b"unchanged"
    assert not list(tmp_path.glob(".mount-config-*"))


async def test_failed_sensitive_write_preserves_old_file_and_cleans_partial_copy(
    local_session, tmp_path: Path
) -> None:
    target = tmp_path / "synthetic.conf"
    target.write_bytes(b"old-content")

    class Interrupted(io.BytesIO):
        def read(self, size=-1):
            raise OSError("synthetic input failure")

    with pytest.raises(WorkspaceArchiveWriteError):
        await local_session._write_sensitive_file(target, Interrupted(b"synthetic-secret"))
    assert target.read_bytes() == b"old-content"
    assert list(tmp_path.iterdir()) == [target]


async def test_ordinary_write_keeps_existing_permission_semantics(local_session, tmp_path: Path):
    target = tmp_path / "ordinary.txt"
    previous = os.umask(0o022)
    try:
        await local_session.write(target, io.BytesIO(b"ordinary-content"))
    finally:
        os.umask(previous)
    assert target.read_bytes() == b"ordinary-content"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
