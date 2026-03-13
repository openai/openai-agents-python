from __future__ import annotations

import io
from pathlib import Path
from typing import Literal

import pytest

from agents.sandbox.manifest import Manifest
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.sandbox.session.sandbox_session_state import SandboxSessionState
from agents.sandbox.snapshot import NoopSnapshot, SnapshotBase
from agents.sandbox.types import ExecResult


class TestNoopSnapshot(SnapshotBase):
    __test__ = False
    type: Literal["test-noop"] = "test-noop"

    async def persist(self, data: io.IOBase) -> None:
        _ = data

    async def restore(self) -> io.IOBase:
        raise FileNotFoundError(Path("<test-noop>"))

    async def restorable(self) -> bool:
        return False


def test_sandbox_session_state_roundtrip_preserves_custom_snapshot_type() -> None:
    state = SandboxSessionState(
        manifest=Manifest(),
        snapshot=TestNoopSnapshot(id="custom-snapshot"),
    )

    payload = state.model_dump_json()
    restored = SandboxSessionState.model_validate_json(payload)

    assert isinstance(restored.snapshot, TestNoopSnapshot)
    assert restored.snapshot.id == "custom-snapshot"


def test_snapshot_parse_uses_registered_custom_snapshot_type() -> None:
    parsed = SnapshotBase.parse({"type": "test-noop", "id": "registered"})

    assert isinstance(parsed, TestNoopSnapshot)
    assert parsed.id == "registered"


def test_duplicate_snapshot_type_registration_raises() -> None:
    class TestDuplicateSnapshotA(SnapshotBase):
        __test__ = False
        type: Literal["test-duplicate"] = "test-duplicate"

        async def persist(self, data: io.IOBase) -> None:
            _ = data

        async def restore(self) -> io.IOBase:
            raise FileNotFoundError(Path("<test-duplicate-a>"))

        async def restorable(self) -> bool:
            return False

    _ = TestDuplicateSnapshotA

    with pytest.raises(TypeError, match="already registered"):

        class TestDuplicateSnapshotB(SnapshotBase):
            __test__ = False
            type: Literal["test-duplicate"] = "test-duplicate"

            async def persist(self, data: io.IOBase) -> None:
                _ = data

            async def restore(self) -> io.IOBase:
                raise FileNotFoundError(Path("<test-duplicate-b>"))

            async def restorable(self) -> bool:
                return False


def test_snapshot_subclasses_require_type_discriminator_default() -> None:
    with pytest.raises(TypeError, match="must define a non-empty string default for `type`"):

        class TestMissingTypeSnapshot(SnapshotBase):
            __test__ = False

            async def persist(self, data: io.IOBase) -> None:
                _ = data

            async def restore(self) -> io.IOBase:
                raise FileNotFoundError(Path("<test-missing-type>"))

            async def restorable(self) -> bool:
                return False


class _PersistTrackingSession(BaseSandboxSession):
    def __init__(self, snapshot: SnapshotBase) -> None:
        self.state = SandboxSessionState(
            manifest=Manifest(),
            snapshot=snapshot,
        )
        self.persist_workspace_calls = 0
        self.persist_payload = b"tracked"

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        _ = (command, timeout)
        raise AssertionError("_exec_internal() should not be called in this test")

    async def read(self, path: Path) -> io.IOBase:
        _ = path
        raise AssertionError("read() should not be called in this test")

    async def write(self, path: Path, data: io.IOBase) -> None:
        _ = (path, data)
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


@pytest.mark.asyncio
async def test_noop_snapshot_stop_skips_workspace_persist() -> None:
    session = _PersistTrackingSession(NoopSnapshot(id="noop"))

    await session.stop()

    assert session.persist_workspace_calls == 0


@pytest.mark.asyncio
async def test_non_noop_snapshot_stop_persists_workspace() -> None:
    snapshot = TestNoopSnapshot(id="custom-snapshot")
    session = _PersistTrackingSession(snapshot)

    await session.stop()

    assert session.persist_workspace_calls == 1
