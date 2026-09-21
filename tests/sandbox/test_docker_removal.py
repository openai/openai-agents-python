"""Host removal tests use recording workers; no Docker or filesystem mutations run."""

from __future__ import annotations

import asyncio
import io
import json
import stat
import threading
import uuid
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from agents.sandbox import Manifest, Permissions, SandboxPathGrant, User
from agents.sandbox.errors import InvalidManifestPathError, WorkspaceArchiveWriteError
from agents.sandbox.files import EntryKind, FileEntry
from agents.sandbox.sandboxes import (
    DockerRemovalService,
    _docker_removal_worker as worker_code,
    docker_removal,
)
from agents.sandbox.sandboxes.docker import (
    DockerSandboxClient,
    DockerSandboxSession,
    DockerSandboxSessionState,
    _finish_host_removal_call,
)
from agents.sandbox.snapshot import NoopSnapshot


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
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[DockerRemovalService, RecordingContainer, RecordingWorker]:
    instance = object.__new__(DockerRemovalService)
    instance.docker_client = Mock()
    instance._lock = threading.RLock()
    instance._bindings = {}
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
    assert worker.calls[-1] == {"operation": "remove", "user": "developer"}
    assert container.events == ["pause", "unpause", "pause", "unpause"]


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
async def test_repeated_cancellation_waits_for_actual_host_completion() -> None:
    started = threading.Event()
    finish = threading.Event()
    completed: list[str] = []

    def operation() -> None:
        started.set()
        assert finish.wait(5)
        completed.append("finished")

    task = asyncio.create_task(_finish_host_removal_call(operation))
    try:
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
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
    worker_code._remove("/workspace/empty")
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
        ("1000:3000", (1000, 3000, [])),
        ("developer:tools", (1000, 2000, [])),
    ],
)
def test_requested_user_and_groups_are_preserved(
    user: str, expected: tuple[int, int, list[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts = {
        "/etc/passwd": [["developer", "x", "1000", "1001", "", "/home/developer", "/bin/sh"]],
        "/etc/group": [["tools", "x", "2000", "developer"]],
    }
    monkeypatch.setattr(worker_code, "_accounts", accounts.__getitem__)
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
    assert calls == [
        ("setns", 20, 0),
        ("fchdir", 21),
        ("chroot", "."),
        ("chdir", "/"),
        ("close", 20),
        ("close", 21),
    ]


@pytest.mark.asyncio
async def test_create_binds_before_returning_the_session(
    service: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, container, worker = service
    monkeypatch.setattr(container, "start", lambda: container.events.append("start"), raising=False)
    from agents.sandbox.sandboxes.docker import DockerSandboxClientOptions

    client = DockerSandboxClient(manager.docker_client, removal_service=manager)

    async def create_container(*args: Any, **kwargs: Any) -> RecordingContainer:
        return container

    monkeypatch.setattr(client, "_create_container", create_container)
    wrapped = await client.create(
        manifest=manifest(), options=DockerSandboxClientOptions(image="trusted-image")
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
    assert worker.calls[-2] == {"operation": "inspect", "path": "/workspace/build"}
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
        {"operation": "inspect", "path": "/workspace/private/missing"},
        {"operation": "remove", "user": "developer"},
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
        {"operation": "remove", "user": "1000:1000"},
    ]
    output = io.StringIO()
    monkeypatch.setattr(worker_code.sys, "argv", ["worker", "123"])
    monkeypatch.setattr(worker_code.sys, "stdin", io.StringIO("\n".join(map(json.dumps, requests))))
    monkeypatch.setattr(worker_code.sys, "stdout", output)
    monkeypatch.setattr(worker_code, "_enter_container", lambda _: None)
    monkeypatch.setattr(
        worker_code,
        "_Bindings",
        lambda paths: SimpleNamespace(paths=paths, validate=lambda: None, close=lambda: None),
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

    def remove_as_user(path: str, user: str) -> None:
        nonlocal current_user
        current_user = user
        worker_code._remove(path)

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
        {"ok": True} if accessible else {"ok": False, "reason": "PermissionError"}
    )
    assert removed == ([original] if accessible and exists else [])


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
        worker_code._accounts(path)
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
    assert worker_code._accounts("/etc/passwd") == [
        ["developer", "x", "1000", "1000", "", "/home/developer", "/bin/sh"]
    ]
    assert events == ["stat", "open", "stat", "close"]


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
