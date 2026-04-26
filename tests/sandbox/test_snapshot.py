from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Literal

import pytest
from pydantic import PrivateAttr, ValidationError

from agents.sandbox import Manifest, RemoteSnapshot, RemoteSnapshotSpec, resolve_snapshot
from agents.sandbox.entries import File
from agents.sandbox.errors import SnapshotPersistError
from agents.sandbox.materialization import MaterializationResult
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxSessionState
from agents.sandbox.session import Dependencies, SandboxSessionState
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.sandbox_session import SandboxSession
from agents.sandbox.snapshot import LocalSnapshot, NoopSnapshot, SnapshotBase
from agents.sandbox.types import ExecResult, User
from tests.utils.factories import TestSessionState


class TestNoopSnapshot(SnapshotBase):
    __test__ = False
    type: Literal["test-noop"] = "test-noop"

    async def persist(self, data: io.IOBase, *, dependencies: Dependencies | None = None) -> None:
        _ = (data, dependencies)

    async def restore(self, *, dependencies: Dependencies | None = None) -> io.IOBase:
        _ = dependencies
        raise FileNotFoundError(Path("<test-noop>"))

    async def restorable(self, *, dependencies: Dependencies | None = None) -> bool:
        _ = dependencies
        return False


class TestRestorableSnapshot(SnapshotBase):
    __test__ = False
    type: Literal["test-restorable"] = "test-restorable"
    payload: bytes = b"restored-workspace"

    async def persist(self, data: io.IOBase, *, dependencies: Dependencies | None = None) -> None:
        _ = (data, dependencies)

    async def restore(self, *, dependencies: Dependencies | None = None) -> io.IOBase:
        _ = dependencies
        return io.BytesIO(self.payload)

    async def restorable(self, *, dependencies: Dependencies | None = None) -> bool:
        _ = dependencies
        return True


class _TrackingBytesIO(io.BytesIO):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        super().close()


class TestClosingRestoreSnapshot(SnapshotBase):
    __test__ = False
    type: Literal["test-closing-restore"] = "test-closing-restore"
    payload: bytes = b"restored-workspace"
    _stream: _TrackingBytesIO = PrivateAttr()

    def model_post_init(self, __context: object) -> None:
        del __context
        self._stream = _TrackingBytesIO(self.payload)

    async def persist(self, data: io.IOBase, *, dependencies: Dependencies | None = None) -> None:
        _ = (data, dependencies)

    async def restore(self, *, dependencies: Dependencies | None = None) -> io.IOBase:
        _ = dependencies
        return self._stream

    async def restorable(self, *, dependencies: Dependencies | None = None) -> bool:
        _ = dependencies
        return True


def test_sandbox_session_state_roundtrip_preserves_custom_snapshot_type() -> None:
    state = TestSessionState(
        manifest=Manifest(),
        snapshot=TestNoopSnapshot(id="custom-snapshot"),
        snapshot_fingerprint="deadbeef",
        snapshot_fingerprint_version="workspace_tar_sha256_v1",
    )

    payload = state.model_dump_json()
    restored = SandboxSessionState.model_validate_json(payload)

    assert isinstance(restored.snapshot, TestNoopSnapshot)
    assert restored.snapshot.id == "custom-snapshot"
    assert restored.snapshot_fingerprint == "deadbeef"
    assert restored.snapshot_fingerprint_version == "workspace_tar_sha256_v1"


def test_sandbox_session_state_model_dump_preserves_snapshot_subclass_fields() -> None:
    state = TestSessionState(
        manifest=Manifest(),
        snapshot=LocalSnapshot(id="local-snapshot", base_path=Path("/tmp/snapshots")),
    )

    payload = state.model_dump()

    assert payload["snapshot"] == {
        "type": "local",
        "id": "local-snapshot",
        "base_path": Path("/tmp/snapshots"),
    }


def test_sandbox_session_state_model_dump_exclude_unset_preserves_snapshot_fields() -> None:
    state = TestSessionState(
        manifest=Manifest(),
        snapshot=LocalSnapshot(id="local-snapshot", base_path=Path("/tmp/snapshots")),
    )

    payload = state.model_dump(exclude_unset=True)

    assert payload["snapshot"] == {
        "type": "local",
        "id": "local-snapshot",
        "base_path": Path("/tmp/snapshots"),
    }


