"""Service and client removal tests use recording Docker and worker doubles."""

from __future__ import annotations

import asyncio
import io
import threading
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import docker.errors  # type: ignore[import-untyped]
import pytest

from agents.run_config import SandboxRunConfig
from agents.sandbox import Manifest, Permissions, SandboxPathGrant, User
from agents.sandbox.capabilities import Capability
from agents.sandbox.entries import File
from agents.sandbox.errors import InvalidManifestPathError, WorkspaceArchiveWriteError
from agents.sandbox.files import EntryKind, FileEntry
from agents.sandbox.runtime_session_manager import SandboxRuntimeSessionManager
from agents.sandbox.sandbox_agent import SandboxAgent
from agents.sandbox.sandboxes import (
    DockerRemovalService,
    docker_removal,
)
from agents.sandbox.sandboxes.docker import (
    DockerSandboxClient,
)

from . import _docker_removal_helpers as removal_helpers
from ._docker_removal_helpers import (
    RecordingContainer,
    RecordingWorker,
    manifest,
    session,
)

service = removal_helpers.service


@pytest.mark.asyncio
@pytest.mark.parametrize(("bound", "change_grants"), [(True, True), (True, False), (False, True)])
async def test_live_manifest_update_preserves_removal_authority(
    service: Any, monkeypatch: pytest.MonkeyPatch, bound: bool, change_grants: bool
) -> None:
    class ConfigureManifest(Capability):
        type: str = "configure_manifest"
        grants: tuple[SandboxPathGrant, ...]

        def process_manifest(self, manifest: Manifest) -> Manifest:
            return manifest.model_copy(
                update={
                    "extra_path_grants": self.grants,
                    "entries": {"added.txt": File(content=b"capability")},
                }
            )

    manager, container, worker = service
    configured = manifest()
    current = session(manager, container, configured)
    if bound:
        manager.bind_new(container, configured)
    else:
        current._removal_service = None
    monkeypatch.setattr(current, "running", AsyncMock(return_value=True))
    apply_entries = AsyncMock()
    monkeypatch.setattr(current, "_apply_entry_batch", apply_entries)
    original_state = current.state
    agent = SandboxAgent(name="Live removal authority")
    runtime = SandboxRuntimeSessionManager(
        starting_agent=agent, sandbox_config=SandboxRunConfig(session=current), run_state=None
    )
    grants = configured.extra_path_grants
    if change_grants:
        grants = (*grants, SandboxPathGrant(path="/new-grant", read_only=True))
    capability = ConfigureManifest(grants=grants)

    if bound and change_grants:
        with pytest.raises(ValueError, match="original live authority binding"):
            await runtime._create_resources(
                agent=agent, capabilities=[capability], is_resumed_state=False
            )
        assert current.state is original_state
        assert current.state.manifest == configured
        apply_entries.assert_not_awaited()
        assert [call["operation"] for call in worker.calls] == ["bind"]
    else:
        resources = await runtime._create_resources(
            agent=agent, capabilities=[capability], is_resumed_state=False
        )
        assert resources.session is current
        assert current.state.manifest.extra_path_grants == grants
        assert current.state.manifest.entries == {"added.txt": File(content=b"capability")}
        apply_entries.assert_awaited_once()
        applied = apply_entries.await_args
        assert applied is not None
        assert applied.args[0] == [(Path("/workspace/added.txt"), File(content=b"capability"))]

    if bound:
        await current.rm("build", recursive=True)
        assert worker.removed == ["/workspace/build"]


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["build", "/external/build"])
async def test_recursive_removal_preserves_unrelated_writable_trees(
    service: Any, target: str
) -> None:
    manager, container, worker = service
    configured = manifest()
    manager.bind_new(container, configured)
    await session(manager, container, configured).rm(
        target, recursive=True, user=User(name="developer")
    )
    assert worker.removed == ["/workspace/build" if target == "build" else target]
    assert worker.calls[-1] == {
        "operation": "remove",
        "user": "developer",
        "max_entry_visits": 100_000,
        "max_cpu_seconds": 10,
    }
    assert container.events == ["pause", "unpause", "pause", "unpause"]


