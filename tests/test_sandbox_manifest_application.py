from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agents.sandbox.entries import Dir, File, GCSMount
from agents.sandbox.manifest import Manifest
from agents.sandbox.materialization import MaterializedFile
from agents.sandbox.session.manifest_application import ManifestApplier
from agents.sandbox.types import ExecResult, Group, User


def _materialized(dest: Path) -> list[MaterializedFile]:
    return [MaterializedFile(path=dest, sha256=dest.as_posix())]


@pytest.mark.asyncio
async def test_manifest_applier_only_applies_ephemeral_entries_without_account_provisioning() -> (
    None
):
    mkdir_calls: list[Path] = []
    exec_calls: list[tuple[str, ...]] = []
    apply_calls: list[tuple[str, Path, Path]] = []

    async def mkdir(path: Path) -> None:
        mkdir_calls.append(path)

    async def exec_checked_nonzero(*command: str) -> ExecResult:
        exec_calls.append(command)
        return ExecResult(stdout=b"", stderr=b"", exit_code=0)

    async def apply_entry(entry: object, dest: Path, base_dir: Path) -> list[MaterializedFile]:
        apply_calls.append((type(entry).__name__, dest, base_dir))
        return _materialized(dest)

    applier = ManifestApplier(
        mkdir=mkdir,
        exec_checked_nonzero=exec_checked_nonzero,
        apply_entry=apply_entry,
    )
    manifest = Manifest(
        root="/workspace",
        entries={
            "keep.txt": File(content=b"keep"),
            "tmp.txt": File(content=b"tmp", ephemeral=True),
        },
        users=[User(name="alice")],
        groups=[Group(name="dev", users=[User(name="alice")])],
    )

    result = await applier.apply_manifest(manifest, only_ephemeral=True)

    assert mkdir_calls == [Path("/workspace")]
    assert exec_calls == []
    assert apply_calls == [("File", Path("/workspace/tmp.txt"), Path("/"))]
    assert result.files == _materialized(Path("/workspace/tmp.txt"))


@pytest.mark.asyncio
async def test_manifest_applier_only_ephemeral_reapplies_nested_ephemeral_children() -> None:
    apply_calls: list[tuple[str, Path, Path]] = []

    async def mkdir(_path: Path) -> None:
        return None

    async def exec_checked_nonzero(*_command: str) -> ExecResult:
        return ExecResult(stdout=b"", stderr=b"", exit_code=0)

    async def apply_entry(entry: object, dest: Path, base_dir: Path) -> list[MaterializedFile]:
        apply_calls.append((type(entry).__name__, dest, base_dir))
        return _materialized(dest)

    applier = ManifestApplier(
        mkdir=mkdir,
        exec_checked_nonzero=exec_checked_nonzero,
        apply_entry=apply_entry,
    )
    manifest = Manifest(
        root="/workspace",
        entries={
            "dir": Dir(
                children={
                    "keep.txt": File(content=b"keep"),
                    "tmp.txt": File(content=b"tmp", ephemeral=True),
                }
            )
        },
    )

    result = await applier.apply_manifest(manifest, only_ephemeral=True)

    assert apply_calls == [("File", Path("/workspace/dir/tmp.txt"), Path("/"))]
    assert result.files == _materialized(Path("/workspace/dir/tmp.txt"))


@pytest.mark.asyncio
async def test_manifest_applier_only_ephemeral_reapplies_full_ephemeral_directories() -> None:
    applied_entries: list[tuple[object, Path, Path]] = []

    async def mkdir(_path: Path) -> None:
        return None

    async def exec_checked_nonzero(*_command: str) -> ExecResult:
        return ExecResult(stdout=b"", stderr=b"", exit_code=0)

    async def apply_entry(entry: object, dest: Path, base_dir: Path) -> list[MaterializedFile]:
        applied_entries.append((entry, dest, base_dir))
        return _materialized(dest)

    applier = ManifestApplier(
        mkdir=mkdir,
        exec_checked_nonzero=exec_checked_nonzero,
        apply_entry=apply_entry,
    )
    manifest = Manifest(
        root="/workspace",
        entries={
            "tmp": Dir(
                ephemeral=True,
                children={
                    "keep.txt": File(content=b"keep"),
                    "nested": Dir(children={"child.txt": File(content=b"child")}),
                    "tmp.txt": File(content=b"tmp", ephemeral=True),
                },
            )
        },
    )

    result = await applier.apply_manifest(manifest, only_ephemeral=True)

    assert len(applied_entries) == 1
    entry, dest, base_dir = applied_entries[0]
    assert isinstance(entry, Dir)
    assert dest == Path("/workspace/tmp")
    assert base_dir == Path("/")
    assert set(entry.children) == {"keep.txt", "nested", "tmp.txt"}
    assert result.files == _materialized(Path("/workspace/tmp"))