def test_backend_session_state_model_dump_roundtrip_preserves_local_snapshot_fields() -> None:
    state = UnixLocalSandboxSessionState(
        manifest=Manifest(),
        snapshot=LocalSnapshot(id="local-snapshot", base_path=Path("/tmp/snapshots")),
    )

    payload = state.model_dump()
    restored = UnixLocalSandboxSessionState.model_validate(payload)

    assert isinstance(restored.snapshot, LocalSnapshot)
    assert restored.snapshot.base_path == Path("/tmp/snapshots")


def test_snapshot_exclude_unset_preserves_type_discriminator() -> None:
    payload = LocalSnapshot(id="local-snapshot", base_path=Path("/tmp/snapshots")).model_dump(
        exclude_unset=True
    )

    assert payload == {
        "type": "local",
        "id": "local-snapshot",
        "base_path": Path("/tmp/snapshots"),
    }


@pytest.mark.asyncio
async def test_local_snapshot_restorable_requires_file(tmp_path: Path) -> None:
    snapshot = LocalSnapshot(id="local-snapshot", base_path=tmp_path)
    snapshot_path = tmp_path / "local-snapshot.tar"

    assert await snapshot.restorable() is False

    snapshot_path.mkdir()

    assert await snapshot.restorable() is False

    snapshot_path.rmdir()
    snapshot_path.write_bytes(b"workspace")

    assert await snapshot.restorable() is True


def test_snapshot_parse_uses_registered_custom_snapshot_type() -> None:
    parsed = SnapshotBase.parse({"type": "test-noop", "id": "registered"})

    assert isinstance(parsed, TestNoopSnapshot)
    assert parsed.id == "registered"


def test_snapshot_models_are_frozen() -> None:
    snapshot = LocalSnapshot(id="local-snapshot", base_path=Path("/tmp/snapshots"))

    with pytest.raises(ValidationError) as exc_info:
        snapshot.id = "changed"

    assert exc_info.value.errors(include_url=False) == [
        {
            "type": "frozen_instance",
            "loc": ("id",),
            "msg": "Instance is frozen",
            "input": "changed",
        }
    ]


def test_duplicate_snapshot_type_registration_raises() -> None:
    class TestDuplicateSnapshotA(SnapshotBase):
        __test__ = False
        type: Literal["test-duplicate"] = "test-duplicate"

        async def persist(
            self, data: io.IOBase, *, dependencies: Dependencies | None = None
        ) -> None:
            _ = (data, dependencies)

        async def restore(self, *, dependencies: Dependencies | None = None) -> io.IOBase:
            _ = dependencies
            raise FileNotFoundError(Path("<test-duplicate-a>"))

        async def restorable(self, *, dependencies: Dependencies | None = None) -> bool:
            _ = dependencies
            return False

    _ = TestDuplicateSnapshotA

    with pytest.raises(TypeError, match="already registered"):

        class TestDuplicateSnapshotB(SnapshotBase):
            __test__ = False
            type: Literal["test-duplicate"] = "test-duplicate"

            async def persist(
                self, data: io.IOBase, *, dependencies: Dependencies | None = None
            ) -> None:
                _ = (data, dependencies)

            async def restore(self, *, dependencies: Dependencies | None = None) -> io.IOBase:
                _ = dependencies
                raise FileNotFoundError(Path("<test-duplicate-b>"))

            async def restorable(self, *, dependencies: Dependencies | None = None) -> bool:
                _ = dependencies
                return False


def test_snapshot_subclasses_require_type_discriminator_default() -> None:
    with pytest.raises(TypeError, match="must define a non-empty string default for `type`"):

        class TestMissingTypeSnapshot(SnapshotBase):
            __test__ = False

            async def persist(
                self, data: io.IOBase, *, dependencies: Dependencies | None = None
            ) -> None:
                _ = (data, dependencies)

            async def restore(self, *, dependencies: Dependencies | None = None) -> io.IOBase:
                _ = dependencies
                raise FileNotFoundError(Path("<test-missing-type>"))

            async def restorable(self, *, dependencies: Dependencies | None = None) -> bool:
                _ = dependencies
                return False