@pytest.mark.asyncio
@pytest.mark.parametrize("service", [{"max_entry_visits": 3, "max_cpu_seconds": 1}], indirect=True)
async def test_removal_forwards_application_resource_limits(service: Any) -> None:
    manager, container, worker = service
    configured = manifest()
    manager.bind_new(container, configured)
    await session(manager, container, configured).rm("build", recursive=True)
    assert worker.calls[-1] == {
        "operation": "remove",
        "user": "1000:1000",
        "max_entry_visits": 3,
        "max_cpu_seconds": 1,
    }


@pytest.mark.parametrize(
    "limit", ["max_concurrent_removals", "max_entry_visits", "max_cpu_seconds"]
)
def test_service_rejects_nonpositive_limits_before_connecting(service: Any, limit: str) -> None:
    limits = {"max_concurrent_removals": 4, "max_entry_visits": 100_000, "max_cpu_seconds": 10}
    limits[limit] = 0
    docker_removal.DockerClient.reset_mock()
    with pytest.raises(ValueError, match="resource limits must be positive"):
        DockerRemovalService(**limits)
    docker_removal.DockerClient.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "is_directory"),
    [(".", True), ("/workspace", True), ("/external", True), ("/external", False)],
)
async def test_recursive_removal_preserves_live_binding_roots(
    service: Any, monkeypatch: pytest.MonkeyPatch, target: str, is_directory: bool
) -> None:
    manager, container, worker = service
    configured = Manifest(
        root="/workspace",
        extra_path_grants=(
            SandboxPathGrant(path="/external"),
            SandboxPathGrant(path="/protected", read_only=True),
        ),
    )
    manager.bind_new(container, configured)
    original_request = worker.request

    def request(**data: Any) -> dict[str, Any]:
        result = original_request(**data)
        if data["operation"] == "inspect":
            result["is_directory"] = is_directory
        return result

    monkeypatch.setattr(worker, "request", request)
    current = session(manager, container, configured)
    with pytest.raises(WorkspaceArchiveWriteError) as caught:
        await current.rm(target, recursive=True, user=User(name="0"))
    assert caught.value.context["reason"] == "docker_removal_bound_root"
    assert worker.removed == []
    assert not container.attrs["State"]["Paused"]
    await current.rm("build", recursive=True)
    assert worker.removed == ["/workspace/build"]


