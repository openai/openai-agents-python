"""Saved workspace paths cannot grant host deletion or snapshot restore authority."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agents.classic.sandbox.manifest import Manifest
from agents.classic.sandbox.runtime_session_manager import SandboxRuntimeSessionManager
from agents.classic.sandbox.sandbox_agent import SandboxAgent
from agents.classic.sandbox.session import SandboxSessionState
from agents.classic.sandbox.snapshot import LocalSnapshot

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="UnixLocal requires Unix")

if sys.platform != "win32":
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxSession,
        UnixLocalSandboxSessionState,
    )


@pytest.mark.parametrize("loader", ["client", "model", "json", "constructor", "parse"])
async def test_restored_root_requires_current_authorization(tmp_path: Path, loader: str) -> None:
    root = tmp_path / "victim"
    root.mkdir()
    target = root / "synthetic.txt"
    target.write_text("synthetic host data")
    client = UnixLocalSandboxClient()
    fresh = await client.create(manifest=Manifest(root=str(root)))
    payload = client.serialize_session_state(fresh.state)
    payload["workspace_root_owned"] = True
    if loader == "client":
        restored = client.deserialize_session_state(payload)
    elif loader == "model":
        restored = UnixLocalSandboxSessionState.model_validate(payload)
    elif loader == "json":
        restored = UnixLocalSandboxSessionState.model_validate_json(json.dumps(payload))
    elif loader == "constructor":
        restored = UnixLocalSandboxSessionState(**payload)
    else:
        restored = SandboxSessionState.parse(payload)

    with pytest.raises(ValueError, match="current trusted manifest"):
        await client.resume(restored)
    with pytest.raises(ValueError, match="current trusted manifest"):
        UnixLocalSandboxSession.from_state(restored)
    assert target.read_text() == "synthetic host data"


async def test_root_authorization_does_not_transfer_deletion_ownership(tmp_path: Path) -> None:
    client = UnixLocalSandboxClient()
    fresh = await client.create()
    root = Path(fresh.state.manifest.root)
    target = root / "synthetic.txt"
    target.write_text("old workspace")
    payload = client.serialize_session_state(fresh.state)
    other_client = UnixLocalSandboxClient()
    restored = other_client.deserialize_session_state(payload)
    try:
        with pytest.raises(ValueError, match="same root"):
            await other_client.resume(restored, trusted_manifest=Manifest(root=str(tmp_path)))
        resumed = await other_client.resume(restored, trusted_manifest=Manifest(root=str(root)))
        with await resumed.read(target) as stream:
            assert stream.read() == b"old workspace"
        await other_client.delete(resumed)
        assert target.read_text() == "old workspace"
    finally:
        await client.delete(fresh)
    assert not root.exists()


async def test_typed_manifest_cannot_claim_sdk_deletion_ownership(tmp_path: Path) -> None:
    root = tmp_path / "application"
    root.mkdir()
    target = root / "synthetic.txt"
    target.write_text("application data")
    state = UnixLocalSandboxSessionState(
        manifest=Manifest(root=str(root)),
        snapshot=LocalSnapshot(id="fixture", base_path=tmp_path / "snapshots"),
        workspace_root_owned=True,
    )
    client = UnixLocalSandboxClient()
    session = await client.resume(state)
    await client.delete(session)
    assert target.read_text() == "application data"


async def test_runner_root_binding_preserves_snapshot_recovery(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "synthetic.txt"
    target.write_text("snapshot data")
    manifest = Manifest(root=str(root))
    snapshot = LocalSnapshot(id="fixture", base_path=tmp_path / "snapshots")
    client = UnixLocalSandboxClient()
    fresh = await client.create(manifest=manifest, snapshot=snapshot)
    archive = await fresh.persist_workspace()
    try:
        await snapshot.persist(archive)
    finally:
        archive.close()
    payload = client.serialize_session_state(fresh.state)
    payload["workspace_root_owned"] = True
    restored = client.deserialize_session_state(payload)
    target.write_text("changed live data")
    unexpected = root / "unexpected.txt"
    unexpected.write_text("stale data")
    agent = SandboxAgent(name="fixture", capabilities=())
    with pytest.raises(ValueError, match="current trusted manifest"):
        SandboxRuntimeSessionManager._process_resumed_state_manifest(
            agent=agent,
            capabilities=[],
            session_state=restored,
            trusted_manifest=None,
            provider_backend_id="unix_local",
        )
    assert target.read_text() == "changed live data"
    processed = SandboxRuntimeSessionManager._process_resumed_state_manifest(
        agent=agent,
        capabilities=[],
        session_state=restored,
        trusted_manifest=manifest,
        provider_backend_id="unix_local",
    )
    resumed = await client.resume(processed)
    await resumed._inner._restore_snapshot_into_workspace_on_resume()
    assert target.read_text() == "snapshot data"
    assert not unexpected.exists()
    await client.delete(resumed)
    assert target.read_text() == "snapshot data"


async def test_state_replacement_cannot_clear_or_delete_host_root(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "synthetic.txt"
    target.write_text("preserved")
    client = UnixLocalSandboxClient()
    fresh = await client.create(manifest=Manifest(root=str(root)))
    payload = client.serialize_session_state(fresh.state)
    payload["workspace_root_owned"] = True
    fresh.state = client.deserialize_session_state(payload)
    with pytest.raises(ValueError, match="current trusted manifest"):
        await client.delete(fresh)
    with pytest.raises(ValueError, match="current trusted manifest"):
        await fresh._inner._clear_workspace_root_on_resume()
    with pytest.raises(ValueError, match="current trusted manifest"):
        await fresh.hydrate_workspace(io.BytesIO())
    assert target.read_text() == "preserved"


async def test_cross_process_recovery_requires_fresh_root_authority(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "synthetic.txt"
    target.write_text("preserved across processes")
    client = UnixLocalSandboxClient()
    fresh = await client.create(manifest=Manifest(root=str(root)))
    payload = client.serialize_session_state(fresh.state)
    payload["workspace_root_owned"] = True
    script = """