class _PersistTrackingSession(BaseSandboxSession):
    def __init__(self, snapshot: SnapshotBase, *, workspace_root: Path) -> None:
        self.state = TestSessionState(
            manifest=Manifest(root=str(workspace_root)),
            snapshot=snapshot,
        )
        self.persist_workspace_calls = 0
        self.persist_payload = b"tracked"

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = timeout
        process = await asyncio.create_subprocess_exec(
            *(str(part) for part in command),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return ExecResult(
            stdout=stdout or b"",
            stderr=stderr or b"",
            exit_code=process.returncode or 0,
        )

    async def read(self, path: Path, *, user: object = None) -> io.IOBase:
        _ = (path, user)
        raise AssertionError("read() should not be called in this test")

    async def write(self, path: Path, data: io.IOBase, *, user: object = None) -> None:
        _ = (path, data, user)
        raise AssertionError("write() should not be called in this test")

    async def running(self) -> bool:
        return True

    async def persist_workspace(self) -> io.IOBase:
        self.persist_workspace_calls += 1
        return io.BytesIO(self.persist_payload)

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        _ = data

    async def shutdown(self) -> None:
        return


class _ResumeTrackingSession(BaseSandboxSession):
    def __init__(
        self,
        *,
        snapshot: SnapshotBase | None = None,
        running: bool = True,
        workspace_root: Path,
        workspace_state_preserved: bool = True,
        system_state_preserved: bool = False,
        workspace_root_ready: bool | None = None,
    ) -> None:
        self.state = TestSessionState(
            manifest=Manifest(root=str(workspace_root)),
            snapshot=snapshot or TestRestorableSnapshot(id="resume-snapshot"),
        )
        self.state.workspace_root_ready = (
            workspace_state_preserved if workspace_root_ready is None else workspace_root_ready
        )
        self._running = running
        self._set_start_state_preserved(
            workspace_state_preserved,
            system=system_state_preserved,
        )
        self.clear_calls = 0
        self.hydrate_payloads: list[bytes] = []
        self.apply_manifest_calls: list[bool] = []
        self.apply_manifest_provision_accounts_calls: list[bool] = []
        self.provision_manifest_accounts_calls = 0

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = timeout
        process = await asyncio.create_subprocess_exec(
            *(str(part) for part in command),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return ExecResult(
            stdout=stdout or b"",
            stderr=stderr or b"",
            exit_code=process.returncode or 0,
        )

    async def read(self, path: Path, *, user: object = None) -> io.IOBase:
        _ = (path, user)
        raise AssertionError("read() should not be called in this test")

    async def write(self, path: Path, data: io.IOBase, *, user: object = None) -> None:
        _ = (path, data, user)
        raise AssertionError("write() should not be called in this test")

    async def running(self) -> bool:
        return self._running

    async def persist_workspace(self) -> io.IOBase:
        return io.BytesIO(b"persisted-workspace")

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        payload = data.read()
        assert isinstance(payload, bytes)
        self.hydrate_payloads.append(payload)

    async def shutdown(self) -> None:
        return

    async def _apply_manifest(
        self,
        *,
        only_ephemeral: bool = False,
        provision_accounts: bool = True,
    ) -> MaterializationResult:
        self.apply_manifest_calls.append(only_ephemeral)
        self.apply_manifest_provision_accounts_calls.append(provision_accounts)
        return MaterializationResult(files=[])

    async def apply_manifest(self, *, only_ephemeral: bool = False) -> MaterializationResult:
        return await self._apply_manifest(
            only_ephemeral=only_ephemeral,
            provision_accounts=not only_ephemeral,
        )

    async def provision_manifest_accounts(self) -> None:
        self.provision_manifest_accounts_calls += 1

    async def _clear_workspace_root_on_resume(self) -> None:
        self.clear_calls += 1


class _ClosingPersistTrackingSession(_PersistTrackingSession):
    def __init__(self, snapshot: SnapshotBase, *, workspace_root: Path) -> None:
        super().__init__(snapshot, workspace_root=workspace_root)
        self.archive = _TrackingBytesIO(self.persist_payload)

    async def persist_workspace(self) -> io.IOBase:
        self.persist_workspace_calls += 1
        return self.archive


@pytest.mark.asyncio
async def test_noop_snapshot_stop_skips_workspace_persist(tmp_path: Path) -> None:
    session = _PersistTrackingSession(NoopSnapshot(id="noop"), workspace_root=tmp_path)

    await session.stop()

    assert session.persist_workspace_calls == 0


@pytest.mark.asyncio
async def test_non_noop_snapshot_stop_persists_workspace(tmp_path: Path) -> None:
    snapshot = TestNoopSnapshot(id="custom-snapshot")
    session = _PersistTrackingSession(snapshot, workspace_root=tmp_path)

    await session.stop()

    assert session.persist_workspace_calls == 1


@pytest.mark.asyncio
async def test_stop_closes_persisted_workspace_archive(tmp_path: Path) -> None:
    snapshot = TestNoopSnapshot(id="custom-snapshot")
    session = _ClosingPersistTrackingSession(snapshot, workspace_root=tmp_path)

    await session.stop()

    assert session.archive.close_calls == 1
    assert session.archive.closed


@pytest.mark.asyncio
async def test_non_noop_snapshot_stop_records_snapshot_fingerprint(tmp_path: Path) -> None:
    (tmp_path / "tracked.txt").write_bytes(b"tracked")
    snapshot = TestNoopSnapshot(id="custom-snapshot")
    session = _PersistTrackingSession(snapshot, workspace_root=tmp_path)

    await session.stop()

    assert session.state.snapshot_fingerprint is not None
    assert session.state.snapshot_fingerprint_version == "workspace_tar_sha256_v1"
    cache_payload = session._parse_snapshot_fingerprint_record(
        session._snapshot_fingerprint_cache_path().read_text()
    )
    assert cache_payload["fingerprint"] == session.state.snapshot_fingerprint
    assert cache_payload["version"] == session.state.snapshot_fingerprint_version


@pytest.mark.asyncio
async def test_start_skips_snapshot_restore_when_live_workspace_fingerprint_matches(
    tmp_path: Path,
) -> None:
    session = _ResumeTrackingSession(workspace_root=tmp_path)
    (tmp_path / "tracked.txt").write_bytes(b"tracked")

    await session.stop()

    await session.start()

    assert session.clear_calls == 0
    assert session.hydrate_payloads == []
    assert session.provision_manifest_accounts_calls == 0
    assert session.apply_manifest_calls == [True]


@pytest.mark.asyncio
async def test_start_closes_restored_workspace_archive(tmp_path: Path) -> None:
    snapshot = TestClosingRestoreSnapshot(id="resume-snapshot")
    session = _ResumeTrackingSession(snapshot=snapshot, running=False, workspace_root=tmp_path)

    await session.start()

    assert snapshot._stream.close_calls == 1
    assert snapshot._stream.closed


@pytest.mark.asyncio
async def test_start_restores_snapshot_when_live_workspace_fingerprint_mismatches(
    tmp_path: Path,
) -> None:
    session = _ResumeTrackingSession(workspace_root=tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_bytes(b"tracked")

    await session.stop()
    tracked.write_bytes(b"drifted")

    await session.start()

    assert session.clear_calls == 1
    assert session.hydrate_payloads == [b"restored-workspace"]
    assert session.provision_manifest_accounts_calls == 1
    assert session.apply_manifest_calls == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest_mutation", ["ephemeral_entry", "user"])
async def test_start_restores_snapshot_when_resume_manifest_changes(
    tmp_path: Path,
    manifest_mutation: str,
) -> None:
    session = _ResumeTrackingSession(workspace_root=tmp_path)
    (tmp_path / "tracked.txt").write_bytes(b"tracked")

    await session.stop()

    if manifest_mutation == "ephemeral_entry":
        session.state.manifest.entries["ephemeral.txt"] = File(content=b"temp", ephemeral=True)
    else:
        session.state.manifest.users.append(User(name="sandbox-user"))

    await session.start()

    assert session.clear_calls == 1
    assert session.hydrate_payloads == [b"restored-workspace"]
    assert session.provision_manifest_accounts_calls == 1
    assert session.apply_manifest_calls == [True]


@pytest.mark.asyncio
async def test_start_applies_full_manifest_for_fresh_non_restorable_backend(
    tmp_path: Path,
) -> None:
    session = _ResumeTrackingSession(
        snapshot=NoopSnapshot(id="fresh"),
        workspace_root=tmp_path,
        workspace_state_preserved=False,
    )

    await session.start()

    assert session.clear_calls == 0
    assert session.hydrate_payloads == []
    assert session.provision_manifest_accounts_calls == 0
    assert session.apply_manifest_calls == [False]
    assert session.apply_manifest_provision_accounts_calls == [True]


@pytest.mark.asyncio
async def test_start_reapplies_only_ephemeral_manifest_for_preserved_non_restorable_backend(
    tmp_path: Path,
) -> None:
    session = _ResumeTrackingSession(
        snapshot=NoopSnapshot(id="preserved"),
        workspace_root=tmp_path,
        workspace_state_preserved=True,
    )

    await session.start()

    assert session.clear_calls == 0
    assert session.hydrate_payloads == []
    assert session.provision_manifest_accounts_calls == 0
    assert session.apply_manifest_calls == [True]
    assert session.apply_manifest_provision_accounts_calls == [False]


@pytest.mark.asyncio
async def test_start_reapplies_only_ephemeral_manifest_when_preserved_probe_succeeds(
    tmp_path: Path,
) -> None:
    session = _ResumeTrackingSession(
        snapshot=NoopSnapshot(id="preserved-probed"),
        workspace_root=tmp_path,
        workspace_state_preserved=True,
        workspace_root_ready=False,
    )

    await session.start()

    assert session.clear_calls == 0
    assert session.hydrate_payloads == []
    assert session.provision_manifest_accounts_calls == 0
    assert session.apply_manifest_calls == [True]
    assert session.apply_manifest_provision_accounts_calls == [False]


@pytest.mark.asyncio
async def test_start_applies_full_manifest_when_preserved_non_restorable_workspace_unproven(
    tmp_path: Path,
) -> None:
    session = _ResumeTrackingSession(
        snapshot=NoopSnapshot(id="unproven"),
        workspace_root=tmp_path / "missing-workspace",
        workspace_state_preserved=True,
        workspace_root_ready=False,
    )

    await session.start()

    assert session.clear_calls == 0
    assert session.hydrate_payloads == []
    assert session.provision_manifest_accounts_calls == 0
    assert session.apply_manifest_calls == [False]
    assert session.apply_manifest_provision_accounts_calls == [True]


@pytest.mark.asyncio
async def test_start_applies_full_manifest_without_accounts_when_system_state_preserved(
    tmp_path: Path,
) -> None:
    session = _ResumeTrackingSession(
        snapshot=NoopSnapshot(id="system-preserved"),
        workspace_root=tmp_path / "missing-workspace",
        workspace_state_preserved=True,
        system_state_preserved=True,
        workspace_root_ready=False,
    )

    await session.start()

    assert session.clear_calls == 0
    assert session.hydrate_payloads == []
    assert session.provision_manifest_accounts_calls == 0
    assert session.apply_manifest_calls == [False]
    assert session.apply_manifest_provision_accounts_calls == [False]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "snapshot_id",
    [
        "../escape",
        "..\\escape",
        "nested/escape",
        "../",
        "..//",
        "..\\",
        "nested/",
        "nested//",
        "nested\\",
    ],
)
async def test_local_snapshot_rejects_non_basename_ids(
    tmp_path: Path,
    snapshot_id: str,
) -> None:
    snapshot = LocalSnapshot(id=snapshot_id, base_path=tmp_path / "snapshots")

    with pytest.raises(ValueError, match="single path segment"):
        await snapshot.persist(io.BytesIO(b"payload"))

    with pytest.raises(ValueError, match="single path segment"):
        await snapshot.restore()

    assert list(tmp_path.rglob("*.tar")) == []


@pytest.mark.asyncio
async def test_local_snapshot_persist_is_atomic_on_copy_failure(tmp_path: Path) -> None:
    class _FailingSnapshotSource(io.BytesIO):
        def __init__(self) -> None:
            super().__init__(b"new-snapshot")
            self._reads = 0

        def read(self, size: int | None = -1) -> bytes:
            self._reads += 1
            if self._reads == 1:
                return b"new"
            raise OSError("copy failed")

    snapshot = LocalSnapshot(id="atomic", base_path=tmp_path)
    path = tmp_path / "atomic.tar"
    path.write_bytes(b"previous-snapshot")

    with pytest.raises(SnapshotPersistError):
        await snapshot.persist(_FailingSnapshotSource())

    assert path.read_bytes() == b"previous-snapshot"
    assert {p.name for p in tmp_path.iterdir()} == {"atomic.tar"}


class _FakeRemoteSnapshotClient:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, bytes]] = []
        self.downloads: list[str] = []
        self.exists_calls: list[str] = []
        self._stored: dict[str, bytes] = {}

    async def upload(self, snapshot_id: str, data: io.IOBase) -> None:
        payload = data.read()
        assert isinstance(payload, bytes)
        self.uploads.append((snapshot_id, payload))
        self._stored[snapshot_id] = payload

    async def download(self, snapshot_id: str) -> io.IOBase:
        self.downloads.append(snapshot_id)
        return io.BytesIO(self._stored[snapshot_id])

    async def exists(self, snapshot_id: str) -> bool:
        self.exists_calls.append(snapshot_id)
        return snapshot_id in self._stored