@pytest.mark.asyncio
async def test_recursive_removal_rejects_alias_to_ancestor_of_workspace(service: Any) -> None:
    manager, container, worker = service
    configured = Manifest(
        root="/external/group/workspace",
        extra_path_grants=(
            SandboxPathGrant(path="/external"),
            SandboxPathGrant(path="/protected", read_only=True),
        ),
    )
    manager.bind_new(container, configured)
    worker.aliases["/external/group/workspace/link/tree"] = "/external/group"
    current = session(manager, container, configured)
    with pytest.raises(WorkspaceArchiveWriteError) as caught:
        await current.rm("link/tree", recursive=True)
    assert caught.value.context["reason"] == "docker_removal_bound_root"
    assert worker.removed == []
    await current.rm("build", recursive=True)
    assert worker.removed == ["/external/group/workspace/build"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_stage", ["state", "unpause"])
@pytest.mark.parametrize("operation", ["denied", "worker_failure", "success"])
async def test_pause_cleanup_preserves_operation_failure(
    service: Any, monkeypatch: pytest.MonkeyPatch, cleanup_stage: str, operation: str
) -> None:
    manager, container, worker = service
    configured = manifest()
    manager.bind_new(container, configured)
    cleanup_error = docker.errors.APIError("cleanup unavailable")
    worker_error = RuntimeError("worker failure")
    primary_error = WorkspaceArchiveWriteError(
        path=Path("/workspace/build"), context={"reason": "test_denied"}
    )
    original_request = worker.request

    def fail_cleanup(*_: Any) -> None:
        raise cleanup_error

    def request(**data: Any) -> dict[str, Any]:
        if cleanup_stage == "state":
            monkeypatch.setattr(manager, "_state", fail_cleanup)
        else:
            monkeypatch.setattr(container, "unpause", fail_cleanup)
        if operation == "denied":
            raise primary_error
        if operation == "worker_failure":
            raise worker_error
        return original_request(**data)

    monkeypatch.setattr(worker, "request", request)
    current = session(manager, container, configured)
    if operation == "success":
        # A caller's handled exception must not be mistaken for an operation failure.
        try:
            raise ValueError("previous caller failure")
        except ValueError:
            with pytest.raises(docker.errors.APIError) as caught_cleanup:
                await current.rm("build", recursive=True)
        assert caught_cleanup.value is cleanup_error
        assert worker.removed == ["/workspace/build"]
    else:
        with pytest.raises(WorkspaceArchiveWriteError) as caught:
            await current.rm("build", recursive=True)
        assert caught.value.__context__ is cleanup_error
        if operation == "denied":
            assert caught.value is primary_error
            assert cleanup_error.__context__ is None
        else:
            assert caught.value.__cause__ is worker_error
            assert cleanup_error.__context__ is worker_error
        assert worker.removed == []
    assert container.attrs["State"]["Paused"]


@pytest.mark.asyncio
async def test_fixed_grant_alias_cannot_move_protection(service: Any) -> None:
    manager, container, worker = service
    configured = manifest()
    manager.bind_new(container, configured)
    worker.aliases["/grant-alias"] = "/unrelated"
    with pytest.raises(WorkspaceArchiveWriteError):
        await session(manager, container, configured).rm("/external", recursive=True)
    assert worker.removed == []
    assert not container.attrs["State"]["Paused"]


@pytest.mark.asyncio
async def test_target_alias_is_authorized_inside_pause(service: Any) -> None:
    manager, container, worker = service
    configured = manifest()
    manager.bind_new(container, configured)
    worker.aliases["/workspace/link/tree"] = "/external"
    with pytest.raises(WorkspaceArchiveWriteError):
        await session(manager, container, configured).rm("link/tree", recursive=True)
    assert worker.removed == []


@pytest.mark.asyncio
async def test_pruned_restore_uses_real_rm_with_unrelated_grant(
    service: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, container, worker = service
    configured = manifest()
    manager.bind_new(container, configured)
    current = session(manager, container, configured)

    async def listing(_: Path) -> list[FileEntry]:
        return [
            FileEntry(
                path="/workspace/build",
                kind=EntryKind.DIRECTORY,
                permissions=Permissions(directory=True),
                owner="0",
                group="0",
                size=0,
            )
        ]

    monkeypatch.setattr(current, "ls", listing)
    await current._clear_workspace_dir_on_resume_pruned(
        current_dir=Path("/workspace"), skip_rel_paths=set()
    )
    assert worker.removed == ["/workspace/build"]


def test_existing_pause_is_owned_by_its_caller(service: Any) -> None:
    manager, container, worker = service
    configured = manifest()
    container.attrs["State"]["Paused"] = True
    manager.bind_new(container, configured)
    manager.remove(container, configured, "build", None)
    assert container.attrs["State"]["Paused"]
    assert container.events == []
    assert worker.calls[-1]["user"] == "1000:1000"


def test_transport_uncertainty_leaves_workload_paused(
    service: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, container, worker = service
    configured = manifest()
    manager.bind_new(container, configured)

    def lost_request(**_: Any) -> dict[str, Any]:
        worker.uncertain = True
        raise RuntimeError("lost worker")

    monkeypatch.setattr(worker, "request", lost_request)
    with pytest.raises(WorkspaceArchiveWriteError):
        manager.remove(container, configured, "build", None)
    assert container.attrs["State"]["Paused"]
    assert worker.removed == []
    with pytest.raises(ValueError, match="no longer usable"):
        manager.remove(container, configured, "build", None)


def test_binding_cannot_be_reconstructed_from_persisted_configuration(service: Any) -> None:
    manager, container, _ = service
    with pytest.raises(ValueError, match="original live authority"):
        manager.assert_bound(container, manifest())


def test_changed_configuration_requires_new_authority(service: Any) -> None:
    manager, container, worker = service
    manager.bind_new(container, manifest())
    with pytest.raises(ValueError, match="original live authority"):
        manager.remove(container, Manifest(root="/workspace"), "build", None)
    assert worker.removed == []


def test_windows_path_is_rejected_before_mutation(service: Any) -> None:
    manager, container, worker = service
    configured = manifest()
    manager.bind_new(container, configured)
    with pytest.raises(InvalidManifestPathError):
        manager.remove(
            container, configured, cast(Any, PureWindowsPath("C:/workspace/build")), None
        )
    assert worker.removed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "exception", "base_exception"])
async def test_repeated_cancellation_waits_for_actual_host_completion(
    service: Any, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    manager, container, _ = service
    configured = manifest()
    manager.bind_new(container, configured)
    current = session(manager, container, configured)
    started = threading.Event()
    finish = threading.Event()
    completed: list[str] = []

    def operation() -> None:
        started.set()
        finished = finish.wait(5)
        assert finished
        completed.append("finished")
        if outcome == "exception":
            raise RuntimeError("worker failed")
        if outcome == "base_exception":
            raise BaseException("worker stopped")

    monkeypatch.setattr(manager, "remove", lambda *args: operation())
    task = asyncio.create_task(current.rm("build", recursive=True))
    try:
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert completed == ["finished"]
    finally:
        finish.set()


def test_worker_transport_eof_marks_outcome_uncertain() -> None:
    worker = object.__new__(docker_removal._Worker)
    worker.process = cast(Any, SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO("")))
    worker.uncertain = False
    with pytest.raises(RuntimeError, match="transport failed"):
        worker.request(operation="inspect", path="/workspace/build")
    assert worker.uncertain


def test_client_rejects_a_service_connected_to_another_daemon(service: Any) -> None:
    manager, _, _ = service
    with pytest.raises(ValueError, match="connection"):
        DockerSandboxClient(Mock(), removal_service=manager)


@pytest.mark.parametrize(
    "host_configuration",
    [
        {"Privileged": True},
        {"CapAdd": ["SYS_ADMIN"]},
        {"SecurityOpt": ["seccomp=unconfined"]},
        {"PidMode": "container:other"},
        {"Runtime": "unverified"},
    ],
)
def test_host_service_rejects_uncontrolled_execution_modes(
    service: Any, host_configuration: dict[str, Any]
) -> None:
    manager, container, _ = service
    manager.docker_client.info.return_value = {
        "SecurityOptions": ["name=seccomp,profile=builtin"],
        "DefaultRuntime": "runc",
    }
    manager.docker_client.version.return_value = {"Version": "26.0.0"}
    container.attrs.update(HostConfig=host_configuration)
    container.attrs["State"].update(Running=True, Pid=123, StartedAt="incarnation")
    with pytest.raises(ValueError, match="private container"):
        DockerRemovalService._state(manager, container)


def test_host_service_verifies_local_pid_and_identity_user_maps(
    service: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, container, _ = service
    manager.docker_client.info.return_value = {
        "SecurityOptions": ["name=seccomp,profile=builtin"],
        "DefaultRuntime": "runc",
    }
    manager.docker_client.version.return_value = {"Version": "26.0.0"}
    container.attrs.update(HostConfig={})
    container.attrs["State"].update(Running=True, Pid=123, StartedAt="incarnation")
    values = {
        "cgroup": f"0::/system.slice/docker-{container.id}.scope",
        "uid_map": "0 0 4294967295",
        "gid_map": "0 0 4294967295",
    }
    monkeypatch.setattr(Path, "read_text", lambda path: values[path.name])
    assert DockerRemovalService._state(manager, container) == (123, "incarnation")
    container.attrs["Mounts"] = [{"Type": "bind", "RW": False, "Propagation": "rprivate"}]
    assert DockerRemovalService._state(manager, container) == (123, "incarnation")
    for field, value in (("Type", "volume"), ("RW", True), ("Propagation", "rshared")):
        mount = container.attrs["Mounts"][0]
        previous = mount[field]
        mount[field] = value
        with pytest.raises(ValueError, match="private container"):
            DockerRemovalService._state(manager, container)
        mount[field] = previous
    values["cgroup"] = "0::/unrelated"
    with pytest.raises(ValueError, match="not on this service's host"):
        DockerRemovalService._state(manager, container)


def test_writable_shared_mounts_cannot_acquire_authority(service: Any) -> None:
    manager, container, worker = service
    configured = Manifest(
        root="/workspace",
        extra_path_grants=(SandboxPathGrant(path="/toolchain", host_path="/host/toolchain"),),
    )
    with pytest.raises(ValueError, match="shared host paths"):
        manager.bind_new(container, configured)
    assert worker.calls == []
    assert not container.attrs["State"]["Paused"]


@pytest.mark.asyncio
@pytest.mark.parametrize("restore", [False, True])
async def test_read_only_host_mount_preserves_workspace_cleanup(
    service: Any, monkeypatch: pytest.MonkeyPatch, restore: bool, tmp_path: Path
) -> None:
    from agents.sandbox.sandboxes.docker import DockerSandboxClientOptions

    manager, container, worker = service
    source = str(tmp_path / "toolchain")
    configured = Manifest(
        root="/workspace",
        extra_path_grants=(SandboxPathGrant(path="/toolchain", host_path=source, read_only=True),),
    )
    container.attrs["Mounts"] = [
        {
            "Type": "bind",
            "Source": source,
            "Destination": "/toolchain",
            "RW": False,
            "Propagation": "rprivate",
        }
    ]
    monkeypatch.setattr(container, "start", lambda: None, raising=False)
    client = DockerSandboxClient(manager.docker_client, removal_service=manager)
    monkeypatch.setattr(client, "_create_container", AsyncMock(return_value=container))
    wrapped = await client.create(
        manifest=configured, options=DockerSandboxClientOptions(image="trusted-image")
    )
    if restore:
        current = session(manager, container, configured)
        monkeypatch.setattr(
            current,
            "ls",
            AsyncMock(
                return_value=[
                    FileEntry(
                        path="/workspace/build",
                        kind=EntryKind.DIRECTORY,
                        permissions=Permissions(directory=True),
                        owner="0",
                        group="0",
                        size=0,
                    )
                ]
            ),
        )
        await current._clear_workspace_dir_on_resume_pruned(
            current_dir=Path("/workspace"), skip_rel_paths=set()
        )
    else:
        await wrapped.rm("build", recursive=True)
    assert worker.removed == ["/workspace/build"]
    worker.aliases["/workspace/link/child"] = "/toolchain/child"
    with pytest.raises(WorkspaceArchiveWriteError):
        await wrapped.rm("link/child", recursive=True)
    assert worker.removed == ["/workspace/build"]
    changed = configured.model_copy(
        update={
            "extra_path_grants": (
                SandboxPathGrant(
                    path="/toolchain", host_path=str(tmp_path / "replaced"), read_only=True
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="original live authority"):
        manager.assert_bound(container, changed)


@pytest.mark.parametrize("invalid", ["source", "writable", "missing", "workspace_alias"])
def test_read_only_host_binding_rejects_untrusted_mount_layout(
    service: Any, invalid: str, tmp_path: Path
) -> None:
    manager, container, worker = service
    source = str(tmp_path / "toolchain")
    configured = Manifest(
        root="/workspace",
        extra_path_grants=(SandboxPathGrant(path="/toolchain", host_path=source, read_only=True),),
    )
    mount = {
        "Type": "bind",
        "Source": source,
        "Destination": "/toolchain",
        "RW": False,
        "Propagation": "rprivate",
    }
    if invalid == "source":
        mount["Source"] = str(tmp_path / "other")
    if invalid == "writable":
        mount["RW"] = True
    container.attrs["Mounts"] = [] if invalid == "missing" else [mount]
    if invalid == "workspace_alias":
        worker.aliases["/toolchain"] = "/workspace/mounted"
    with pytest.raises(ValueError):
        manager.bind_new(container, configured)
    assert not manager._bindings
    assert not worker.removed
    assert not container.attrs["State"]["Paused"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [None, "writable_first", "read_only_first", "workspace", "transport", "close"]
)
async def test_create_binds_before_returning_the_session(
    service: Any, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    manager, container, worker = service
    monkeypatch.setattr(container, "start", lambda: container.events.append("start"), raising=False)
    removed_pauses: list[bool] = []
    remove = Mock(
        side_effect=lambda **kwargs: removed_pauses.append(container.attrs["State"]["Paused"])
    )
    monkeypatch.setattr(container, "remove", remove, raising=False)
    from agents.sandbox.sandboxes.docker import DockerSandboxClientOptions

    client = DockerSandboxClient(manager.docker_client, removal_service=manager)
    configured = manifest()
    primary = RuntimeError("worker transport failed")
    if failure in ("transport", "close"):
        request = worker.request

        def lost_request(**kwargs: Any) -> dict[str, Any]:
            request(**kwargs)
            worker.uncertain = True
            raise primary

        monkeypatch.setattr(worker, "request", lost_request)
        if failure == "close":
            monkeypatch.setattr(
                worker, "close", Mock(side_effect=BrokenPipeError("cleanup failed"))
            )
    elif failure is not None:
        worker.aliases["/grant-alias"] = "/workspace" if failure == "workspace" else "/external"
        if failure == "read_only_first":
            configured = configured.model_copy(
                update={"extra_path_grants": tuple(reversed(configured.extra_path_grants))}
            )

    async def create_container(*args: Any, **kwargs: Any) -> RecordingContainer:
        return container

    monkeypatch.setattr(client, "_create_container", create_container)
    if failure is not None:
        with pytest.raises((ValueError, RuntimeError)) as caught:
            await client.create(
                manifest=configured, options=DockerSandboxClientOptions(image="trusted-image")
            )
        assert manager._bindings == {}
        assert [call["operation"] for call in worker.calls] == ["bind"]
        assert worker.removed == []
        if failure in ("transport", "close"):
            assert caught.value is primary
            assert container.events == (
                ["start", "pause", "close"] if failure == "transport" else ["start", "pause"]
            )
            assert removed_pauses == [True]
        else:
            assert "distinct canonical workspace and grant roots" in str(caught.value)
            assert container.events == ["start", "pause", "close", "unpause"]
        if failure == "close":
            worker.close.assert_called_once_with()
        remove.assert_called_once_with(force=True)
        return

    wrapped = await client.create(
        manifest=configured, options=DockerSandboxClientOptions(image="trusted-image")
    )
    await wrapped.rm("build", recursive=True)
    assert container.events[:3] == ["start", "pause", "unpause"]
    assert worker.removed == ["/workspace/build"]


@pytest.mark.asyncio
async def test_relative_parent_segments_use_the_normalized_request(service: Any) -> None:
    manager, container, worker = service
    configured = manifest()
    manager.bind_new(container, configured)
    await session(manager, container, configured).rm("link/../build", recursive=True)
    assert worker.calls[-2] == {
        "operation": "inspect",
        "path": "/workspace/build",
        "workspace_root": "/workspace",
    }
    assert worker.removed == ["/workspace/build"]


@pytest.mark.asyncio
async def test_missing_target_still_checks_the_requested_user(
    service: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, container, worker = service
    configured = manifest()
    manager.bind_new(container, configured)
    calls: list[dict[str, Any]] = []

    def request(**data: Any) -> dict[str, Any]:
        assert container.attrs["State"]["Paused"]
        calls.append(data)
        if data["operation"] == "inspect":
            return {"path": "", "is_directory": False}
        raise RuntimeError("PermissionError")

    monkeypatch.setattr(worker, "request", request)
    with pytest.raises(WorkspaceArchiveWriteError):
        await session(manager, container, configured).rm(
            "private/missing", recursive=True, user="developer"
        )
    assert calls == [
        {
            "operation": "inspect",
            "path": "/workspace/private/missing",
            "workspace_root": "/workspace",
        },
        {
            "operation": "remove",
            "user": "developer",
            "max_entry_visits": 100_000,
            "max_cpu_seconds": 10,
        },
    ]
    assert not container.attrs["State"]["Paused"]


def test_worker_close_reaps_and_closes_output_after_broken_input_pipe() -> None:
    worker = object.__new__(docker_removal._Worker)
    broken_pipe = BrokenPipeError("input pipe closed")
    events: list[str] = []

    def close_input() -> None:
        events.append("stdin.close")
        raise broken_pipe

    def wait() -> None:
        events.append("wait")
        raise OSError("secondary wait failure")

    worker.process = cast(
        Any,
        SimpleNamespace(
            stdin=SimpleNamespace(close=close_input),
            wait=wait,
            stdout=SimpleNamespace(close=lambda: events.append("stdout.close")),
        ),
    )
    with pytest.raises(BrokenPipeError) as caught:
        worker.close()
    assert caught.value is broken_pipe
    assert events == ["stdin.close", "wait", "stdout.close"]


def test_service_close_attempts_all_workers_and_client_after_a_worker_failure(service: Any) -> None:
    manager, _, _ = service
    primary = BrokenPipeError("worker input closed")
    failed = Mock(close=Mock(side_effect=primary))
    survivor = Mock()
    manager._bindings = {
        "first": SimpleNamespace(close=failed.close),
        "second": SimpleNamespace(close=survivor.close),
    }
    manager.docker_client.close.side_effect = OSError("secondary client failure")
    with pytest.raises(BrokenPipeError) as caught:
        manager.close()
    assert caught.value is primary
    failed.close.assert_called_once_with()
    survivor.close.assert_called_once_with()
    manager.docker_client.close.assert_called_once_with()
    assert manager._bindings == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["removed", "missing_at_lookup", "missing_at_remove", "lookup_error", "remove_error"]
)
async def test_delete_releases_authority_only_after_confirmed_container_removal(
    service: Any, outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, container, worker = service
    configured = manifest()
    manager.bind_new(container, configured)
    client = DockerSandboxClient(manager.docker_client, removal_service=manager)
    inner = session(manager, container, configured)
    shutdown = AsyncMock()
    monkeypatch.setattr(inner, "shutdown", shutdown)
    wrapped = client._wrap_session(inner, instrumentation=client._instrumentation)
    container.remove = Mock()
    manager.docker_client.containers.get.return_value = container
    if outcome == "missing_at_lookup":
        manager.docker_client.containers.get.side_effect = docker.errors.NotFound("gone")
    elif outcome == "missing_at_remove":
        container.remove.side_effect = docker.errors.NotFound("gone")
    elif outcome == "lookup_error":
        manager.docker_client.containers.get.side_effect = docker.errors.APIError("unavailable")
    elif outcome == "remove_error":
        container.remove.side_effect = docker.errors.APIError("unavailable")
    if outcome.endswith("error"):
        with pytest.raises(docker.errors.APIError):
            await client.delete(wrapped)
        assert container.id in manager._bindings
        assert "close" not in container.events
    else:
        deleted = await client.delete(wrapped)
        assert deleted is wrapped
        assert container.id not in manager._bindings
        assert container.events.count("close") == 1
    shutdown.assert_awaited_once_with()
    assert worker.removed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("service", [{"max_concurrent_removals": 1}], indirect=True)
@pytest.mark.parametrize("outcome", ["success", "failure", "uncertain"])
async def test_service_bounds_concurrent_removal_without_pausing_rejected_work(
    service: Any, outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, first_container, first_worker = service
    second_container = RecordingContainer()
    second_container.id = "b" * 64
    second_worker = RecordingWorker(second_container)
    monkeypatch.setattr(docker_removal, "_Worker", Mock(side_effect=[first_worker, second_worker]))
    configured = manifest()
    manager.bind_new(first_container, configured)
    manager.bind_new(second_container, configured)
    first_session = session(manager, first_container, configured)
    second_session = session(manager, second_container, configured)
    entered = threading.Event()
    finish = threading.Event()
    request = first_worker.request

    def block(**data: Any) -> dict[str, Any]:
        if data["operation"] == "remove":
            entered.set()
            if not finish.wait(5):
                raise RuntimeError("test completion was not released")
            first_worker.uncertain = outcome == "uncertain"
            if outcome != "success":
                raise RuntimeError("worker failure")
        return request(**data)

    first_worker.request = block
    first_task = asyncio.create_task(first_session.rm("build", recursive=True))
    try:
        started = await asyncio.to_thread(entered.wait, 5)
        assert started
        events = list(second_container.events)
        with pytest.raises(WorkspaceArchiveWriteError) as caught:
            await second_session.rm("build", recursive=True)
        assert caught.value.context["reason"] == "docker_removal_capacity"
        assert second_worker.removed == []
        assert second_container.events == events
        assert not first_task.done()
    finally:
        finish.set()
        results = await asyncio.gather(first_task, return_exceptions=True)
    assert (
        (results == [None])
        if outcome == "success"
        else isinstance(results[0], WorkspaceArchiveWriteError)
    )
    if outcome == "uncertain":
        assert first_container.attrs["State"]["Paused"]
        manager.release(first_container.id)
        with pytest.raises(WorkspaceArchiveWriteError) as caught:
            await second_session.rm("build", recursive=True)
        assert caught.value.context["reason"] == "docker_removal_capacity"
    else:
        await second_session.rm("build", recursive=True)
        assert second_worker.removed == ["/workspace/build"]
        assert not first_container.attrs["State"]["Paused"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["rm", "delete"])
async def test_unrelated_container_progresses_during_a_blocked_removal(
    service: Any, operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, first_container, first_worker = service
    second_container = RecordingContainer()
    second_container.id = "b" * 64
    second_container.remove = Mock()
    second_worker = RecordingWorker(second_container)
    monkeypatch.setattr(docker_removal, "_Worker", Mock(side_effect=[first_worker, second_worker]))
    configured = manifest()
    manager.bind_new(first_container, configured)
    manager.bind_new(second_container, configured)
    first_session = session(manager, first_container, configured)
    second_session = session(manager, second_container, configured)
    entered = threading.Event()
    finish = threading.Event()
    request = first_worker.request

    def block(**data: Any) -> dict[str, Any]:
        if data["operation"] == "remove":
            entered.set()
            if not finish.wait(5):
                raise RuntimeError("test completion was not released")
        return request(**data)

    first_worker.request = block
    first_task = asyncio.create_task(first_session.rm("build", recursive=True))
    second_task = None
    try:
        started = await asyncio.to_thread(entered.wait, 5)
        assert started
        if operation == "rm":
            second_task = asyncio.create_task(second_session.rm("build", recursive=True))
        else:
            client = DockerSandboxClient(manager.docker_client, removal_service=manager)
            monkeypatch.setattr(second_session, "shutdown", AsyncMock())
            manager.docker_client.containers.get.return_value = second_container
            wrapped = client._wrap_session(second_session, instrumentation=client._instrumentation)
            second_task = asyncio.create_task(client.delete(wrapped))
        done, _ = await asyncio.wait({second_task}, timeout=1)
        assert second_task in done
        second_task.result()
        assert not first_task.done()
        assert not finish.is_set()
    finally:
        finish.set()
        await asyncio.gather(
            first_task, *([second_task] if second_task else []), return_exceptions=True
        )
    assert first_worker.removed == ["/workspace/build"]
    assert not first_container.attrs["State"]["Paused"]
    if operation == "rm":
        assert second_worker.removed == ["/workspace/build"]
    else:
        assert second_container.id not in manager._bindings
        assert second_container.events.count("close") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("accessible", [False, True])
async def test_relative_removal_preserves_workspace_alias_traversal(
    service: Any, accessible: bool
) -> None:
    manager, container, worker = service
    alias = "/private/workspace-alias"
    worker.aliases[alias] = "/workspace"
    worker.aliases[alias + "/build"] = "/workspace/build"
    configured = Manifest(root=alias)
    manager.bind_new(container, configured)
    request = worker.request
    inspected: dict[str, Any] = {}

    def check_user(**data: Any) -> dict[str, Any]:
        if data["operation"] == "inspect":
            inspected.update(data)
        if data["operation"] == "remove":
            assert data["user"] == "developer"
            if inspected["path"].startswith("/private/") and not accessible:
                raise RuntimeError("PermissionError")
        return request(**data)

    worker.request = check_user
    current = session(manager, container, configured)
    if accessible:
        await current.rm("build", recursive=True, user="developer")
    else:
        with pytest.raises(WorkspaceArchiveWriteError):
            await current.rm("build", recursive=True, user="developer")
    assert inspected == {"operation": "inspect", "path": alias + "/build", "workspace_root": alias}
    assert worker.removed == (["/workspace/build"] if accessible else [])


@pytest.mark.asyncio
async def test_delete_keeps_event_loop_live_and_waits_for_worker_cleanup_on_cancel(
    service: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, container, worker = service
    manager.bind_new(container, manifest())
    container.remove = Mock()
    manager.docker_client.containers.get.return_value = container
    client = DockerSandboxClient(manager.docker_client, removal_service=manager)
    inner = session(manager, container, manifest())
    monkeypatch.setattr(inner, "shutdown", AsyncMock())
    wrapped = client._wrap_session(inner, instrumentation=client._instrumentation)
    entered = threading.Event()
    finish = threading.Event()
    completed: list[str] = []

    def close() -> None:
        entered.set()
        if not finish.wait(5):
            raise RuntimeError("test completion was not released")
        completed.append("closed")

    worker.close = close
    task = asyncio.create_task(client.delete(wrapped))
    try:
        started = await asyncio.to_thread(entered.wait, 5)
        assert started
        assert not task.done()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)
    assert completed == ["closed"]
    assert container.id not in manager._bindings
