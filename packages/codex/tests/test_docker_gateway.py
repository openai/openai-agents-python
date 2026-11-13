"""Docker gateways use only their current provider's explicit Unix daemon."""

import sys
from types import SimpleNamespace

import pytest

from agents import Environment
from agents.sandbox import DockerGateway, Manifest, sandbox_environment


@pytest.fixture
def configured_client(tmp_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("The gateway requires a Unix socket client")
    docker = pytest.importorskip("docker")
    from agents.classic.sandbox.sandboxes.docker import DockerSandboxClient

    monkeypatch.setattr("_agents_codex.sandbox.shutil.which", lambda name: "/fixture/docker")
    connection = docker.DockerClient(
        base_url="unix://" + str(tmp_path / "selected.sock"), version="1.45"
    )
    client = DockerSandboxClient(connection)
    yield client
    connection.close()


def test_docker_gateway_rejects_a_different_provider(configured_client):
    gateway = DockerGateway(configured_client)
    with pytest.raises(ValueError, match="current SDK client"):
        sandbox_environment(
            object(), manifest=Manifest(root="/workspace"), options={}, connect=gateway
        )


@pytest.mark.parametrize(
    "mode",
    [
        {"session": object()},
        {"authorized_resume_state": object()},
        {"snapshot": object()},
        {"managed_workspace": True},
        {"authorized_resume_checkpoint": object()},
    ],
)
def test_docker_gateway_requires_fresh_acquisition(configured_client, mode):
    with pytest.raises(ValueError, match="fresh creation"):
        sandbox_environment(
            configured_client,
            manifest=Manifest(root="/workspace"),
            options={},
            connect=DockerGateway(configured_client),
            **mode,
        )


@pytest.mark.parametrize("path", ["exec-server", "/bin/../exec-server", "/bin/server\x00"])
def test_docker_gateway_rejects_ambiguous_executable_paths(configured_client, path):
    with pytest.raises(ValueError, match="absolute container executable"):
        DockerGateway(configured_client, exec_server_path=path)


@pytest.mark.parametrize(
    "info",
    [
        {"ID": "selected", "OSType": "windows", "Plugins": {"Authorization": []}},
        {"ID": "selected", "OSType": "linux", "Plugins": {"Authorization": ["custom"]}},
        {"ID": "selected", "OSType": "linux", "Plugins": {}},
        {"OSType": "linux", "Plugins": {"Authorization": []}},
    ],
)
async def test_docker_gateway_rejects_unproven_daemon_scope(configured_client, monkeypatch, info):
    monkeypatch.setattr(configured_client.docker_client.api, "info", lambda: info)
    with pytest.raises(ValueError, match="Linux daemon|daemon identity"):
        await DockerGateway(configured_client)._preflight()


async def test_docker_gateway_rejects_custom_authorization_headers(configured_client):
    configured_client.docker_client.api.headers["Authorization"] = "synthetic-boundary"
    with pytest.raises(ValueError, match="authorization scope"):
        await DockerGateway(configured_client)._preflight()


def test_docker_gateway_rejects_replacement_transport(configured_client, monkeypatch):
    monkeypatch.setattr(
        configured_client.docker_client.api, "get_adapter", lambda url: SimpleNamespace()
    )
    with pytest.raises(ValueError, match="standard current Unix socket"):
        DockerGateway(configured_client)


def test_ordinary_environment_does_not_admit_a_docker_transport():
    with pytest.raises(ValueError, match="wss"):
        Environment(url="docker://synthetic/container", cwd="/workspace")


def test_docker_gateway_rejects_relative_cli_selection(configured_client, monkeypatch):
    monkeypatch.setattr("_agents_codex.sandbox.shutil.which", lambda name: "workspace/docker")
    with pytest.raises(ValueError, match="Docker CLI"):
        DockerGateway(configured_client)