@pytest.mark.asyncio
async def test_manifest_applier_respects_explicit_base_dir() -> None:
    apply_calls: list[tuple[str, Path, Path]] = []

    async def mkdir(_path: Path) -> None:
        return None

    async def exec_checked_nonzero(*_command: str) -> ExecResult:
        return ExecResult(stdout=b"", stderr=b"", exit_code=0)

    async def apply_entry(entry: object, dest: Path, base_dir: Path) -> list[MaterializedFile]:
        apply_calls.append((type(entry).__name__, dest, base_dir))
        return _materialized(dest)

    applier = ManifestApplier(
        mkdir=mkdir,
        exec_checked_nonzero=exec_checked_nonzero,
        apply_entry=apply_entry,
    )
    manifest = Manifest(entries={"file.txt": File(content=b"hello")})

    result = await applier.apply_manifest(manifest, base_dir=Path("/tmp/project"))

    assert apply_calls == [("File", Path("/workspace/file.txt"), Path("/tmp/project"))]
    assert result.files == _materialized(Path("/workspace/file.txt"))


@pytest.mark.asyncio
async def test_manifest_applier_provisions_groups_and_unique_users_before_entries() -> None:
    exec_calls: list[tuple[str, ...]] = []

    async def mkdir(_path: Path) -> None:
        return None

    async def exec_checked_nonzero(*command: str) -> ExecResult:
        exec_calls.append(command)
        return ExecResult(stdout=b"", stderr=b"", exit_code=0)

    async def apply_entry(_entry: object, _dest: Path, _base_dir: Path) -> list[MaterializedFile]:
        return []

    applier = ManifestApplier(
        mkdir=mkdir,
        exec_checked_nonzero=exec_checked_nonzero,
        apply_entry=apply_entry,
    )
    manifest = Manifest(
        users=[User(name="alice")],
        groups=[Group(name="dev", users=[User(name="alice"), User(name="bob")])],
    )

    result = await applier.apply_manifest(manifest)

    assert result.files == []
    assert exec_calls[0] == ("groupadd", "dev")
    assert exec_calls.count(("groupadd", "alice")) == 0
    assert exec_calls.count(("groupadd", "bob")) == 0
    assert ("useradd", "-U", "-M", "-s", "/usr/sbin/nologin", "alice") in exec_calls
    assert ("useradd", "-U", "-M", "-s", "/usr/sbin/nologin", "bob") in exec_calls
    assert ("usermod", "-aG", "dev", "alice") in exec_calls
    assert ("usermod", "-aG", "dev", "bob") in exec_calls


@pytest.mark.asyncio
async def test_apply_entry_batch_flushes_parallel_work_before_overlapping_paths() -> None:
    events: list[tuple[str, Path]] = []

    async def mkdir(_path: Path) -> None:
        return None

    async def exec_checked_nonzero(*_command: str) -> ExecResult:
        return ExecResult(stdout=b"", stderr=b"", exit_code=0)

    async def apply_entry(_entry: object, dest: Path, _base_dir: Path) -> list[MaterializedFile]:
        events.append(("start", dest))
        await asyncio.sleep(0)
        events.append(("end", dest))
        return _materialized(dest)

    applier = ManifestApplier(
        mkdir=mkdir,
        exec_checked_nonzero=exec_checked_nonzero,
        apply_entry=apply_entry,
    )
    destinations = [
        Path("/workspace/alpha.txt"),
        Path("/workspace/beta.txt"),
        Path("/workspace/nested"),
        Path("/workspace/nested/child.txt"),
    ]

    files = await applier._apply_entry_batch(
        [
            (destinations[0], File(content=b"a")),
            (destinations[1], File(content=b"b")),
            (destinations[2], Dir()),
            (destinations[3], File(content=b"c")),
        ],
        base_dir=Path("/"),
    )

    assert [file.path for file in files] == destinations
    child_start = events.index(("start", destinations[3]))
    assert events.index(("end", destinations[0])) < child_start
    assert events.index(("end", destinations[1])) < child_start
    assert events.index(("end", destinations[2])) < child_start


@pytest.mark.asyncio
async def test_apply_entry_batch_flushes_before_and_after_mount_entries() -> None:
    events: list[tuple[str, Path]] = []

    async def mkdir(_path: Path) -> None:
        return None

    async def exec_checked_nonzero(*_command: str) -> ExecResult:
        return ExecResult(stdout=b"", stderr=b"", exit_code=0)

    async def apply_entry(_entry: object, dest: Path, _base_dir: Path) -> list[MaterializedFile]:
        events.append(("start", dest))
        await asyncio.sleep(0)
        events.append(("end", dest))
        return _materialized(dest)

    applier = ManifestApplier(
        mkdir=mkdir,
        exec_checked_nonzero=exec_checked_nonzero,
        apply_entry=apply_entry,
    )
    destinations = [
        Path("/workspace/alpha.txt"),
        Path("/workspace/beta.txt"),
        Path("/workspace/mount"),
        Path("/workspace/gamma.txt"),
    ]

    files = await applier._apply_entry_batch(
        [
            (destinations[0], File(content=b"a")),
            (destinations[1], File(content=b"b")),
            (destinations[2], GCSMount(bucket="sandbox-bucket")),
            (destinations[3], File(content=b"c")),
        ],
        base_dir=Path("/"),
    )

    assert [file.path for file in files] == destinations
    mount_start = events.index(("start", destinations[2]))
    gamma_start = events.index(("start", destinations[3]))
    assert events.index(("end", destinations[0])) < mount_start
    assert events.index(("end", destinations[1])) < mount_start
    assert events.index(("end", destinations[2])) < gamma_start
