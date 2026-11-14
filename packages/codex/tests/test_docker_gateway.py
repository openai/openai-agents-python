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
    config = tmp_path / "docker-config"
    config.mkdir()
    (config / "config.json").write_text("{}")
    monkeypatch.setenv("DOCKER_CONFIG", str(config))
    netrc = tmp_path / "empty.netrc"
    netrc.write_text("")
    monkeypatch.setenv("NETRC", str(netrc))
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


@pytest.mark.parametrize("mode", ["auth", "cookie", "netrc"])
async def test_docker_gateway_rejects_effective_http_auth_before_daemon_calls(
    configured_client, monkeypatch, tmp_path, mode
):
    import requests

    api = configured_client.docker_client.api
    if mode == "auth":
        api.auth = ("scoped-user", "synthetic-password")
    elif mode == "cookie":
        api.cookies.set("role", "synthetic-scope")
    else:
        netrc = tmp_path / "scoped.netrc"
        netrc.write_text("machine localhost login scoped-user password synthetic-password\n")
        netrc.chmod(0o600)
        monkeypatch.setenv("NETRC", str(netrc))
    prepared = api.prepare_request(requests.Request("GET", "http+docker://localhost/_ping"))
    assert "Authorization" in prepared.headers or "Cookie" in prepared.headers
    calls = []

    def info():
        calls.append("info")
        return {"ID": "selected", "OSType": "linux", "Plugins": {"Authorization": []}}

    monkeypatch.setattr(api, "info", info)
    with pytest.raises(ValueError, match="authentication scope"):
        await DockerGateway(configured_client)._preflight()
    assert calls == []


@pytest.mark.parametrize("trust_env", [False, True])
async def test_docker_gateway_preserves_unauthed_default_and_explicit_environment_policy(
    configured_client, monkeypatch, trust_env
):
    api = configured_client.docker_client.api
    api.trust_env = trust_env
    monkeypatch.setattr(
        api, "info", lambda: {"ID": "selected", "OSType": "linux", "Plugins": {"Authorization": []}}
    )
    current, _, daemon = await DockerGateway(configured_client)._preflight()
    assert current is api and len(daemon) == 64
    assert api.trust_env is trust_env


async def test_docker_gateway_rejects_auth_learned_from_daemon_info(configured_client, monkeypatch):
    api = configured_client.docker_client.api

    def info():
        api.cookies.set("role", "synthetic-scope")
        return {"ID": "selected", "OSType": "linux", "Plugins": {"Authorization": []}}

    monkeypatch.setattr(api, "info", info)
    with pytest.raises(ValueError, match="authentication scope"):
        await DockerGateway(configured_client)._preflight()


async def test_docker_gateway_respects_explicit_netrc_opt_out(
    configured_client, monkeypatch, tmp_path
):
    api = configured_client.docker_client.api
    netrc = tmp_path / "scoped.netrc"
    netrc.write_text("machine localhost login scoped-user password synthetic-password\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))
    api.trust_env = False
    monkeypatch.setattr(
        api, "info", lambda: {"ID": "selected", "OSType": "linux", "Plugins": {"Authorization": []}}
    )
    current, _, _ = await DockerGateway(configured_client)._preflight()
    assert current is api and api.trust_env is False


@pytest.mark.parametrize("mode", ["query", "hook", "cert", "authority"])
async def test_docker_gateway_rejects_custom_request_scope(configured_client, monkeypatch, mode):
    api = configured_client.docker_client.api
    if mode == "query":
        api.params = {"role": "synthetic-scope"}
    elif mode == "hook":
        api.hooks["response"].append(lambda response: response)
    elif mode == "cert":
        api.cert = "synthetic-client-certificate"
    else:
        api.base_url = "http+docker://other-authority"
    calls = []
    monkeypatch.setattr(api, "info", lambda: calls.append("info"))
    with pytest.raises(ValueError, match="authentication scope"):
        await DockerGateway(configured_client)._preflight()
    assert calls == []


@pytest.mark.parametrize("user", ["", " root", "root ", "--privileged", "root\n", "a/b", "a" * 257])
def test_selected_process_user_rejects_ambiguous_current_declarations(configured_client, user):
    with pytest.raises(ValueError, match="container user"):
        DockerGateway(configured_client, run_as=user)


@pytest.mark.parametrize("user", ["root", "nobody", "1000:1000", "worker:users"])
def test_selected_process_user_is_an_immutable_current_gateway_option(configured_client, user):
    gateway = DockerGateway(configured_client, run_as=user)
    assert gateway.run_as == user


def test_private_selected_user_receipt_does_not_create_a_public_environment_label():
    from _agents_codex.sandbox import _DockerStdioEnvironment

    selected = _DockerStdioEnvironment(
        url="docker://current/container",
        cwd="/workspace",
        _argv=("docker", "exec", "--user=1000:1000", "current", "server", "stdio"),
        _require_process_identity=True,
    )
    native = selected._to_native()
    command = native.get("stdio_command")
    assert command is not None
    assert command.get("require_process_identity") is True
    assert "--user=1000:1000" in command["args"]
    assert selected.env is None
    default = _DockerStdioEnvironment(
        url="docker://current/container",
        cwd="/workspace",
        _argv=("docker", "exec"),
    )
    assert "require_process_identity" not in default._to_native()["stdio_command"]
