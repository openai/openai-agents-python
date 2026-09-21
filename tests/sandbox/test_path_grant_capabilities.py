"""Read-only grants fail at backend capability boundaries before resource mutation."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from agents.run_config import SandboxRunConfig
from agents.sandbox import Manifest, SandboxPathGrant
from agents.sandbox.runtime_session_manager import SandboxRuntimeSessionManager
from agents.sandbox.sandbox_agent import SandboxAgent
from agents.sandbox.snapshot import NoopSnapshot

from . import _docker_removal_helpers as removal_helpers

service = removal_helpers.service


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "resume"])
@pytest.mark.parametrize(
    ("backend", "prefix", "state_fields", "option_fields"),
    [
        (
            "docker",
            "Docker",
            {"image": "test-image", "container_id": "existing"},
            {"image": "test-image"},
        ),
        ("unix_local", "UnixLocal", {}, {}),
        ("e2b", "E2B", {"sandbox_id": "existing"}, {"sandbox_type": "e2b"}),
        ("modal", "Modal", {"app_name": "test-app"}, {"app_name": "test-app"}),
        ("runloop", "Runloop", {"devbox_id": "existing"}, {}),
        ("daytona", "Daytona", {"sandbox_id": "existing"}, {}),
        ("blaxel", "Blaxel", {"sandbox_name": "existing"}, {}),
        (
            "cloudflare",
            "Cloudflare",
            {"sandbox_id": "existing", "worker_url": "https://example.invalid"},
            {"worker_url": "https://example.invalid"},
        ),
        ("vercel", "Vercel", {"sandbox_id": "existing"}, {}),
    ],
)
async def test_clients_reject_unsupported_grants_before_create_or_reconnect(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    prefix: str,
    state_fields: dict[str, Any],
    option_fields: dict[str, Any],
    operation: str,
) -> None:
    if backend in ("docker", "unix_local"):
        module = pytest.importorskip(f"agents.sandbox.sandboxes.{backend}", exc_type=ImportError)
    else:
        module = importlib.import_module(f"agents.extensions.sandbox.{backend}.sandbox")
    transport = Mock()
    if backend == "blaxel":
        monkeypatch.setattr(module, "_import_blaxel_sdk", lambda: transport)
    elif backend == "runloop":
        monkeypatch.setattr(
            module,
            "_import_runloop_sdk",
            lambda: SimpleNamespace(
                async_sdk=lambda **kwargs: transport,
            ),
        )
    elif backend == "daytona":
        monkeypatch.setattr(
            module,
            "_import_daytona_sdk",
            lambda: (
                lambda *args: transport,
                Mock(),
                Mock(),
                Mock(),
            ),
        )
    kwargs = {"docker_client": transport} if backend == "docker" else {}
    client = getattr(module, prefix + "SandboxClient")(**kwargs)
    transport.reset_mock()
    configured = Manifest(
        root="/home/user" if backend == "runloop" else "/workspace",
        extra_path_grants=(SandboxPathGrant(path="/opt/toolchain", read_only=True),),
    )
    snapshot = NoopSnapshot(id="capability-test")
    state = getattr(module, prefix + "SandboxSessionState")(
        manifest=configured,
        snapshot=snapshot,
        **state_fields,
    )
    previous = state.model_dump()
    with pytest.raises(ValueError, match="backend with atomic recursive removal") as caught:
        if operation == "create":
            await client.create(
                manifest=configured,
                snapshot=snapshot,
                options=getattr(module, prefix + "SandboxClientOptions")(**option_fields),
            )
        else:
            await client.resume(state)
    assert "DockerRemovalService" in str(caught.value)
    assert "rootful Linux daemon host" in str(caught.value)
    assert state.model_dump() == previous
    assert transport.mock_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["start", "runtime"])
async def test_injected_local_session_rejects_before_snapshot_or_workspace_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    module = pytest.importorskip("agents.sandbox.sandboxes.unix_local", exc_type=ImportError)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "existing").write_text("preserve")
    snapshot = NoopSnapshot(id="capability-test")
    restorable = AsyncMock(return_value=True)
    monkeypatch.setattr(NoopSnapshot, "restorable", restorable)
    current = module.UnixLocalSandboxSession(
        state=module.UnixLocalSandboxSessionState(
            manifest=Manifest(
                root=str(workspace),
                extra_path_grants=(
                    SandboxPathGrant(path=str(tmp_path / "toolchain"), read_only=True),
                ),
            ),
            snapshot=snapshot,
        )
    )
    running = AsyncMock(return_value=True)
    monkeypatch.setattr(current, "running", running)
    start = AsyncMock()
    prepare = AsyncMock()
    cleanup = AsyncMock()
    monkeypatch.setattr(current, "_ensure_backend_started", start)
    monkeypatch.setattr(current, "_prepare_backend_workspace", prepare)
    monkeypatch.setattr(current, "_clear_workspace_root_on_resume", cleanup)
    with pytest.raises(ValueError, match="backend with atomic recursive removal"):
        if entrypoint == "start":
            await current.start()
        else:
            agent = SandboxAgent(name="Capability preflight")
            manager = SandboxRuntimeSessionManager(
                starting_agent=agent,
                sandbox_config=SandboxRunConfig(session=current),
                run_state=None,
            )
            await manager._create_resources(agent=agent, capabilities=[], is_resumed_state=False)
    running.assert_not_awaited()
    start.assert_not_awaited()
    prepare.assert_not_awaited()
    restorable.assert_not_awaited()
    cleanup.assert_not_awaited()
    assert (workspace / "existing").read_text() == "preserve"


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
