"""Persisted UnixLocal path grants require current application authority."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from agents.classic.sandbox.errors import WorkspaceArchiveWriteError
from agents.classic.sandbox.manifest import Manifest
from agents.classic.sandbox.runtime_session_manager import SandboxRuntimeSessionManager
from agents.classic.sandbox.sandbox_agent import SandboxAgent
from agents.classic.sandbox.workspace_paths import SandboxPathGrant

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="UnixLocal requires Unix")

if sys.platform != "win32":
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxClient,
        UnixLocalSandboxSessionState,
    )


@pytest.mark.parametrize("loader", ["client", "model", "json", "constructor"])
async def test_saved_unix_local_grant_cannot_authorize_host_file(
    tmp_path: Path, loader: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "synthetic.txt").write_text("synthetic host data")
    client = UnixLocalSandboxClient()
    fresh = await client.create(manifest=Manifest(root=str(workspace)))
    payload = client.serialize_session_state(fresh.state)
    payload["manifest"]["extra_path_grants"] = [{"path": str(outside)}]

    if loader == "client":
        restored = client.deserialize_session_state(payload)
    elif loader == "model":
        restored = UnixLocalSandboxSessionState.model_validate(payload)
    elif loader == "json":
        restored = UnixLocalSandboxSessionState.model_validate_json(json.dumps(payload))
    else:
        restored = UnixLocalSandboxSessionState(**payload)

    with pytest.raises(ValueError, match="trusted manifest|rebound"):
        await client.resume(restored)


async def test_saved_unix_local_grant_uses_current_trusted_manifest(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "synthetic.txt"
    target.write_text("synthetic host data")
    grant = SandboxPathGrant(path=str(outside), read_only=True)
    manifest = Manifest(root=str(workspace), extra_path_grants=(grant,))
    client = UnixLocalSandboxClient()
    fresh = await client.create(manifest=manifest)
    saved = client.serialize_session_state(fresh.state)
    saved["manifest"]["extra_path_grants"][0]["read_only"] = False

    restored = client.deserialize_session_state(saved.copy())
    agent = SandboxAgent(name="fixture", default_manifest=manifest, capabilities=())
    with pytest.raises(ValueError, match="trusted manifest|path grants"):
        SandboxRuntimeSessionManager._process_resumed_state_manifest(
            agent=agent,
            capabilities=[],
            session_state=restored,
            trusted_manifest=None,
            provider_backend_id="unix_local",
        )

    restored = client.deserialize_session_state(saved)
    processed = SandboxRuntimeSessionManager._process_resumed_state_manifest(
        agent=agent,
        capabilities=[],
        session_state=restored,
        trusted_manifest=manifest,
        provider_backend_id="unix_local",
    )
    resumed = await client.resume(processed)
    with await resumed.read(target) as stream:
        assert stream.read() == b"synthetic host data"
    with pytest.raises(WorkspaceArchiveWriteError):
        await resumed.write(target, io.BytesIO(b"changed"))
    assert target.read_text() == "synthetic host data"

    changed = Manifest(root=str(workspace))
    restored = client.deserialize_session_state(client.serialize_session_state(fresh.state))
    with pytest.raises(ValueError, match="path grants"):
        SandboxRuntimeSessionManager._process_resumed_state_manifest(
            agent=agent,
            capabilities=[],
            session_state=restored,
            trusted_manifest=changed,
            provider_backend_id="unix_local",
        )
