"""Opt-in, host-side removal for private, rootful Linux Docker containers.

Run this service in a trusted process on the Docker daemon host, as root. It uses
host Python workers, not binaries supplied by the container. It does not listen on
a network socket. Give its Docker client only to trusted application code.

Only Docker sessions explicitly configured with this service use its removal
authority. Other backends and Docker clients without the service retain their
existing recursive removal and snapshot restoration behavior.

The service requires a trusted image, Docker 26+ with its builtin seccomp profile,
and the runc runtime. Workspaces and path-only grant roots must exist in the image.
Read-only host bind mounts are supported outside the private workspace. Writable
shared mounts, additional capabilities, user namespaces, and missing roots are excluded.
The application must exclusively own container lifecycle and Docker API access;
other host administrators are trusted. A service/worker transport failure leaves
the container paused. Before manually resuming it, stop all service workers.

Use one service for the client's lifetime and close it after deleting its sessions.
Binding is live authority and is never serialized. Reattaching a running container
requires the same live binding; a new service must create a new container.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from docker import DockerClient  # type: ignore[import-untyped]
from docker.models.containers import Container  # type: ignore[import-untyped]

from ..errors import WorkspaceArchiveWriteError
from ..manifest import Manifest
from ..workspace_paths import (
    WorkspacePathPolicy,
    coerce_posix_path,
    posix_path_for_error,
    sandbox_path_grant_host_path,
)


class _Worker:
    def __init__(self, pid: int) -> None:
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-S",
                str(Path(__file__).with_name("_docker_removal_worker.py")),
                str(pid),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            env={"LC_ALL": "C.UTF-8"},
            start_new_session=True,
        )
        self.uncertain = False

    def request(self, **request: Any) -> dict[str, Any]:
        assert self.process.stdin is not None and self.process.stdout is not None
        try:
            self.process.stdin.write(json.dumps(request) + "\n")
            self.process.stdin.flush()
            response = json.loads(self.process.stdout.readline())
        except Exception as exc:
            self.uncertain = True
            raise RuntimeError("Docker removal worker transport failed") from exc
        if not response["ok"]:
            raise OSError(response["errno"], response["reason"])
        return cast(dict[str, Any], response)

    def close(self) -> None:
        operations: list[Callable[[], object]] = []
        if self.process.stdin is not None:
            operations.append(self.process.stdin.close)
        operations.append(self.process.wait)
        if self.process.stdout is not None:
            operations.append(self.process.stdout.close)
        error: Exception | None = None
        for operation in operations:
            try:
                operation()
            except Exception as exc:
                if error is None:
                    error = exc
        if error is not None:
            raise error


def _configuration(manifest: Manifest) -> tuple[str, tuple[tuple[str, bool, str | None], ...]]:
    return manifest.root, tuple(
        (
            grant.path,
            grant.read_only,
            str(sandbox_path_grant_host_path(grant)) if grant.host_path is not None else None,
        )
        for grant in manifest.extra_path_grants
    )


@dataclass
class _Binding:
    worker: _Worker
    incarnation: tuple[int, str]
    configuration: tuple[str, tuple[tuple[str, bool, str | None], ...]]
    policy: WorkspacePathPolicy
    lock: threading.RLock = field(default_factory=threading.RLock)

    def close(self) -> None:
        with self.lock:
            self.worker.close()


class DockerRemovalService:
    """Own fixed grant bindings and paused-workload removal on a local Docker host.

    Construct ``DockerSandboxClient(service.docker_client, removal_service=service)``.
    The service must not be shared with untrusted callers or exposed to containers.
    Only newly created, private containers can acquire a binding. Replacing a bound
    canonical root invalidates it; changing its original symlink alias does not.
    Workspace and grant roots must resolve to distinct canonical paths.
    Recursive removal rejects the workspace root and effective external grant roots,
    including their ancestors. Remove their children to clear them instead.

    Create the service on the daemon host, then pass its connection and live service
    to the client::

        service = DockerRemovalService(
            max_concurrent_removals=4, max_entry_visits=100_000, max_cpu_seconds=10
        )
        client = DockerSandboxClient(service.docker_client, removal_service=service)

    Use the normal ``client.create`` and ``session.start`` lifecycle. After use, await
    ``client.delete(session)`` before calling ``service.close()``. Each removal pauses
    all workload processes until it finishes, which can affect concurrent command
    deadlines. Stopped/restarted containers require a fresh session; a binding must
    not be reconstructed from saved paths. Read-only host bind mounts outside the
    private workspace are supported; writable shared mounts and custom security
    profiles are unsupported. Other host processes that can modify mount sources
    must be trusted; pausing the container does not pause host processes.

    Choose resource limits for the host and share this service across the managed
    clients. Calls beyond the concurrency limit are rejected before pausing.
    Each removal child has a CPU-time limit; entry visits include repeated
    visits to ancestors. Reaching a limit may leave a partially removed tree. These
    limits do not impose a wall-clock deadline on stalled kernel I/O. An uncertain
    worker outcome consumes a concurrency slot until all workers have been stopped
    and the application replaces the service.
    """

    docker_client: DockerClient
    _lock: threading.RLock
    _bindings: dict[str, _Binding]
    _closed: bool
    _removal_slots: threading.BoundedSemaphore
    _max_entry_visits: int
    _max_cpu_seconds: int

    def __init__(
        self,
        *,
        max_concurrent_removals: int,
        max_entry_visits: int,
        max_cpu_seconds: int,
        socket_path: str = "/var/run/docker.sock",
    ) -> None:
        if sys.platform != "linux" or os.geteuid() != 0:
            raise RuntimeError("DockerRemovalService requires root on the Linux Docker host")
        if min(max_concurrent_removals, max_entry_visits, max_cpu_seconds) <= 0:
            raise ValueError("Docker removal resource limits must be positive")
        self.docker_client = DockerClient(base_url=f"unix://{socket_path}")
        self._lock = threading.RLock()
        self._bindings = {}
        self._closed = False
        self._removal_slots = threading.BoundedSemaphore(max_concurrent_removals)
        self._max_entry_visits = max_entry_visits
        self._max_cpu_seconds = max_cpu_seconds

    def _state(self, container: Container) -> tuple[int, str]:
        container.reload()
        attrs = container.attrs
        host = attrs["HostConfig"]
        state = attrs["State"]
        daemon = self.docker_client.info()
        security = daemon.get("SecurityOptions", [])
        # Kernel-submitted namespace operations must not outlive the paused tasks.
        # Docker's builtin profile blocks io_uring; custom profiles are not equivalent.
        if (
            int(self.docker_client.version()["Version"].split(".")[0]) < 26
            or "name=seccomp,profile=builtin" not in security
            or "name=selinux" in security
            or daemon.get("DefaultRuntime") != "runc"
        ):
            raise ValueError("Docker removal requires Docker 26+ with builtin seccomp and runc")
        if (
            any(
                mount.get("Type") != "bind"
                or mount.get("RW") is not False
                or mount.get("Propagation") != "rprivate"
                for mount in attrs.get("Mounts", [])
            )
            or host.get("Privileged")
            or host.get("CapAdd")
            or host.get("CapDrop")
            or host.get("GroupAdd")
            or host.get("SecurityOpt")
            or host.get("Runtime") not in (None, "", "runc")
            or host.get("PidMode") not in (None, "")
            or host.get("UsernsMode") not in (None, "", "host")
            or not state["Running"]
            or state.get("Restarting")
        ):
            raise ValueError("Docker removal service requires a running private container")
        pid = int(state["Pid"])
        identity = container.id
        if not identity or not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise ValueError("Docker removal service requires a complete container ID")
        cgroups = Path(f"/proc/{pid}/cgroup").read_text().splitlines()
        if not any(
            line.endswith(f"/docker/{identity}") or line.endswith(f"/docker-{identity}.scope")
            for line in cgroups
        ):
            raise ValueError("Docker container is not on this service's host")
        for name in ("uid_map", "gid_map"):
            mapping = Path(f"/proc/{pid}/{name}").read_text().split()
            if mapping != ["0", "0", "4294967295"]:
                raise ValueError("Docker removal service does not support remapped users")
        return pid, str(state["StartedAt"])

    @contextmanager
    def _paused(
        self, container: Container, worker_uncertain: Callable[[], bool]
    ) -> Iterator[tuple[int, str]]:
        incarnation = self._state(container)
        already_paused = bool(container.attrs["State"]["Paused"])
        if not already_paused:
            container.pause()
        # The Docker pause request completes before exec or filesystem work begins.
        if self._state(container) != incarnation or not container.attrs["State"]["Paused"]:
            raise RuntimeError("Docker container changed while pausing; left paused")
        completed = False
        try:
            yield incarnation
            completed = True
        finally:
            primary_error = None if completed else sys.exc_info()[1]
            if not already_paused and not worker_uncertain():
                try:
                    if self._state(container) == incarnation and container.attrs["State"]["Paused"]:
                        container.unpause()
                except Exception as cleanup_error:
                    if primary_error is None:
                        raise
                    # Keep the primary cause and avoid a context cycle back to that error.
                    cleanup_error.__context__ = primary_error.__context__
                    primary_error.__context__ = cleanup_error

    def bind_new(self, container: Container, manifest: Manifest) -> None:
        """Bind before a newly created session is returned to its trusted application."""
        with self._lock:
            if self._closed:
                raise ValueError("Docker removal service is closed")
            if container.id in self._bindings:
                raise ValueError("Docker removal authority is already bound")
        if any(
            grant.host_path is not None and not grant.read_only
            for grant in manifest.extra_path_grants
        ):
            raise ValueError("Docker removal service does not support writable shared host paths")
        # Reuse the Docker client's mount authority checks without a module import cycle.
        from .docker import (
            _assert_existing_container_path_grants_match,
            _validate_docker_path_grants,
        )

        _validate_docker_path_grants(manifest)
        _assert_existing_container_path_grants_match(container, manifest)
        worker: _Worker | None = None
        with self._paused(
            container, lambda: worker is not None and worker.uncertain
        ) as incarnation:
            worker = _Worker(incarnation[0])
            try:
                result = worker.request(
                    operation="bind",
                    paths=[manifest.root, *(grant.path for grant in manifest.extra_path_grants)],
                )
                paths = result["paths"]
                root = coerce_posix_path(paths[0])
                for grant, path in zip(manifest.extra_path_grants, paths[1:], strict=True):
                    mounted = coerce_posix_path(path)
                    if grant.host_path is not None and (
                        mounted.is_relative_to(root) or root.is_relative_to(mounted)
                    ):
                        raise ValueError(
                            "Docker removal requires host mounts outside the workspace"
                        )
                if len(set(paths)) != len(paths):
                    raise ValueError(
                        "Docker removal requires distinct canonical workspace and grant roots"
                    )
                policy = WorkspacePathPolicy(
                    root=paths[0],
                    extra_path_grants=tuple(
                        grant.model_copy(update={"path": path})
                        for grant, path in zip(manifest.extra_path_grants, paths[1:], strict=True)
                    ),
                )
                with self._lock:
                    if self._closed or container.id in self._bindings:
                        raise ValueError("Docker removal authority cannot be registered")
                    self._bindings[container.id] = _Binding(
                        worker, incarnation, _configuration(manifest), policy
                    )
            except BaseException:
                with suppress(Exception):
                    worker.close()
                raise

    @contextmanager
    def _bound(self, container: Container, manifest: Manifest) -> Iterator[_Binding]:
        with self._lock:
            binding = self._bindings.get(container.id)
        if binding is None:
            raise ValueError("Docker removal requires the original live authority binding")
        with binding.lock:
            with self._lock:
                current = self._bindings.get(container.id)
            if current is not binding or binding.configuration != _configuration(manifest):
                raise ValueError("Docker removal requires the original live authority binding")
            if binding.worker.uncertain or self._state(container) != binding.incarnation:
                raise ValueError("Docker removal authority is no longer usable")
            yield binding

    def assert_bound(self, container: Container, manifest: Manifest) -> None:
        with self._bound(container, manifest):
            pass

    def remove(
        self, container: Container, manifest: Manifest, path: Path | str, user: str | None
    ) -> None:
        """Authorize and remove while the workload is paused; never use container exec."""
        with self._bound(container, manifest) as binding:
            original_policy = WorkspacePathPolicy(
                root=manifest.root, extra_path_grants=manifest.extra_path_grants
            )
            original = original_policy.normalize_sandbox_path(path)
            if not self._removal_slots.acquire(blocking=False):
                raise WorkspaceArchiveWriteError(
                    path=posix_path_for_error(original),
                    context={"reason": "docker_removal_capacity"},
                )
            try:
                with self._paused(container, lambda: binding.worker.uncertain):
                    try:
                        inspection = binding.worker.request(
                            operation="inspect",
                            path=original.as_posix(),
                            workspace_root=manifest.root
                            if not coerce_posix_path(path).is_absolute()
                            else None,
                        )
                        target = inspection["path"]
                        if target:
                            selected = coerce_posix_path(target)
                            root = binding.policy.normalize_sandbox_path(".")
                            live_roots = (
                                root,
                                *(
                                    grant
                                    for grant, _ in binding.policy.extra_path_grant_rules()
                                    if not grant.is_relative_to(root)
                                ),
                            )
                            if any(
                                selected == bound_root
                                or (
                                    inspection["is_directory"]
                                    and bound_root.is_relative_to(selected)
                                )
                                for bound_root in live_roots
                            ):
                                raise WorkspaceArchiveWriteError(
                                    path=posix_path_for_error(original),
                                    context={"reason": "docker_removal_bound_root"},
                                )
                            if inspection["is_directory"]:
                                binding.policy.validate_recursive_remove(target)
                            else:
                                binding.policy.normalize_sandbox_path(target, for_write=True)
                        docker_user = user or container.attrs.get("Config", {}).get("User") or "0"
                        binding.worker.request(
                            operation="remove",
                            user=docker_user,
                            max_entry_visits=self._max_entry_visits,
                            max_cpu_seconds=self._max_cpu_seconds,
                        )
                    except OSError as exc:
                        raise WorkspaceArchiveWriteError(
                            path=posix_path_for_error(original),
                            context={
                                "reason": "docker_removal_failed",
                                "worker_reason": exc.strerror,
                                "errno": exc.errno,
                            },
                            cause=exc,
                        ) from exc
                    except RuntimeError as exc:
                        raise WorkspaceArchiveWriteError(
                            path=posix_path_for_error(original),
                            context={"reason": "docker_removal_failed"},
                            cause=exc,
                        ) from exc
            finally:
                if not binding.worker.uncertain:
                    self._removal_slots.release()

    def release(self, container_id: str) -> None:
        """Release authority after its container has been deleted."""
        with self._lock:
            binding = self._bindings.pop(container_id, None)
        if binding is not None:
            binding.close()

    def close(self) -> None:
        """Release workers; an uncertain operation does not automatically thaw its container."""
        with self._lock:
            self._closed = True
            bindings = tuple(self._bindings.values())
            self._bindings.clear()
        error: Exception | None = None
        for binding in bindings:
            try:
                binding.close()
            except Exception as exc:
                if error is None:
                    error = exc
        try:
            self.docker_client.close()
        finally:
            if error is not None:
                raise error
