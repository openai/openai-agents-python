from __future__ import annotations

from typing import Any, cast

import docker.errors  # type: ignore[import-untyped]
import pytest

from agents.sandbox.config import DEFAULT_PYTHON_SANDBOX_IMAGE
from agents.sandbox.manifest import Manifest
from agents.sandbox.sandboxes.docker import (
    DockerSandboxClient,
    DockerSandboxClientOptions,
    DockerSandboxSession,
    DockerSandboxSessionState,
)
from agents.sandbox.session import BaseSandboxClientOptions
from agents.sandbox.snapshot import NoopSnapshot


class _Images:
    def get(self, image: str) -> object:
        _ = image
        return object()

    def pull(self, *args: object, **kwargs: object) -> None:
        raise AssertionError(f"unexpected image pull: {args!r} {kwargs!r}")


class _Container:
    id = "replacement-container"
    status = "created"
    attrs: dict[str, object] = {"Mounts": []}

    def reload(self) -> None:
        return None

    def start(self) -> None:
        self.status = "running"


class _Containers:
    def __init__(self) -> None:
        self.created = _Container()
        self.create_calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> _Container:
        self.create_calls.append(dict(kwargs))
        return self.created

    def get(self, container_id: str) -> _Container:
        _ = container_id
        raise docker.errors.NotFound("container not found")


class _DockerClient:
    def __init__(self) -> None:
        self.images = _Images()
        self.containers = _Containers()


class _NoDockerProviderAccess:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"unexpected Docker provider access: {name}")


def _client() -> tuple[DockerSandboxClient, _DockerClient]:
    docker_client = _DockerClient()
    client = DockerSandboxClient(docker_client=cast(object, docker_client))
    return client, docker_client


def _state(*, network_mode: str | None = None) -> DockerSandboxSessionState:
    payload: dict[str, object] = {
        "manifest": Manifest(),
        "snapshot": NoopSnapshot(id="snapshot"),
        "image": DEFAULT_PYTHON_SANDBOX_IMAGE,
        "container_id": "missing-container",
    }
    if network_mode is not None:
        payload["network_mode"] = network_mode
    return DockerSandboxSessionState.model_validate(payload)


def test_docker_options_accept_network_mode_none() -> None:
    options = DockerSandboxClientOptions(
        image=DEFAULT_PYTHON_SANDBOX_IMAGE,
        network_mode="none",
    )

    assert options.network_mode == "none"


def test_docker_options_reject_other_network_modes() -> None:
    with pytest.raises(ValueError):
        DockerSandboxClientOptions(
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            network_mode=cast(Any, "bridge"),
        )


def test_docker_options_reject_exposed_ports_with_network_mode_none() -> None:
    with pytest.raises(ValueError, match="exposed_ports"):
        DockerSandboxClientOptions(
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            exposed_ports=(8080,),
            network_mode="none",
        )


def test_docker_options_network_mode_round_trip() -> None:
    options = DockerSandboxClientOptions(
        image=DEFAULT_PYTHON_SANDBOX_IMAGE,
        network_mode="none",
    )

    restored = BaseSandboxClientOptions.parse(options.model_dump(mode="json"))

    assert restored == options
    assert isinstance(restored, DockerSandboxClientOptions)
    assert restored.network_mode == "none"


def test_docker_options_omitted_network_mode_preserves_default_behavior() -> None:
    restored = BaseSandboxClientOptions.parse(
        {
            "type": "docker",
            "image": DEFAULT_PYTHON_SANDBOX_IMAGE,
        }
    )

    assert isinstance(restored, DockerSandboxClientOptions)
    assert restored.network_mode is None


@pytest.mark.asyncio
async def test_docker_client_create_applies_and_persists_network_mode_none() -> None:
    client, docker_client = _client()

    session = await client.create(
        options=DockerSandboxClientOptions(
            image=DEFAULT_PYTHON_SANDBOX_IMAGE,
            network_mode="none",
        )
    )

    assert docker_client.containers.create_calls[0]["network_mode"] == "none"
    assert isinstance(session._inner, DockerSandboxSession)
    assert session._inner.state.network_mode == "none"


@pytest.mark.asyncio
async def test_docker_create_container_passes_network_mode_none() -> None:
    client, docker_client = _client()

    container = await client._create_container(
        DEFAULT_PYTHON_SANDBOX_IMAGE,
        network_mode="none",
    )

    assert container is docker_client.containers.created
    assert docker_client.containers.create_calls == [
        {
            "entrypoint": ["tail"],
            "image": DEFAULT_PYTHON_SANDBOX_IMAGE,
            "detach": True,
            "command": ["-f", "/dev/null"],
            "environment": None,
            "network_mode": "none",
        }
    ]


@pytest.mark.asyncio
async def test_docker_create_container_omits_network_mode_by_default() -> None:
    client, docker_client = _client()

    await client._create_container(DEFAULT_PYTHON_SANDBOX_IMAGE)

    assert "network_mode" not in docker_client.containers.create_calls[0]


def test_docker_session_state_network_mode_round_trip() -> None:
    client, _ = _client()
    state = _state(network_mode="none")

    restored = client.deserialize_session_state(state.model_dump(mode="json"))

    assert isinstance(restored, DockerSandboxSessionState)
    assert restored.network_mode == "none"


def test_docker_session_state_rejects_invalid_network_mode_before_provider_access() -> None:
    client = DockerSandboxClient(docker_client=cast(object, _NoDockerProviderAccess()))
    payload = _state().model_dump(mode="json")
    payload["network_mode"] = "bridge"

    with pytest.raises(ValueError):
        client.deserialize_session_state(payload)


def test_docker_state_rejects_no_network_exposed_ports_before_provider_access() -> None:
    client = DockerSandboxClient(docker_client=cast(object, _NoDockerProviderAccess()))
    payload = _state(network_mode="none").model_dump(mode="json")
    payload["exposed_ports"] = [8080]

    with pytest.raises(ValueError):
        client.deserialize_session_state(payload)


def test_docker_session_state_without_network_mode_preserves_old_payloads() -> None:
    client, _ = _client()
    payload = _state().model_dump(mode="json")
    payload.pop("network_mode", None)

    restored = client.deserialize_session_state(payload)

    assert isinstance(restored, DockerSandboxSessionState)
    assert restored.network_mode is None


@pytest.mark.asyncio
async def test_docker_resume_reapplies_network_mode_to_replacement_container() -> None:
    client, docker_client = _client()
    state = _state(network_mode="none")

    await client.resume(state)

    assert docker_client.containers.create_calls[0]["network_mode"] == "none"
    assert state.container_id == "replacement-container"
