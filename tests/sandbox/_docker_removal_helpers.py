"""Recording fixtures shared only by Docker removal boundary tests."""

from __future__ import annotations

import sys
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from agents.sandbox import Manifest, SandboxPathGrant
from agents.sandbox.sandboxes import (
    DockerRemovalService,
    docker_removal,
)
from agents.sandbox.sandboxes.docker import (
    DockerSandboxSession,
    DockerSandboxSessionState,
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
