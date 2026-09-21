"""Opt-in, host-side removal for private, rootful Linux Docker containers.

Run this service in a trusted process on the Docker daemon host, as root. It uses
host Python workers, not binaries supplied by the container. It does not listen on
a network socket. Give its Docker client only to trusted application code.

The service requires a trusted image, Docker 26+ with its builtin seccomp profile,
and the runc runtime. Workspaces and grant roots must exist in the image.
The initial implementation excludes shared mounts, additional capabilities, user
namespaces, and containers whose workspace or grant roots do not already exist.
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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from docker import DockerClient  # type: ignore[import-untyped]
from docker.models.containers import Container  # type: ignore[import-untyped]

from ..errors import WorkspaceArchiveWriteError
from ..manifest import Manifest
from ..workspace_paths import WorkspacePathPolicy, posix_path_for_error


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
            raise RuntimeError(f"Docker removal worker rejected request: {response['reason']}")
        return cast(dict[str, Any], response)

    def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
        self.process.wait()
        if self.process.stdout is not None:
            self.process.stdout.close()


def _configuration(manifest: Manifest) -> tuple[str, tuple[tuple[str, bool], ...]]:
    return manifest.root, tuple(
        (grant.path, grant.read_only) for grant in manifest.extra_path_grants
    )


@dataclass
class _Binding:
    worker: _Worker
    incarnation: tuple[int, str]
    configuration: tuple[str, tuple[tuple[str, bool], ...]]
    policy: WorkspacePathPolicy


class DockerRemovalService:
    """Own fixed grant bindings and paused-workload removal on a local Docker host.

    Construct ``DockerSandboxClient(service.docker_client, removal_service=service)``.
    The service must not be shared with untrusted callers or exposed to containers.
    Only newly created, private containers can acquire a binding. Replacing a bound
    canonical root invalidates it; changing its original symlink alias does not.

    Create the service on the daemon host, then pass its connection and live service
    to the client::

        service = DockerRemovalService()
        client = DockerSandboxClient(service.docker_client, removal_service=service)

    Use the normal ``client.create`` and ``session.start`` lifecycle. After use, await
    ``client.delete(session)`` before calling ``service.close()``. Each removal pauses
    all workload processes until it finishes, which can affect concurrent command
    deadlines. Stopped/restarted containers require a fresh session; a binding must
    not be reconstructed from saved paths. Shared mounts and custom security profiles
    are deliberately unsupported by this initial implementation.
    """

    docker_client: DockerClient
    _lock: threading.RLock
    _bindings: dict[str, _Binding]

    def __init__(self, *, socket_path: str = "/var/run/docker.sock") -> None:
        if sys.platform != "linux" or os.geteuid() != 0:
            raise RuntimeError("DockerRemovalService requires root on the Linux Docker host")
        self.docker_client = DockerClient(base_url=f"unix://{socket_path}")
        self._lock = threading.RLock()
        self._bindings = {}

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
            attrs.get("Mounts")
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
        self, container: Container, worker: _Worker | None = None
    ) -> Iterator[tuple[int, str]]:
        incarnation = self._state(container)
        already_paused = bool(container.attrs["State"]["Paused"])
        if not already_paused:
            container.pause()
        # The Docker pause request completes before exec or filesystem work begins.
        if self._state(container) != incarnation or not container.attrs["State"]["Paused"]:
            raise RuntimeError("Docker container changed while pausing; left paused")
        try:
            yield incarnation
        finally:
            if not already_paused and (worker is None or not worker.uncertain):
                if self._state(container) == incarnation and container.attrs["State"]["Paused"]:
                    container.unpause()

    def bind_new(self, container: Container, manifest: Manifest) -> None:
        """Bind before a newly created session is returned to its trusted application."""
        with self._lock:
            if container.id in self._bindings:
                raise ValueError("Docker removal authority is already bound")
            if any(grant.host_path is not None for grant in manifest.extra_path_grants):
                raise ValueError("Docker removal service does not support shared host paths")
            with self._paused(container) as incarnation:
                worker = _Worker(incarnation[0])
                try:
                    result = worker.request(
                        operation="bind",
                        paths=[
                            manifest.root,
                            *(grant.path for grant in manifest.extra_path_grants),
                        ],
                    )
                    paths = result["paths"]
                    policy = WorkspacePathPolicy(
                        root=paths[0],
                        extra_path_grants=tuple(
                            grant.model_copy(update={"path": path})
                            for grant, path in zip(
                                manifest.extra_path_grants, paths[1:], strict=True
                            )
                        ),
                    )
                    self._bindings[container.id] = _Binding(
                        worker, incarnation, _configuration(manifest), policy
                    )
                except BaseException:
                    worker.close()
                    raise

    def assert_bound(self, container: Container, manifest: Manifest) -> None:
        with self._lock:
            binding = self._bindings.get(container.id)
            if binding is None or binding.configuration != _configuration(manifest):
                raise ValueError("Docker removal requires the original live authority binding")
            if binding.worker.uncertain or self._state(container) != binding.incarnation:
                raise ValueError("Docker removal authority is no longer usable")

    def remove(
        self, container: Container, manifest: Manifest, path: Path | str, user: str | None
    ) -> None:
        """Authorize and remove while the workload is paused; never use container exec."""
        with self._lock:
            self.assert_bound(container, manifest)
            binding = self._bindings[container.id]
            original_policy = WorkspacePathPolicy(
                root=manifest.root, extra_path_grants=manifest.extra_path_grants
            )
            original = original_policy.normalize_sandbox_path(path)
            selected = (
                original
                if Path(path).is_absolute()
                else binding.policy.normalize_sandbox_path(path)
            )
            with self._paused(container, binding.worker):
                try:
                    inspection = binding.worker.request(
                        operation="inspect", path=selected.as_posix()
                    )
                    target = inspection["path"]
                    if target:
                        if inspection["is_directory"]:
                            binding.policy.validate_recursive_remove(target)
                        else:
                            binding.policy.normalize_sandbox_path(target, for_write=True)
                    docker_user = user or container.attrs.get("Config", {}).get("User") or "0"
                    binding.worker.request(operation="remove", user=docker_user)
                except RuntimeError as exc:
                    raise WorkspaceArchiveWriteError(
                        path=posix_path_for_error(original),
                        context={"reason": "docker_removal_failed"},
                        cause=exc,
                    ) from exc

    def release(self, container_id: str) -> None:
        """Release authority after its container has been deleted."""
        with self._lock:
            binding = self._bindings.pop(container_id, None)
            if binding is not None:
                binding.worker.close()

    def close(self) -> None:
        """Release workers; an uncertain operation does not automatically thaw its container."""
        with self._lock:
            for container_id in tuple(self._bindings):
                self.release(container_id)
            self.docker_client.close()
