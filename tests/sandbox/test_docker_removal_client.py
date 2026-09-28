"""Docker client lifecycle tests with simulated transport and real filesystem binding."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from agents.sandbox import Manifest, SandboxPathGrant
from agents.sandbox.errors import WorkspaceArchiveWriteError
from agents.sandbox.sandboxes.docker import DockerSandboxClient, DockerSandboxClientOptions
from agents.sandbox.sandboxes.docker_removal import DockerRemovalService

from . import _docker_removal_helpers as removal_helpers
from ._docker_removal_helpers import RecordingContainer, RecordingWorker, session

service = removal_helpers.service
worker_code = pytest.importorskip(
    "agents.sandbox.sandboxes._docker_removal_worker", exc_type=ImportError
)


@pytest.fixture
def client_lifecycle(
    service: tuple[DockerRemovalService, RecordingContainer, RecordingWorker],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[DockerSandboxClient, DockerRemovalService, Any, RecordingWorker]]:
    manager, container, worker = service
    client = DockerSandboxClient(manager.docker_client, removal_service=manager)
    monkeypatch.setattr(client, "get_container", lambda _: None)
    monkeypatch.setattr(container, "start", lambda: None, raising=False)
    monkeypatch.setattr(container, "remove", Mock(), raising=False)

    def create_container(**kwargs: Any) -> RecordingContainer:
        # Docker's daemon creates its working directory before startup.
        if workdir := kwargs.get("working_dir"):
            Path(workdir).mkdir(parents=True, exist_ok=True)
        return container

    manager.docker_client.containers.create.side_effect = create_container
    # The real worker enters the container root. Model that root on tmp_path's
    # filesystem, which may differ from the host root (for example, tmpfs /tmp).
    # Keep canonicalization, open/fstat, and descriptor identity checks real.
    root_stat = tmp_path.stat()

    def container_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        return root_stat if path == "/" else os.stat(path, *args, **kwargs)

    worker_os = SimpleNamespace(**vars(os))
    worker_os.stat = container_stat
    # Exercise real O_PATH on Linux; other Unix hosts use read-only descriptors.
    worker_os.O_PATH = getattr(os, "O_PATH", os.O_RDONLY)
    monkeypatch.setattr(worker_code, "os", worker_os)
    with ExitStack() as bindings:
        original_request = worker.request

        def request(**data: Any) -> dict[str, Any]:
            response = original_request(**data)
            if data["operation"] == "bind":
                bound = bindings.enter_context(worker_code._bind_paths(data["paths"]))
                response["paths"] = bound.paths
            return response

        monkeypatch.setattr(worker, "request", request)
        try:
            yield client, manager, container, worker
        finally:
            manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("workspace_setup", ["missing", "existing", "ancestor_grant"])
async def test_client_bootstraps_workspace_before_strict_binding(
    client_lifecycle: tuple[DockerSandboxClient, DockerRemovalService, Any, RecordingWorker],
    tmp_path: Path,
    resume: bool,
    workspace_setup: str,
) -> None:
    client, manager, container, worker = client_lifecycle
    root = tmp_path / "nested" / "workspace"
    if workspace_setup == "existing":
        root.mkdir(parents=True)
        (root / "keep.txt").write_text("existing contents")
    grants = (
        (SandboxPathGrant(path=str(root.parent), read_only=True),)
        if workspace_setup == "ancestor_grant"
        else ()
    )
    configured = Manifest(root=str(root), extra_path_grants=grants)
    state = session(manager, container, configured).state
    state.container_id = "missing-container"
    if resume:
        wrapped = await client.resume(state)
    else:
        wrapped = await client.create(
            manifest=configured, options=DockerSandboxClientOptions(image="trusted-image")
        )
    assert root.is_dir()
    manager.assert_bound(container, configured)
    assert not wrapped._inner.state.workspace_root_ready
    if workspace_setup == "existing":
        assert (root / "keep.txt").read_text() == "existing contents"
    with pytest.raises(WorkspaceArchiveWriteError):
        await wrapped.rm(str(root), recursive=True)
    if grants:
        with pytest.raises(WorkspaceArchiveWriteError):
            await wrapped.rm(str(root.parent), recursive=True)
    assert not worker.removed


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
async def test_client_bootstrap_does_not_create_missing_grant_roots(
    client_lifecycle: tuple[DockerSandboxClient, DockerRemovalService, Any, RecordingWorker],
    tmp_path: Path,
    resume: bool,
) -> None:
    client, manager, container, worker = client_lifecycle
    root = tmp_path / "workspace"
    grant = tmp_path / "external"
    configured = Manifest(root=str(root), extra_path_grants=(SandboxPathGrant(path=str(grant)),))
    state = session(manager, container, configured).state
    state.container_id = "missing-container"
    state.workspace_root_ready = True
    original_session_id = state.session_id
    with pytest.raises(FileNotFoundError):
        if resume:
            await client.resume(state)
        else:
            await client.create(
                manifest=configured, options=DockerSandboxClientOptions(image="trusted-image")
            )
    assert root.is_dir()
    assert not grant.exists()
    assert not manager._bindings
    container.remove.assert_called_once_with(force=True)
    assert "close" in container.events
    if resume:
        assert state.container_id == "missing-container"
        assert state.session_id == original_session_id
        assert state.workspace_root_ready