class _UploadDownloadOnlyRemoteSnapshotClient:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, bytes]] = []

    async def upload(self, snapshot_id: str, data: io.IOBase) -> None:
        payload = data.read()
        assert isinstance(payload, bytes)
        self.uploads.append((snapshot_id, payload))

    async def download(self, snapshot_id: str) -> io.IOBase:
        return io.BytesIO(b"downloaded")


@pytest.mark.asyncio
async def test_remote_snapshot_persist_restore_and_restorable_use_injected_dependency() -> None:
    client = _FakeRemoteSnapshotClient()
    dependencies = Dependencies().bind_value("tests.remote_snapshot_client", client)
    snapshot = RemoteSnapshot(id="snap-123", client_dependency_key="tests.remote_snapshot_client")

    assert await snapshot.restorable(dependencies=dependencies) is False

    await snapshot.persist(io.BytesIO(b"workspace-tar"), dependencies=dependencies)

    assert client.uploads == [("snap-123", b"workspace-tar")]
    assert await snapshot.restorable(dependencies=dependencies) is True
    assert client.exists_calls == ["snap-123", "snap-123"]

    restored = await snapshot.restore(dependencies=dependencies)

    assert client.downloads == ["snap-123"]
    assert restored.read() == b"workspace-tar"


def test_remote_snapshot_spec_builds_remote_snapshot() -> None:
    snapshot = resolve_snapshot(
        RemoteSnapshotSpec(client_dependency_key="tests.remote_snapshot_client"),
        "snap-123",
    )

    assert isinstance(snapshot, RemoteSnapshot)
    assert snapshot.id == "snap-123"
    assert snapshot.client_dependency_key == "tests.remote_snapshot_client"


