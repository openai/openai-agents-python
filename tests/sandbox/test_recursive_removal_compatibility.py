"""Snapshot pruning keeps the default backends' released removal behavior."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agents.sandbox import Manifest, SandboxPathGrant
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession

from . import _docker_removal_helpers as removal_helpers

service = removal_helpers.service


@pytest.mark.asyncio
async def test_default_remote_start_restores_with_unrelated_read_only_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("agents.sandbox.sandboxes.unix_local", exc_type=ImportError)
    from .test_snapshot import _ResumeTrackingSession

    # This provider double executes real file commands; only hydration is recorded.
    # Keeping the shared rm and pruning paths is essential to this compatibility check.
    workspace = tmp_path / "workspace"
    stale = workspace / "build" / "stale.txt"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old workspace")
    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    protected = toolchain / "config"
    protected.write_bytes(b"protected")
    session = _ResumeTrackingSession(workspace_root=workspace, running=False)
    session.state.manifest = Manifest(
        root=str(workspace),
        extra_path_grants=(SandboxPathGrant(path=str(toolchain), read_only=True),),
    )
    monkeypatch.setattr(
        session,
        "_clear_workspace_root_on_resume",
        BaseSandboxSession._clear_workspace_root_on_resume.__get__(session),
    )
    monkeypatch.setattr(session, "_ensure_runtime_helpers", AsyncMock())

    await session.start()

    assert not stale.parent.exists()
    assert session.hydrate_payloads == [b"restored-workspace"]
    assert session.apply_manifest_calls == [True]
    assert protected.read_bytes() == b"protected"


@pytest.mark.asyncio
async def test_unix_local_start_restores_with_unrelated_read_only_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = pytest.importorskip("agents.sandbox.sandboxes.unix_local", exc_type=ImportError)
    from .test_snapshot import TestRestorableSnapshot

    workspace = tmp_path / "workspace"
    stale = workspace / "build" / "stale.txt"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old workspace")
    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    protected = toolchain / "config"
    protected.write_bytes(b"protected")
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        restored = tarfile.TarInfo("restored.txt")
        restored.size = len(b"saved workspace")
        archive.addfile(restored, io.BytesIO(b"saved workspace"))
    session = module.UnixLocalSandboxSession(
        state=module.UnixLocalSandboxSessionState(
            manifest=Manifest(
                root=str(workspace),
                extra_path_grants=(SandboxPathGrant(path=str(toolchain), read_only=True),),
            ),
            snapshot=TestRestorableSnapshot(id="removal-compatibility", payload=payload.getvalue()),
        )
    )
    # Avoid native shell setup while retaining actual startup, pruning, and tar hydration.
    monkeypatch.setattr(session, "_ensure_runtime_helpers", AsyncMock())
    monkeypatch.setattr(session, "provision_manifest_accounts", AsyncMock())
    monkeypatch.setattr(session, "_reapply_ephemeral_manifest_on_resume", AsyncMock())

    await session.start()

    assert not stale.parent.exists()
    assert (workspace / "restored.txt").read_bytes() == b"saved workspace"
    assert protected.read_bytes() == b"protected"
    assert await session.running()


@pytest.mark.asyncio
async def test_live_docker_authority_preserves_resume_and_cleanup(service: Any) -> None:
    from agents.sandbox.sandboxes.docker import DockerSandboxClient

    manager, container, worker = service
    configured = removal_helpers.manifest()
    manager.bind_new(container, configured)
    manager.docker_client.containers.get.return_value = container
    current = removal_helpers.session(manager, container, configured)
    client = DockerSandboxClient(manager.docker_client, removal_service=manager)

    resumed = await client.resume(current.state)
    await resumed.rm("build", recursive=True)

    assert worker.removed == ["/workspace/build"]
    assert resumed.state.manifest == configured
