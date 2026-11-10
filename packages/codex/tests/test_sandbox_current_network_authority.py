"""Current provider restrictions gate borrowed and authorized-resume resources."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from agents import Environment
from agents.classic.testing.sandbox import scripted_sandbox_session
from agents.sandbox import Manifest, SandboxSession, sandbox_environment


@pytest.mark.parametrize("backend", ["docker", "e2b", "vercel"])
@pytest.mark.parametrize("lifecycle", ["borrowed", "resume"])
@pytest.mark.parametrize("change", ["matching", "network", "ports"])
async def test_current_network_authority_precedes_provider_and_gateway(
    monkeypatch, backend, lifecycle, change
):
    manifest = Manifest(root="/trusted")
    scripted = scripted_sandbox_session(manifest=manifest)
    ports = (3000,) if change == "ports" else ()
    if backend == "docker":
        from agents.classic.sandbox.sandboxes.docker import (
            DockerSandboxClient,
            DockerSandboxClientOptions,
            DockerSandboxSession,
            DockerSandboxSessionState,
        )

        mode = "none" if change == "matching" else None

        class Container:
            attrs = {
                "Mounts": [],
                "HostConfig": {"NetworkMode": mode or "bridge"},
                "NetworkSettings": {"Networks": {} if mode else {"bridge": {}}},
            }

            def reload(self):
                pass

        state = DockerSandboxSessionState(
            snapshot=scripted.state.snapshot,
            manifest=manifest,
            image="synthetic",
            container_id="synthetic",
            network_mode=mode,
            exposed_ports=ports,
        )
        live = SandboxSession(
            DockerSandboxSession(
                docker_client=None,
                container=Container(),
                state=state,
            )
        )
        client = DockerSandboxClient(None)
        options = DockerSandboxClientOptions(
            image="synthetic",
            network_mode=None if change == "ports" else "none",
        )
    elif backend == "e2b":
        from agents.classic.extensions.sandbox.e2b.sandbox import (
            E2BSandboxClient,
            E2BSandboxClientOptions,
            E2BSandboxSessionState,
            E2BSandboxType,
        )

        state = E2BSandboxSessionState(
            snapshot=scripted.state.snapshot,
            manifest=manifest,
            sandbox_id="synthetic",
            allow_internet_access=change == "network",
            exposed_ports=ports,
        )
        live = SandboxSession(scripted)
        live.state = state
        client = E2BSandboxClient()
        options = E2BSandboxClientOptions(
            sandbox_type=E2BSandboxType.E2B,
            allow_internet_access=False,
        )
    else:
        from agents.classic.extensions.sandbox.vercel.sandbox import (
            VercelSandboxClient,
            VercelSandboxClientOptions,
            VercelSandboxSessionState,
        )

        state = VercelSandboxSessionState(
            snapshot=scripted.state.snapshot,
            manifest=manifest,
            sandbox_id="synthetic",
            network_policy=None if change == "network" else "deny-all",
            exposed_ports=ports,
        )
        live = SandboxSession(scripted)
        live.state = state
        client = VercelSandboxClient()
        options = VercelSandboxClientOptions(network_policy="deny-all")
    resume = AsyncMock(return_value=live)
    monkeypatch.setattr(client, "resume", resume)
    monkeypatch.setattr(client, "delete", AsyncMock())
    monkeypatch.setattr(live, "start", AsyncMock())
    monkeypatch.setattr(live, "aclose", AsyncMock())
    connected = []

    @asynccontextmanager
    async def connect(selected):
        connected.append(selected)
        yield Environment(url="wss://fixture.invalid", cwd="/trusted")

    async def acquire():
        source = sandbox_environment(
            client,
            manifest=manifest,
            options=options,
            connect=connect,
            **(
                {"session": live} if lifecycle == "borrowed" else {"authorized_resume_state": state}
            ),
        )
        async with source:
            pass

    if change == "matching":
        await acquire()
        assert connected == [live]
        assert resume.await_count == (lifecycle == "resume")
    else:
        with pytest.raises(ValueError, match="network|port exposure"):
            await acquire()
        assert connected == []
        resume.assert_not_awaited()
        client.delete.assert_not_awaited()
        live.aclose.assert_not_awaited()