def test_remote_snapshot_serializes_through_session_state_without_dependencies() -> None:
    state = TestSessionState(
        manifest=Manifest(root="/workspace"),
        snapshot=RemoteSnapshot(
            id="snap-123", client_dependency_key="tests.remote_snapshot_client"
        ),
    )

    payload = state.model_dump(mode="json")

    assert payload["snapshot"] == {
        "type": "remote",
        "id": "snap-123",
        "client_dependency_key": "tests.remote_snapshot_client",
    }

    restored = SandboxSessionState.model_validate(payload)

    assert isinstance(restored.snapshot, RemoteSnapshot)
    assert restored.snapshot.id == "snap-123"
    assert restored.snapshot.client_dependency_key == "tests.remote_snapshot_client"
    assert not hasattr(restored.snapshot, "persisted")


@pytest.mark.asyncio
async def test_remote_snapshot_without_exists_requires_check_method() -> None:
    client = _UploadDownloadOnlyRemoteSnapshotClient()
    dependencies = Dependencies().bind_value("tests.remote_snapshot_client", client)
    snapshot = RemoteSnapshot(id="snap-123", client_dependency_key="tests.remote_snapshot_client")
    expected_error = "Remote snapshot client must implement `exists(snapshot_id, ...)`"

    with pytest.raises(TypeError) as exc_info:
        await snapshot.restorable(dependencies=dependencies)

    assert str(exc_info.value) == expected_error

    await snapshot.persist(io.BytesIO(b"workspace-tar"), dependencies=dependencies)

    assert client.uploads == [("snap-123", b"workspace-tar")]

    with pytest.raises(TypeError) as exc_info:
        await snapshot.restorable(dependencies=dependencies)

    assert str(exc_info.value) == expected_error


@pytest.mark.asyncio
async def test_session_set_dependencies_passes_remote_snapshot_client() -> None:
    client = _FakeRemoteSnapshotClient()
    session = _PersistTrackingSession(
        RemoteSnapshot(id="snap-123", client_dependency_key="tests.remote_snapshot_client"),
        workspace_root=Path("/tmp/test-session-deps"),
    )

    session.set_dependencies(Dependencies().bind_value("tests.remote_snapshot_client", client))

    await session.stop()

    assert client.uploads == [("snap-123", b"tracked")]


@pytest.mark.asyncio
async def test_sandbox_session_set_dependencies_delegates_to_inner_session() -> None:
    client = _FakeRemoteSnapshotClient()
    inner = _PersistTrackingSession(
        RemoteSnapshot(id="snap-123", client_dependency_key="tests.remote_snapshot_client"),
        workspace_root=Path("/tmp/test-session-wrapper-deps"),
    )
    session = SandboxSession(inner)

    session.set_dependencies(Dependencies().bind_value("tests.remote_snapshot_client", client))

    await session.stop()

    assert client.uploads == [("snap-123", b"tracked")]
