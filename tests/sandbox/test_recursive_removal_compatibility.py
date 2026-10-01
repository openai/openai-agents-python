"""Snapshot pruning keeps the default backends' released removal behavior."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agents.sandbox import Manifest, SandboxPathGrant
from agents.sandbox.errors import ExecNonZeroError, WorkspaceArchiveWriteError
from agents.sandbox.session import SandboxSession
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession

from . import _docker_removal_helpers as removal_helpers

service = removal_helpers.service


def _removal_session(
    backend: str, manifest: Manifest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> SandboxSession:
    module = pytest.importorskip("agents.sandbox.sandboxes.unix_local", exc_type=ImportError)
    from agents.sandbox.snapshot import NoopSnapshot

    from .test_runtime_helpers import _install_resolve_helper
    from .test_snapshot import _ResumeTrackingSession

    if backend == "local":
        session = module.UnixLocalSandboxSession(
            state=module.UnixLocalSandboxSessionState(
                manifest=manifest, snapshot=NoopSnapshot(id="recursive-grants")
            )
        )
    else:
        session = _ResumeTrackingSession(workspace_root=Path(manifest.root))
        session.state.manifest = manifest
        # Execute the shipped resolver and real rm against disposable filesystem data.
        monkeypatch.setattr(
            session,
            "_ensure_runtime_helper_installed",
            AsyncMock(return_value=_install_resolve_helper(tmp_path)),
        )
        monkeypatch.setattr(session, "_validate_path_access", session._validate_remote_path_access)
    return SandboxSession(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["local", "remote"])
@pytest.mark.parametrize("alias", ["none", "ancestor", "grant"])
async def test_recursive_remove_preserves_nested_read_only_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str, alias: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external"
    target = external / "tree"
    protected = target / "protected"
    protected.mkdir(parents=True)
    sentinel = protected / "config"
    sentinel.write_bytes(b"protected")
    writable = target / "writable"
    writable.mkdir()
    sibling = writable / "data"
    sibling.write_bytes(b"allowed")
    grant_path = protected
    remove_path = target
    if alias == "ancestor":
        link = workspace / "link"
        link.symlink_to(external, target_is_directory=True)
        remove_path = link / "tree"
    elif alias == "grant":
        grant_path = tmp_path / "protected-alias"
        grant_path.symlink_to(protected, target_is_directory=True)
    session = _removal_session(
        backend,
        Manifest(
            root=str(workspace),
            extra_path_grants=(
                SandboxPathGrant(path=str(external)),
                SandboxPathGrant(path=str(grant_path), read_only=True),
            ),
        ),
        tmp_path,
        monkeypatch,
    )

    with pytest.raises(WorkspaceArchiveWriteError) as direct:
        await session.rm(sentinel)
    assert direct.value.context["reason"] == "read_only_extra_path_grant"
    with pytest.raises((WorkspaceArchiveWriteError, ExecNonZeroError)):
        await session.rm(remove_path)
    assert sentinel.read_bytes() == b"protected"
    with pytest.raises(WorkspaceArchiveWriteError) as recursive:
        await session.rm(remove_path, recursive=True)
    assert recursive.value.context["reason"] == "read_only_extra_path_grant"
    assert sentinel.read_bytes() == b"protected"
    assert sibling.read_bytes() == b"allowed"

    await session.rm(writable, recursive=True)
    assert not writable.exists()
    assert sentinel.read_bytes() == b"protected"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["local", "remote"])
@pytest.mark.parametrize("precedence", ["workspace", "first-grant", "nested-writable"])
async def test_recursive_remove_preserves_writable_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str, precedence: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external"
    target = (workspace if precedence == "workspace" else external) / "tree"
    child = target / "child"
    child.mkdir(parents=True)
    (child / "data").write_bytes(b"allowed")
    grants = (
        SandboxPathGrant(path=str(external)),
        SandboxPathGrant(path=str(child), read_only=True),
    )
    if precedence == "first-grant":
        grants = (SandboxPathGrant(path=str(child)), *grants)
    elif precedence == "nested-writable":
        grants = (
            SandboxPathGrant(path=str(external), read_only=True),
            SandboxPathGrant(path=str(target)),
        )
    session = _removal_session(
        backend, Manifest(root=str(workspace), extra_path_grants=grants), tmp_path, monkeypatch
    )

    await session.rm(target, recursive=True)

    assert not target.exists()
    assert workspace.exists()


@pytest.mark.asyncio
async def test_remote_recursive_remove_unlinks_leaf_alias_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external"
    protected = external / "protected"
    protected.mkdir(parents=True)
    sentinel = protected / "data"
    sentinel.write_bytes(b"protected")
    link = workspace / "link"
    link.symlink_to(external, target_is_directory=True)
    session = _removal_session(
        "remote",
        Manifest(
            root=str(workspace),
            extra_path_grants=(
                SandboxPathGrant(path=str(external)),
                SandboxPathGrant(path=str(protected), read_only=True),
            ),
        ),
        tmp_path,
        monkeypatch,
    )

    await session.rm(link, recursive=True)

    assert not link.is_symlink()
    assert sentinel.read_bytes() == b"protected"


@pytest.mark.asyncio
async def test_unix_local_recursive_remove_as_user_preserves_nested_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agents.sandbox.types import ExecResult

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external"
    protected = external / "protected"
    protected.mkdir(parents=True)
    sentinel = protected / "data"
    sentinel.write_bytes(b"protected")
    session = _removal_session(
        "local",
        Manifest(
            root=str(workspace),
            extra_path_grants=(
                SandboxPathGrant(path=str(external)),
                SandboxPathGrant(path=str(protected), read_only=True),
            ),
        ),
        tmp_path,
        monkeypatch,
    )
    # Only the OS permission probe is simulated; use the real local deletion path.
    permission_probe = AsyncMock(return_value=ExecResult(stdout=b"", stderr=b"", exit_code=0))
    monkeypatch.setattr(session._inner, "exec", permission_probe)

    with pytest.raises(WorkspaceArchiveWriteError) as error:
        await session.rm(external, recursive=True, user="example-user")

    assert error.value.context["reason"] == "read_only_extra_path_grant"
    assert permission_probe.await_args is not None
    assert permission_probe.await_args.kwargs["user"] == "example-user"
    assert sentinel.read_bytes() == b"protected"


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