import asyncio, json, sys
from agents.classic.sandbox.manifest import Manifest
from agents.classic.sandbox.sandboxes.unix_local import UnixLocalSandboxClient

async def main():
    client = UnixLocalSandboxClient()
    state = client.deserialize_session_state(json.loads(sys.argv[1]))
    try:
        await client.resume(state)
    except ValueError:
        pass
    else:
        raise AssertionError("Checkpoint root was accepted without current authorization")
    resumed = await client.resume(state, trusted_manifest=Manifest(root=sys.argv[2]))
    with await resumed.read("synthetic.txt") as stream:
        assert stream.read() == b"preserved across processes"
    await client.delete(resumed)

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, json.dumps(payload), str(root)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert target.read_text() == "preserved across processes"


async def test_default_manifest_does_not_authorize_checkpoint_root(tmp_path: Path) -> None:
    client = UnixLocalSandboxClient()
    fresh = await client.create(manifest=Manifest(root=str(tmp_path)))
    payload = client.serialize_session_state(fresh.state)
    payload["manifest"]["root"] = Manifest().root
    restored = client.deserialize_session_state(payload)
    with pytest.raises(ValueError, match="current trusted manifest"):
        await client.resume(restored, trusted_manifest=Manifest())


async def test_manifest_copy_cannot_transfer_workspace_authority(tmp_path: Path) -> None:
    client = UnixLocalSandboxClient()
    fresh = await client.create()
    allocated_state = fresh.state
    allocated_root = Path(allocated_state.manifest.root)
    target = tmp_path / "synthetic.txt"
    target.write_text("application data")
    fresh.state = allocated_state.model_copy(update={"manifest": Manifest(root=str(tmp_path))})
    try:
        with pytest.raises(ValueError, match="current trusted manifest"):
            await client.delete(fresh)
        with pytest.raises(ValueError, match="current trusted manifest"):
            await fresh._inner._clear_workspace_root_on_resume()
        resumed = await client.resume(fresh.state, trusted_manifest=Manifest(root=str(tmp_path)))
        await client.delete(resumed)
        assert target.read_text() == "application data"
        assert allocated_root.is_dir()
    finally:
        fresh.state = allocated_state
        await client.delete(fresh)
    assert not allocated_root.exists()
