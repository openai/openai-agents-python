"""Host removal tests use recording workers; no Docker or filesystem mutations run."""

from __future__ import annotations

import asyncio
import errno
import io
import json
import stat
import sys
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
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
    DockerSandboxSession,
    DockerSandboxSessionState,
)
from agents.sandbox.sandboxes.docker_removal import _Worker
from agents.sandbox.snapshot import NoopSnapshot

worker_code = pytest.importorskip(
    "agents.sandbox.sandboxes._docker_removal_worker", exc_type=ImportError
)


class RecordingContainer:
    id = "a" * 64

    def __init__(self) -> None:
        self.attrs = {"State": {"Paused": False}, "Config": {"User": "1000:1000"}}
        self.events: list[str] = []

    def pause(self) -> None:
        self.events.append("pause")
        self.attrs["State"]["Paused"] = True

    def reload(self) -> None:
        pass

    def unpause(self) -> None:
        self.events.append("unpause")
        self.attrs["State"]["Paused"] = False


class RecordingWorker:
    def __init__(self, container: RecordingContainer) -> None:
        self.container = container
        self.uncertain = False
        self.aliases = {"/grant-alias": "/external/protected"}
        self.calls: list[dict[str, Any]] = []
        self.removed: list[str] = []
        self.selected = ""

    def request(self, **request: Any) -> dict[str, Any]:
        assert self.container.attrs["State"]["Paused"]
        self.calls.append(request)
        if request["operation"] == "bind":
            return {"paths": [self.aliases.get(path, path) for path in request["paths"]]}
        if request["operation"] == "inspect":
            self.selected = self.aliases.get(request["path"], request["path"])
            return {"path": self.selected, "is_directory": True}
        assert request["operation"] == "remove"
        self.removed.append(self.selected)
        return {}

    def close(self) -> None:
        self.container.events.append("close")


@pytest.fixture
def service(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> tuple[DockerRemovalService, RecordingContainer, RecordingWorker]:
    monkeypatch.setattr(
        docker_removal, "sys", SimpleNamespace(platform="linux", exc_info=sys.exc_info)
    )
    monkeypatch.setattr(docker_removal, "os", SimpleNamespace(geteuid=lambda: 0))
    monkeypatch.setattr(docker_removal, "DockerClient", Mock(return_value=Mock()))
    limits = {"max_concurrent_removals": 4, "max_entry_visits": 100_000, "max_cpu_seconds": 10}
    limits.update(getattr(request, "param", {}))
    instance = DockerRemovalService(**limits)
    container = RecordingContainer()
    worker = RecordingWorker(container)
    monkeypatch.setattr(instance, "_state", lambda _: (123, "incarnation"))
    monkeypatch.setattr(docker_removal, "_Worker", lambda _: worker)
    return instance, container, worker


def manifest() -> Manifest:
    return Manifest(
        root="/workspace",
        extra_path_grants=(
            SandboxPathGrant(path="/external"),
            SandboxPathGrant(path="/grant-alias", read_only=True),
        ),
    )


def session(
    service: DockerRemovalService, container: Any, configured: Manifest
) -> DockerSandboxSession:
    return DockerSandboxSession(
        docker_client=service.docker_client,
        container=container,
        state=DockerSandboxSessionState(
            session_id=uuid.uuid4(),
            manifest=configured,
            image="trusted-image",
            snapshot=NoopSnapshot(id="removal-tests"),
            container_id=container.id,
        ),
        removal_service=service,
    )


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


def test_empty_directory_needs_no_search_of_its_contents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_code.os, "lstat", lambda _: SimpleNamespace(st_mode=0o040000))
    removed: list[str] = []
    monkeypatch.setattr(worker_code.os, "rmdir", removed.append)
    monkeypatch.setattr(
        worker_code.os, "scandir", Mock(side_effect=AssertionError("must not search"))
    )
    worker_code._remove("/workspace/empty", max_entry_visits=100_000)
    assert removed == ["/workspace/empty"]


@pytest.mark.parametrize("path", ["/", "//", "///", "/workspace/.."])
def test_worker_refuses_filesystem_root_without_filesystem_calls(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        worker_code.os, "lstat", Mock(side_effect=AssertionError("must not inspect root"))
    )
    with pytest.raises(ValueError, match="filesystem_root"):
        worker_code._selected_path(path)


