"""Grant configuration remains usable independently of recursive removal support."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agents.sandbox import Manifest, SandboxPathGrant
from agents.sandbox.errors import WorkspaceArchiveWriteError
from agents.sandbox.snapshot import NoopSnapshot

from . import _docker_removal_helpers as removal_helpers

service = removal_helpers.service


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "resume"])
async def test_local_read_only_grants_allow_start_and_io_but_reject_recursive_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    module = pytest.importorskip("agents.sandbox.sandboxes.unix_local", exc_type=ImportError)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    toolchain = tmp_path / "toolchain"
    toolchain.mkdir()
    protected = toolchain / "config"
    protected.write_bytes(b"protected")
    configured = Manifest(
        root=str(workspace),
        extra_path_grants=(SandboxPathGrant(path=str(toolchain), read_only=True),),
    )
    client = module.UnixLocalSandboxClient()
    if operation == "create":
        current = await client.create(manifest=configured, snapshot=NoopSnapshot(id="grants"))
    else:
        current = await client.resume(
            module.UnixLocalSandboxSessionState(
                manifest=configured, snapshot=NoopSnapshot(id="grants")
            )
        )
    # Exercise start validation without launching shell commands or native sandboxes.
    prepare = AsyncMock()
    start_workspace = AsyncMock()
    monkeypatch.setattr(current._inner, "_prepare_backend_workspace", prepare)
    monkeypatch.setattr(current._inner, "_start_workspace", start_workspace)
    await current.start()
    prepare.assert_awaited_once()
    start_workspace.assert_awaited_once()
    assert current.state.manifest == configured
    stream = await current.read(protected)
    with stream:
        assert stream.read() == b"protected"
    await current.write(Path("scratch"), io.BytesIO(b"writable"))
    assert (workspace / "scratch").read_bytes() == b"writable"
    with pytest.raises(WorkspaceArchiveWriteError):
        await current.write(protected, io.BytesIO(b"forbidden"))
    with pytest.raises(WorkspaceArchiveWriteError) as caught:
        await current.rm("scratch", recursive=True)
    assert caught.value.context["reason"] == "recursive_remove_with_read_only_grants"
    assert (workspace / "scratch").read_bytes() == b"writable"
    assert protected.read_bytes() == b"protected"


@pytest.mark.asyncio
async def test_live_docker_authority_accepts_read_only_resume_and_cleanup(service: Any) -> None:
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