def test_replaced_bound_root_is_rejected_even_through_new_parent_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bindings = object.__new__(worker_code._Bindings)
    bindings.paths, bindings.fds = ["/data/protected"], [123]
    same_inode = SimpleNamespace(st_dev=1, st_ino=2)
    monkeypatch.setattr(worker_code.os, "stat", lambda *args, **kwargs: same_inode)
    monkeypatch.setattr(worker_code.os, "fstat", lambda _: same_inode)
    monkeypatch.setattr(worker_code, "_canonical", lambda _: "/elsewhere/protected")
    with pytest.raises(ValueError, match="bound_root_replaced"):
        bindings.validate()


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
    values["cgroup"] = "0::/unrelated"
    with pytest.raises(ValueError, match="not on this service's host"):
        DockerRemovalService._state(manager, container)


def test_shared_mounts_cannot_acquire_authority(service: Any) -> None:
    manager, container, worker = service
    configured = Manifest(
        root="/workspace",
        extra_path_grants=(
            SandboxPathGrant(path="/toolchain", host_path="/host/toolchain", read_only=True),
        ),
    )
    with pytest.raises(ValueError, match="shared host paths"):
        manager.bind_new(container, configured)
    assert worker.calls == []
    assert not container.attrs["State"]["Paused"]


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        ("developer", (1000, 1001, [2000])),
        ("1000", (1000, 1001, [2000])),
        ("1000:3000", (1000, 3000, [])),
        ("developer:tools", (1000, 2000, [])),
        ("developer:3000", (1000, 3000, [])),
    ],
)
def test_requested_user_and_groups_are_preserved(
    user: str, expected: tuple[int, int, list[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts = {
        "/etc/passwd": [["developer", "x", "1000", "1001", "", "/home/developer", "/bin/sh"]],
        "/etc/group": [["tools", "x", "2000", "developer"]],
    }
    monkeypatch.setattr(worker_code, "_accounts", lambda path: (entry for entry in accounts[path]))
    assert worker_code._user_ids(user) == expected


def test_namespace_entry_uses_only_mount_namespace_and_closes_host_directory_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        worker_code.ctypes,
        "CDLL",
        lambda *args, **kwargs: SimpleNamespace(
            setns=lambda fd, kind: calls.append(("setns", fd, kind)) or 0
        ),
    )
    monkeypatch.setattr(
        worker_code.os, "open", lambda path, flags: 20 if path.endswith("mnt") else 21
    )
    for method in ("fchdir", "chroot", "chdir", "close"):
        monkeypatch.setattr(
            worker_code.os, method, lambda value, method=method: calls.append((method, value))
        )
    worker_code._enter_container(123)
    assert calls[:4] == [
        ("setns", 20, 0),
        ("fchdir", 21),
        ("chroot", "."),
        ("chdir", "/"),
    ]
    assert sorted(calls[4:]) == [("close", 20), ("close", 21)]


@pytest.mark.asyncio
@pytest.mark.parametrize("collision", [None, "writable_first", "read_only_first", "workspace"])
async def test_create_binds_before_returning_the_session(
    service: Any, monkeypatch: pytest.MonkeyPatch, collision: str | None
) -> None:
    manager, container, worker = service
    monkeypatch.setattr(container, "start", lambda: container.events.append("start"), raising=False)
    remove = Mock()
    monkeypatch.setattr(container, "remove", remove, raising=False)
    from agents.sandbox.sandboxes.docker import DockerSandboxClientOptions

    client = DockerSandboxClient(manager.docker_client, removal_service=manager)
    configured = manifest()
    if collision is not None:
        worker.aliases["/grant-alias"] = "/workspace" if collision == "workspace" else "/external"
        if collision == "read_only_first":
            configured = configured.model_copy(
                update={"extra_path_grants": tuple(reversed(configured.extra_path_grants))}
            )

    async def create_container(*args: Any, **kwargs: Any) -> RecordingContainer:
        return container

    monkeypatch.setattr(client, "_create_container", create_container)
    if collision is not None:
        with pytest.raises(ValueError, match="distinct canonical workspace and grant roots"):
            await client.create(
                manifest=configured, options=DockerSandboxClientOptions(image="trusted-image")
            )
        assert manager._bindings == {}
        assert [call["operation"] for call in worker.calls] == ["bind"]
        assert worker.removed == []
        assert container.events == ["start", "pause", "close", "unpause"]
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


@pytest.mark.parametrize("accessible", [False, True])
@pytest.mark.parametrize("exists", [False, True])
def test_worker_protocol_keeps_original_traversal_for_user_scoped_removal(
    accessible: bool, exists: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exercise the protocol in memory; namespaces, credentials and syscalls are doubles.
    original = "/workspace/private/link/build"
    canonical = "/workspace/shared/build"
    requests = [
        {"operation": "bind", "paths": ["/workspace"]},
        {"operation": "inspect", "path": original},
        {
            "operation": "remove",
            "user": "1000:1000",
            "max_entry_visits": 100_000,
            "max_cpu_seconds": 10,
        },
    ]
    output = io.StringIO()
    monkeypatch.setattr(worker_code.sys, "argv", ["worker", "123"])
    monkeypatch.setattr(worker_code.sys, "stdin", io.StringIO("\n".join(map(json.dumps, requests))))
    monkeypatch.setattr(worker_code.sys, "stdout", output)
    monkeypatch.setattr(worker_code, "_enter_container", lambda _: None)
    monkeypatch.setattr(
        worker_code,
        "_bind_paths",
        lambda paths: nullcontext(SimpleNamespace(paths=paths, validate=lambda: None)),
    )
    monkeypatch.setattr(worker_code, "_canonical", lambda _: "/workspace/shared")
    current_user = "root"
    removed: list[str] = []

    def lstat(path: str) -> SimpleNamespace:
        if current_user != "root" and path == original and not accessible:
            raise PermissionError("ancestor denies search")
        if not exists:
            raise FileNotFoundError("missing leaf")
        return SimpleNamespace(st_mode=0o040755)

    def remove_as_user(path: str, user: str, **limits: int) -> dict[str, Any]:
        nonlocal current_user
        current_user = user
        worker_code._remove(path, max_entry_visits=100_000)
        return {"ok": True}

    monkeypatch.setattr(worker_code.os, "lstat", lstat)
    monkeypatch.setattr(worker_code.os, "rmdir", removed.append)
    monkeypatch.setattr(worker_code, "_remove_as_user", remove_as_user)
    worker_code.main()
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses[1] == {
        "ok": True,
        "path": canonical if exists else "",
        "is_directory": exists,
    }
    assert responses[2] == (
        {"ok": True} if accessible else {"ok": False, "reason": "PermissionError", "errno": None}
    )
    assert removed == ([original] if accessible and exists else [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "error_number"),
    [("PermissionError", errno.EACCES), ("OSError", errno.E2BIG), ("ValueError", None)],
)
async def test_child_failure_reaches_structured_session_error(
    service: Any, monkeypatch: pytest.MonkeyPatch, reason: str, error_number: int | None
) -> None:
    manager, container, _ = service
    configured = manifest()
    requests = [
        {"operation": "bind", "paths": ["/workspace", "/external", "/grant-alias"]},
        {"operation": "inspect", "path": "/workspace/build"},
        {
            "operation": "remove",
            "user": "developer",
            "max_entry_visits": 100_000,
            "max_cpu_seconds": 10,
        },
    ]
    output = io.StringIO()
    child_response = {"ok": False, "reason": reason, "errno": error_number}
    with monkeypatch.context() as worker_patch:
        worker_patch.setattr(worker_code.sys, "argv", ["worker", "123"])
        worker_patch.setattr(
            worker_code.sys, "stdin", io.StringIO("\n".join(map(json.dumps, requests)))
        )
        worker_patch.setattr(worker_code.sys, "stdout", output)
        worker_patch.setattr(worker_code, "_enter_container", lambda _: None)
        worker_patch.setattr(
            worker_code,
            "_bind_paths",
            lambda paths: nullcontext(SimpleNamespace(paths=paths, validate=lambda: None)),
        )
        worker_patch.setattr(worker_code, "_selected_path", lambda path: (path, True))
        worker_patch.setattr(worker_code.os, "pipe", lambda: (20, 21))
        worker_patch.setattr(worker_code.os, "fork", lambda: 123)
        worker_patch.setattr(
            worker_code.os, "read", lambda fd, size: json.dumps(child_response).encode()
        )
        worker_patch.setattr(worker_code.os, "waitpid", lambda pid, options: (pid, 0))
        worker_patch.setattr(worker_code.os, "close", lambda fd: None)
        worker_code.main()

    worker = object.__new__(_Worker)
    worker.process = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO(output.getvalue()))
    worker.uncertain = False
    monkeypatch.setattr(docker_removal, "_Worker", lambda _: worker)
    manager.bind_new(container, configured)
    with pytest.raises(WorkspaceArchiveWriteError) as caught:
        await session(manager, container, configured).rm("build", recursive=True, user="developer")
    assert caught.value.context["reason"] == "docker_removal_failed"
    assert caught.value.context["worker_reason"] == reason
    assert caught.value.context["errno"] == error_number
    assert isinstance(caught.value.cause, OSError)
    assert caught.value.cause.errno == error_number
    assert not worker.uncertain
    assert not container.attrs["State"]["Paused"]


@pytest.mark.parametrize("path", ["/etc/passwd", "/etc/group"])
@pytest.mark.parametrize("kind", [stat.S_IFCHR, stat.S_IFIFO, stat.S_IFLNK])
def test_account_special_files_are_rejected_before_open(
    path: str, kind: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata = Mock(return_value=SimpleNamespace(st_mode=kind | 0o644))
    open_file = Mock(side_effect=AssertionError("must not open special files"))
    monkeypatch.setattr(worker_code.os, "stat", metadata)
    monkeypatch.setattr(worker_code.os, "open", open_file)
    with pytest.raises(ValueError, match="account_file_requires_regular_file"):
        list(worker_code._accounts(path))
    metadata.assert_called_once_with(path, follow_symlinks=False)
    open_file.assert_not_called()


def test_regular_account_file_is_checked_before_and_after_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def metadata(*args: Any, **kwargs: Any) -> SimpleNamespace:
        events.append("stat")
        return SimpleNamespace(st_mode=stat.S_IFREG | 0o644)

    monkeypatch.setattr(worker_code.os, "stat", metadata)
    monkeypatch.setattr(worker_code.os, "open", lambda *args: events.append("open") or 20)
    monkeypatch.setattr(worker_code.os, "fstat", metadata)
    monkeypatch.setattr(
        worker_code.os,
        "fdopen",
        lambda *args, **kwargs: io.StringIO("developer:x:1000:1000::/home/developer:/bin/sh\n"),
    )
    monkeypatch.setattr(worker_code.os, "close", lambda _: events.append("close"))
    assert list(worker_code._accounts("/etc/passwd")) == [
        ["developer", "x", "1000", "1000", "", "/home/developer", "/bin/sh"]
    ]
    assert events == ["stat", "open", "stat", "close"]


@pytest.mark.parametrize("shape", ["newlines", "fields", "members", "oversized"])
def test_user_lookup_bounds_account_parsing(shape: str, monkeypatch: pytest.MonkeyPatch) -> None:
    class AccountField(str):
        def split(self, sep: str | None = None, maxsplit: int = -1) -> list[str]:
            if maxsplit < 0:
                raise AssertionError("account fields must not expand into unbounded lists")
            return super().split(sep, maxsplit)

    class AccountLine(str):
        def rstrip(self, chars: str | None = None) -> AccountLine:
            return AccountLine(super().rstrip(chars))

        def split(self, sep: str | None = None, maxsplit: int = -1) -> list[str]:
            if not 0 <= maxsplit <= 8:
                raise AssertionError("account records require bounded field splitting")
            return [AccountField(field) for field in super().split(sep, maxsplit)]

    class AccountStream(io.StringIO):
        def read(self, size: int = -1) -> str:
            raise AssertionError("account lookup must stream records")

        def readline(self, size: int | None = -1) -> str:
            assert size is not None and 0 < size <= 1024 * 1024 + 1
            return AccountLine(super().readline(size))

    user_record = "root:x:0:0::/root:/bin/sh\n"
    group_record = "tools:x:2:root\n"
    if shape == "newlines":
        passwd = "\n" * (1024 * 1024 - len(user_record)) + user_record
        groups = "\n" * (1024 * 1024 - len(group_record)) + group_record
    elif shape == "fields":
        passwd = ":" * (1024 * 1024)
        groups = passwd
    elif shape == "members":
        passwd = user_record
        groups = "tools:x:2:" + "," * (1024 * 1024 - 15) + "root\n"
    else:
        passwd = user_record + "\n" * (1024 * 1024)
        groups = ""
    streams = {20: AccountStream(passwd), 21: AccountStream(groups)}
    closed: list[int] = []
    metadata = SimpleNamespace(st_mode=stat.S_IFREG | 0o644)
    monkeypatch.setattr(
        worker_code,
        "os",
        SimpleNamespace(
            O_RDONLY=0,
            O_NOFOLLOW=1,
            O_NONBLOCK=2,
            stat=lambda *args, **kwargs: metadata,
            fstat=lambda fd: metadata,
            open=lambda path, flags: 20 if path == "/etc/passwd" else 21,
            fdopen=lambda fd, *args, **kwargs: streams[fd],
            close=closed.append,
        ),
    )
    if shape == "oversized":
        with pytest.raises(ValueError, match="account_file_too_large"):
            worker_code._user_ids("0")
        assert closed == [20]
    else:
        identity = worker_code._user_ids("0")
        assert identity == (0, 0, [] if shape == "fields" else [2])
        assert closed == [20, 21]
        assert streams[21].closed
    assert streams[20].closed


def test_account_stream_closes_on_group_conversion_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []

    def accounts(path: str) -> Iterator[list[str]]:
        try:
            if path == "/etc/passwd":
                yield ["developer", "x", "1000", "1000", "", "/home/developer", "/bin/sh"]
            else:
                yield ["tools", "x", "invalid", "developer"]
                pytest.fail("lookup must stop after conversion failure")
        finally:
            closed.append(path)

    monkeypatch.setattr(worker_code, "_accounts", accounts)
    with pytest.raises(ValueError):
        worker_code._user_ids("developer")
    assert closed == ["/etc/passwd", "/etc/group"]


@pytest.mark.asyncio
@pytest.mark.parametrize("restore", [False, True])
async def test_workspace_overridden_grant_does_not_invalidate_later_removal(
    service: Any, restore: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, container, worker = service
    configured = Manifest(
        root="/workspace",
        extra_path_grants=(
            SandboxPathGrant(path="/workspace/cache", read_only=True),
            SandboxPathGrant(path="/external/protected", read_only=True),
        ),
    )
    manager.bind_new(container, configured)
    current = session(manager, container, configured)
    bindings = object.__new__(worker_code._Bindings)
    bindings.paths = ["/workspace", "/workspace/cache", "/external/protected"]
    bindings.fds = [20, 21, 22]
    originals = {
        path: SimpleNamespace(st_dev=1, st_ino=index)
        for index, path in enumerate([*bindings.paths, "/workspace/build"])
    }
    remaining = dict(originals)
    pinned = dict(zip(bindings.fds, originals.values(), strict=False))

    def metadata(path: str, **kwargs: Any) -> SimpleNamespace:
        if path not in remaining:
            raise FileNotFoundError(path)
        return remaining[path]

    monkeypatch.setattr(worker_code.os, "stat", metadata)
    monkeypatch.setattr(worker_code.os, "fstat", pinned.__getitem__)
    monkeypatch.setattr(worker_code, "_canonical", lambda path: path)
    original_request = worker.request

    def request(**data: Any) -> dict[str, Any]:
        if data["operation"] == "inspect":
            try:
                bindings.validate()
            except Exception as exc:
                raise RuntimeError(type(exc).__name__) from None
        result = original_request(**data)
        if data["operation"] == "remove":
            remaining.pop(worker.selected)
        return result

    monkeypatch.setattr(worker, "request", request)
    if restore:

        async def listing(_: Path) -> list[FileEntry]:
            return [
                FileEntry(
                    path=path,
                    kind=EntryKind.DIRECTORY,
                    permissions=Permissions(directory=True),
                    owner="0",
                    group="0",
                    size=0,
                )
                for path in ("/workspace/cache", "/workspace/build")
            ]

        monkeypatch.setattr(current, "ls", listing)
        await current._clear_workspace_dir_on_resume_pruned(
            current_dir=Path("/workspace"), skip_rel_paths=set()
        )
    else:
        await current.rm("cache", recursive=True)
        await current.rm("build", recursive=True)
    assert worker.removed == ["/workspace/cache", "/workspace/build"]
    assert set(remaining) == {"/workspace", "/external/protected"}

    # Effective external protection still fails closed if its identity changes.
    remaining["/external/protected"] = SimpleNamespace(st_dev=1, st_ino=100)
    with pytest.raises(WorkspaceArchiveWriteError):
        await current.rm("another-build", recursive=True)
    assert worker.removed == ["/workspace/cache", "/workspace/build"]


@pytest.mark.parametrize("failure", ["open", "register", "fstat", "none"])
def test_bound_descriptors_are_closed_after_partial_or_normal_lifetime(
    failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[int] = []
    metadata = SimpleNamespace(st_dev=1, st_ino=1, st_mode=stat.S_IFDIR | 0o755)
    monkeypatch.setattr(worker_code.os, "O_PATH", 0, raising=False)
    monkeypatch.setattr(worker_code, "_canonical", lambda path: path)
    monkeypatch.setattr(worker_code.os, "stat", lambda path: metadata)
    monkeypatch.setattr(worker_code.os, "close", closed.append)
    if failure == "register":
        callback = worker_code.ExitStack.callback

        def register(stack: Any, close: Any, fd: int) -> Any:
            if fd == 21:
                raise OSError("registration failed")
            return callback(stack, close, fd)

        monkeypatch.setattr(worker_code.ExitStack, "callback", register)
    monkeypatch.setattr(
        worker_code.os,
        "open",
        Mock(side_effect=[20, OSError("open failed") if failure == "open" else 21]),
    )
    monkeypatch.setattr(
        worker_code.os,
        "fstat",
        Mock(
            side_effect=[
                metadata,
                OSError("fstat failed") if failure == "fstat" else metadata,
                metadata,
            ]
        ),
    )
    if failure == "none":
        with worker_code._bind_paths(["/workspace", "/protected"]) as bindings:
            assert bindings.paths == ["/workspace", "/protected"]
            assert closed == []
    else:
        with pytest.raises(OSError):
            with worker_code._bind_paths(["/workspace", "/protected"]):
                pytest.fail("binding must fail")
    assert closed == ([20] if failure == "open" else [21, 20])


@pytest.mark.parametrize(
    ("failure", "limit_failure"),
    [
        (None, None),
        (PermissionError(13, "denied"), None),
        (OSError(errno.E2BIG, "removal_entry_limit"), None),
        (ValueError("unknown_user"), None),
        (KeyboardInterrupt(), None),
        (None, PermissionError(1, "limit denied")),
    ],
)
def test_removal_child_always_exits_without_resuming_parent(
    failure: BaseException | None, limit_failure: OSError | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ChildExited(BaseException):
        pass

    writes: list[dict[str, Any]] = []
    exit_codes: list[int] = []

    def exit_child(code: int) -> None:
        exit_codes.append(code)
        raise ChildExited

    user_ids = Mock(
        return_value=(1000, 1000, []),
        side_effect=failure if isinstance(failure, ValueError) else None,
    )
    monkeypatch.setattr(worker_code, "_user_ids", user_ids)
    monkeypatch.setattr(worker_code.os, "pipe", lambda: (20, 21))
    monkeypatch.setattr(worker_code.os, "fork", lambda: 0)
    set_limit = Mock(side_effect=limit_failure)
    monkeypatch.setattr(worker_code.resource, "setrlimit", set_limit)
    monkeypatch.setattr(worker_code.os, "close", lambda fd: None)
    for name in ("setgroups", "setgid", "setuid"):
        monkeypatch.setattr(worker_code.os, name, lambda value: None)
    monkeypatch.setattr(worker_code, "_remove", Mock(side_effect=failure))
    monkeypatch.setattr(worker_code.os, "write", lambda fd, data: writes.append(json.loads(data)))
    monkeypatch.setattr(worker_code.os, "_exit", exit_child)
    with pytest.raises(ChildExited):
        worker_code._remove_as_user(
            "/workspace/build", "1000:1000", max_entry_visits=100_000, max_cpu_seconds=10
        )
    set_limit.assert_called_once_with(worker_code.resource.RLIMIT_CPU, (10, 10))
    if limit_failure is not None:
        user_ids.assert_not_called()
    failure = limit_failure or failure
    assert exit_codes == ([1] if isinstance(failure, KeyboardInterrupt) else [0])
    assert writes == (
        []
        if isinstance(failure, KeyboardInterrupt)
        else [
            {
                "ok": False,
                "reason": type(failure).__name__,
                "errno": getattr(failure, "errno", None),
            }
        ]
        if failure
        else [{"ok": True}]
    )


def test_worker_closes_bindings_when_response_write_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    close = Mock()
    monkeypatch.setattr(worker_code.sys, "argv", ["worker", "123"])
    monkeypatch.setattr(
        worker_code.sys, "stdin", io.StringIO('{"operation":"bind","paths":["/workspace"]}\n')
    )
    monkeypatch.setattr(worker_code.sys, "stdout", Mock(write=Mock(side_effect=BrokenPipeError)))
    monkeypatch.setattr(worker_code, "_enter_container", lambda pid: None)

    @contextmanager
    def bind_paths(paths: list[str]) -> Iterator[Any]:
        try:
            yield SimpleNamespace(paths=paths)
        finally:
            close()

    monkeypatch.setattr(worker_code, "_bind_paths", bind_paths)
    with pytest.raises(BrokenPipeError):
        worker_code.main()
    close.assert_called_once_with()


def test_namespace_entry_closes_mount_handle_when_root_open_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []
    enter = Mock()
    monkeypatch.setattr(
        worker_code.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(setns=enter)
    )
    monkeypatch.setattr(
        worker_code.os, "open", Mock(side_effect=[20, PermissionError("root denied")])
    )
    monkeypatch.setattr(worker_code.os, "close", closed.append)
    with pytest.raises(PermissionError):
        worker_code._enter_container(123)
    assert closed == [20]
    enter.assert_not_called()


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


def test_worker_removes_deep_tree_without_python_recursion_or_open_directory_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = ["/workspace/build" + "/d" * depth for depth in range(1051)]
    children = dict(zip(paths, paths[1:], strict=False))
    remaining = set(paths)
    removed: list[str] = []
    open_scans = 0

    def remove_directory(path: str) -> None:
        assert open_scans <= 1
        if children.get(path) in remaining:
            raise OSError(errno.ENOTEMPTY, "not empty")
        remaining.remove(path)
        removed.append(path)

    class Scan:
        def __init__(self, path: str) -> None:
            self.path = path

        def __enter__(self) -> Any:
            nonlocal open_scans
            open_scans += 1
            assert open_scans == 1
            return iter([SimpleNamespace(path=children[self.path])])

        def __exit__(self, *args: Any) -> None:
            nonlocal open_scans
            open_scans -= 1

    monkeypatch.setattr(worker_code.os, "lstat", lambda path: SimpleNamespace(st_mode=stat.S_IFDIR))
    monkeypatch.setattr(worker_code.os, "rmdir", remove_directory)
    monkeypatch.setattr(worker_code.os, "scandir", Scan)
    monkeypatch.setattr(
        worker_code.os, "unlink", Mock(side_effect=AssertionError("directories only"))
    )
    worker_code._remove(paths[0], max_entry_visits=100_000)
    assert remaining == set()
    assert removed == paths[::-1]
    assert open_scans == 0


def test_bind_preserves_request_failure_when_worker_cleanup_fails(service: Any) -> None:
    manager, container, worker = service
    primary = RuntimeError("worker transport failed")
    worker.request = Mock(side_effect=primary)
    worker.close = Mock(side_effect=BrokenPipeError("cleanup failed"))
    with pytest.raises(RuntimeError) as caught:
        manager.bind_new(container, manifest())
    assert caught.value is primary
    worker.close.assert_called_once_with()
    assert manager._bindings == {}


def test_worker_streams_wide_directory_without_buffering_sibling_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    count = 10000
    deleted = 0
    root_removed = False

    def lstat(path: str) -> SimpleNamespace:
        return SimpleNamespace(st_mode=stat.S_IFDIR if path == "/tree" else stat.S_IFREG)

    def rmdir(path: str) -> None:
        nonlocal root_removed
        if deleted != count:
            raise OSError(errno.ENOTEMPTY, "not empty")
        root_removed = True

    def unlink(path: str) -> None:
        nonlocal deleted
        assert path == f"/tree/{deleted}"
        deleted += 1

    def entries() -> Iterator[SimpleNamespace]:
        for index in range(count):
            assert deleted == index, "each leaf must be consumed before fetching the next"
            yield SimpleNamespace(path=f"/tree/{index}")

    monkeypatch.setattr(worker_code.os, "lstat", lstat)
    monkeypatch.setattr(worker_code.os, "rmdir", rmdir)
    monkeypatch.setattr(worker_code.os, "unlink", unlink)
    monkeypatch.setattr(worker_code.os, "scandir", lambda path: nullcontext(entries()))
    worker_code._remove("/tree", max_entry_visits=100_000)
    assert root_removed
    assert deleted == count


def test_worker_stops_entry_visits_before_additional_filesystem_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visited: list[str] = []
    scans_closed: list[bool] = []

    def remove_leaf(path: str) -> bool:
        visited.append(path)
        return path != "/tree"

    @contextmanager
    def scan(path: str) -> Iterator[Any]:
        try:
            yield (SimpleNamespace(path=f"/tree/{index}") for index in range(100))
        finally:
            scans_closed.append(True)

    monkeypatch.setattr(worker_code, "_remove_leaf", remove_leaf)
    monkeypatch.setattr(worker_code.os, "scandir", scan)
    with pytest.raises(OSError) as caught:
        worker_code._remove("/tree", max_entry_visits=3)
    assert caught.value.errno == errno.E2BIG
    assert visited == ["/tree", "/tree/0", "/tree/1"]
    assert scans_closed == [True]


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


@pytest.mark.parametrize("outcome", ["allowed", "search_denied", "repointed"])
def test_worker_preserves_workspace_alias_permissions_and_bound_identity(
    outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alias = "/private/workspace-alias"
    original = alias + "/build"
    requests = [
        {"operation": "bind", "paths": [alias]},
        {"operation": "inspect", "path": original, "workspace_root": alias},
        {
            "operation": "remove",
            "user": "developer",
            "max_entry_visits": 100_000,
            "max_cpu_seconds": 10,
        },
    ]
    output = io.StringIO()
    monkeypatch.setattr(worker_code.sys, "argv", ["worker", "123"])
    monkeypatch.setattr(worker_code.sys, "stdin", io.StringIO("\n".join(map(json.dumps, requests))))
    monkeypatch.setattr(worker_code.sys, "stdout", output)
    monkeypatch.setattr(worker_code, "_enter_container", lambda pid: None)
    monkeypatch.setattr(
        worker_code,
        "_bind_paths",
        lambda paths: nullcontext(SimpleNamespace(paths=["/canonical"], validate=lambda: None)),
    )
    monkeypatch.setattr(
        worker_code,
        "_canonical",
        lambda path: "/different" if outcome == "repointed" else "/canonical",
    )
    current_user = "root"
    removed: list[str] = []

    def metadata(path: str) -> SimpleNamespace:
        if current_user == "developer" and path.startswith(alias) and outcome == "search_denied":
            raise PermissionError("workspace alias ancestor denies search")
        return SimpleNamespace(st_mode=stat.S_IFDIR)

    def remove_as_user(path: str, user: str, **limits: int) -> dict[str, Any]:
        nonlocal current_user
        current_user = user
        worker_code._remove(path, max_entry_visits=100_000)
        return {"ok": True}

    monkeypatch.setattr(worker_code.os, "lstat", metadata)
    monkeypatch.setattr(worker_code.os, "rmdir", removed.append)
    monkeypatch.setattr(worker_code, "_remove_as_user", remove_as_user)
    worker_code.main()
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    if outcome == "repointed":
        assert responses[1] == {"ok": False, "reason": "ValueError", "errno": None}
        assert responses[2] == {"ok": False, "reason": "ValueError", "errno": None}
    elif outcome == "search_denied":
        assert responses[1]["path"] == "/canonical/build"
        assert responses[2] == {"ok": False, "reason": "PermissionError", "errno": None}
    else:
        assert responses[1]["path"] == "/canonical/build"
        assert responses[2] == {"ok": True}
    assert removed == ([original] if outcome == "allowed" else [])


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
