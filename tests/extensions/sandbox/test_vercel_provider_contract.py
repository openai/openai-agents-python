"""Exercise the installed provider, rather than the adapter's fake Vercel module."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "policy",
    [
        "allow-all",
        "deny-all",
        {
            "allow": ["example.com"],
            "subnets": {"allow": None, "deny": ["192.168.0.0/16"]},
        },
        {
            "allow": {
                "example.com": [{"transform": [{"headers": {"X-Test-Header": "synthetic-value"}}]}]
            },
            "subnets": None,
        },
    ],
)
def test_installed_vercel_preserves_released_policy_and_state(policy: object) -> None:
    pytest.importorskip("vercel.sandbox")

    from agents.extensions.sandbox.vercel import (
        VercelSandboxClient,
        VercelSandboxClientOptions,
        VercelSandboxSessionState,
    )
    from agents.sandbox import Manifest
    from agents.sandbox.snapshot import NoopSnapshot

    options = VercelSandboxClientOptions.model_validate({"network_policy": policy})
    state = VercelSandboxSessionState(
        session_id="00000000-0000-0000-0000-000000000001",
        sandbox_id="synthetic-existing-sandbox",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="synthetic-snapshot"),
        runtime="node22",
        interactive=True,
        network_policy=options.network_policy,
    )
    client = VercelSandboxClient(token="synthetic-token")

    payload = client.serialize_session_state(state)
    assert payload["network_policy"] == policy
    assert payload["sandbox_id"] == "synthetic-existing-sandbox"
    assert payload["runtime"] == "node22"
    assert payload["interactive"] is True
    assert "token" not in payload

    restored = client.deserialize_session_state(payload)
    assert client.serialize_session_state(restored) == payload


@pytest.fixture
def provider_wire(monkeypatch):
    """Exercise real Vercel handles and request codecs over a synthetic HTTP server."""
    import asyncio
    import json
    from types import SimpleNamespace

    import httpx2 as httpx
    from vercel.sandbox import SandboxClient

    wire = SimpleNamespace(
        requests=[],
        clients=[],
        sandboxes={},
        next_id=0,
        block_create=False,
        stop_failures=0,
        block_stop=False,
        stopping=asyncio.Event(),
        refresh_error=None,
        block_refresh=False,
        refreshing=asyncio.Event(),
        allocated=asyncio.Event(),
        release=asyncio.Event(),
    )

    def response(state):
        return {
            "sandbox": {
                "name": state["name"],
                "currentSessionId": state["id"],
                "status": state["status"],
                "cwd": "/vercel/sandbox",
            },
            "session": {
                "id": state["id"],
                "status": state["status"],
                "cwd": "/vercel/sandbox",
                "sourceSandboxName": state["name"],
            },
            "routes": [
                {
                    "url": "https://app.example.com",
                    "port": 3000,
                    "subdomain": "app",
                    "system": False,
                }
            ],
        }

    async def handle(request):
        wire.requests.append(request)
        path = request.url.path.removeprefix("/api/")
        if path == "v3/sandboxes" and request.method == "POST":
            body = json.loads(request.content)
            wire.next_id += 1
            state = {"name": body["name"], "id": f"session-{wire.next_id}", "status": "running"}
            wire.sandboxes[state["name"]] = state
            wire.allocated.set()
            if wire.block_create:
                await wire.release.wait()
            return httpx.Response(200, json=response(state))
        if path.startswith("v2/sandboxes/sessions/"):
            session_id, _, operation = path.removeprefix("v2/sandboxes/sessions/").partition("/")
            if not operation:
                wire.refreshing.set()
                if wire.block_refresh:
                    await wire.release.wait()
                if wire.refresh_error is not None:
                    raise wire.refresh_error
                state = next(s for s in wire.sandboxes.values() if s["id"] == session_id)
                return httpx.Response(200, json=response(state))
            if operation == "stop":
                state = next(s for s in wire.sandboxes.values() if s["id"] == session_id)
                wire.stopping.set()
                if wire.block_stop:
                    await wire.release.wait()
                if wire.stop_failures:
                    wire.stop_failures -= 1
                    return httpx.Response(
                        503, json={"error": {"code": "unavailable", "message": "synthetic"}}
                    )
                state["status"] = "stopped"
                return httpx.Response(200, json=response(state))
            if operation == "snapshot":
                state = next(s for s in wire.sandboxes.values() if s["id"] == session_id)
                return httpx.Response(
                    200,
                    json={
                        "session": response(state)["session"],
                        "snapshot": {
                            "id": "snapshot-synthetic",
                            "sourceSessionId": session_id,
                            "region": "iad1",
                            "status": "created",
                            "sizeBytes": 1,
                            "createdAt": 1,
                            "updatedAt": 1,
                        },
                    },
                )
            if operation == "cmd":
                command = {
                    "id": "cmd-1",
                    "name": "sh",
                    "args": [],
                    "cwd": "/vercel/sandbox",
                    "sessionId": session_id,
                    "startedAt": 1,
                }
                lines = [
                    {"command": command},
                    {"stream": "stdout", "data": "synthetic stdout"},
                    {"stream": "stderr", "data": "synthetic stderr"},
                    {"command": {**command, "exitCode": 7}},
                ]
                return httpx.Response(200, content="\n".join(json.dumps(line) for line in lines))
        if path.startswith("v2/sandboxes/"):
            name = path.removeprefix("v2/sandboxes/")
            state = wire.sandboxes.get(name)
            if state is None:
                return httpx.Response(
                    404, json={"error": {"code": "not_found", "message": "missing"}}
                )
            if request.method == "DELETE":
                wire.sandboxes.pop(name)
            return httpx.Response(200, json=response(state))
        raise AssertionError(f"Unexpected synthetic request: {request.method} {path}")

    original_create = SandboxClient.create.__func__

    def client_factory():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        wire.clients.append(client)
        return client

    def create(cls, **kwargs):
        return original_create(cls, **kwargs, httpx_client_factory=client_factory)

    monkeypatch.setattr(SandboxClient, "create", classmethod(create))
    return wire


def _agents_client():
    from agents.extensions.sandbox.vercel import VercelSandboxClient

    return VercelSandboxClient(
        token="synthetic-token", project_id="synthetic-project", team_id="synthetic-team"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("interactive", [False, True])
async def test_installed_provider_wire_options_output_resume_and_cleanup(
    provider_wire, capsys, interactive
):
    import json

    from agents.extensions.sandbox.vercel import VercelSandboxClientOptions

    client = _agents_client()
    options = VercelSandboxClientOptions(
        timeout_ms=12500,
        runtime="node22",
        interactive=interactive,
        exposed_ports=(3000,),
        network_policy={
            "allow": {"example.com": [{"transform": [{"headers": {"X-Test": "synthetic"}}]}]}
        },
    )
    session = await client.create(options=options)
    request = provider_wire.requests[0]
    body = json.loads(request.content)
    assert body["timeout"] == 12500
    assert body["image"] == "vercel/sandbox/node:al-22"
    assert body["persistent"] is False
    assert body["networkPolicy"] == {
        "allow": {"example.com": [{"transform": [{"headers": {"X-Test": "synthetic"}}]}]}
    }
    assert request.headers["authorization"] == "Bearer synthetic-token"
    assert body["projectId"] == "synthetic-project"

    result = await session.exec("exit 7")
    assert (result.stdout, result.stderr, result.exit_code) == (
        b"synthetic stdout",
        b"synthetic stderr",
        7,
    )
    assert capsys.readouterr().out == ""
    endpoint = await session.resolve_exposed_port(3000)
    assert (endpoint.host, endpoint.port, endpoint.tls) == ("app.example.com", 443, True)

    payload = client.serialize_session_state(session.state)
    assert payload["sandbox_id"] == "session-1"
    assert payload["sandbox_name"] == body["name"]
    assert payload["interactive"] is interactive
    resumed = await client.resume(client.deserialize_session_state(payload))
    assert resumed.state.sandbox_id == "session-1"
    assert provider_wire.next_id == 1
    # Close this second transport without disposing the shared execution.
    await resumed._inner._close_sandbox_client()
    await client.delete(session)
    assert all(s["status"] == "stopped" for s in provider_wire.sandboxes.values())
    assert not any(r.method == "DELETE" for r in provider_wire.requests)
    assert all(transport.is_closed for transport in provider_wire.clients)


@pytest.mark.asyncio
async def test_installed_provider_does_not_adopt_replacement_session(provider_wire):
    from agents.extensions.sandbox.vercel import VercelSandboxClientOptions

    client = _agents_client()
    session = await client.create(options=VercelSandboxClientOptions())
    payload = client.serialize_session_state(session.state)
    original_name = session.state.sandbox_name
    provider_wire.sandboxes[original_name]["id"] = "replacement-session"
    resumed = await client.resume(client.deserialize_session_state(payload))
    assert resumed.state.sandbox_id == "session-2"
    assert resumed.state.sandbox_name != original_name
    assert resumed.state.workspace_root_ready is False
    assert provider_wire.sandboxes[original_name]["id"] == "replacement-session"
    await session._inner._close_sandbox_client()
    await client.delete(resumed)
    assert all(transport.is_closed for transport in provider_wire.clients)


@pytest.mark.asyncio
async def test_installed_provider_cancelled_creation_cleans_allocation(provider_wire):
    import asyncio

    from agents.extensions.sandbox.vercel import VercelSandboxClientOptions

    provider_wire.block_create = True
    task = asyncio.create_task(_agents_client().create(options=VercelSandboxClientOptions()))
    await provider_wire.allocated.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(s["status"] == "stopped" for s in provider_wire.sandboxes.values())
    assert not any(r.method == "DELETE" for r in provider_wire.requests)
    assert all(transport.is_closed for transport in provider_wire.clients)


@pytest.mark.asyncio
@pytest.mark.parametrize("expiration_ms", [None, 0, 86400000])
async def test_installed_provider_snapshot_preserves_millisecond_lifetime(
    provider_wire, expiration_ms
):
    import json

    from agents.extensions.sandbox.vercel import VercelSandboxClientOptions

    client = _agents_client()
    session = await client.create(
        options=VercelSandboxClientOptions(
            workspace_persistence="snapshot",
            snapshot_expiration_ms=expiration_ms,
        )
    )
    stream = await session.persist_workspace()
    assert b"snapshot-synthetic" in stream.read()
    request = next(r for r in provider_wire.requests if r.url.path.endswith("/snapshot"))
    if expiration_ms is None:
        assert request.content == b""
    else:
        assert json.loads(request.content) == {"expiration": expiration_ms}
    await client.delete(session)


@pytest.mark.asyncio
async def test_installed_provider_resumes_legacy_id_without_name(provider_wire):
    from agents.extensions.sandbox.vercel import VercelSandboxSessionState
    from agents.sandbox import Manifest
    from agents.sandbox.snapshot import NoopSnapshot

    provider_wire.sandboxes["legacy-id"] = {
        "name": "legacy-id",
        "id": "legacy-id",
        "status": "running",
    }
    client = _agents_client()
    state = VercelSandboxSessionState(
        sandbox_id="legacy-id",
        manifest=Manifest(),
        snapshot=NoopSnapshot(id="synthetic"),
        workspace_root_ready=True,
    )
    payload = client.serialize_session_state(state)
    payload.pop("sandbox_name")
    resumed = await client.resume(client.deserialize_session_state(payload))
    assert resumed.state.sandbox_id == "legacy-id"
    assert resumed.state.workspace_root_ready is True
    assert provider_wire.next_id == 0
    await client.delete(resumed)
    assert all(s["status"] == "stopped" for s in provider_wire.sandboxes.values())
    assert not any(r.method == "DELETE" for r in provider_wire.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime,image",
    [
        (None, "vercel/sandbox/node:al-24"),
        ("node22", "vercel/sandbox/node:al-22"),
        ("node24", "vercel/sandbox/node:al-24"),
        ("node26", "vercel/sandbox/node:al-26"),
        ("python3.13", "vercel/sandbox/python:al-3.13.1"),
    ],
)
async def test_installed_provider_preserves_runtime_selection(provider_wire, runtime, image):
    import json

    from agents.extensions.sandbox.vercel import VercelSandboxClientOptions

    client = _agents_client()
    session = await client.create(options=VercelSandboxClientOptions(runtime=runtime))
    assert json.loads(provider_wire.requests[0].content)["image"] == image
    await client.delete(session)


@pytest.mark.asyncio
async def test_installed_provider_rejects_unknown_runtime_before_allocation(provider_wire):
    from agents.extensions.sandbox.vercel import VercelSandboxClientOptions

    with pytest.raises(ValueError, match="Unsupported Vercel runtime"):
        await _agents_client().create(options=VercelSandboxClientOptions(runtime="unsupported"))
    assert provider_wire.requests == []
    assert provider_wire.clients == []


@pytest.mark.asyncio
async def test_installed_provider_failed_delete_keeps_cleanup_retryable(provider_wire):
    from vercel.sandbox import SandboxApiError

    from agents.extensions.sandbox.vercel import VercelSandboxClientOptions

    client = _agents_client()
    session = await client.create(options=VercelSandboxClientOptions())
    provider_wire.stop_failures = 1
    with pytest.raises(SandboxApiError) as failure:
        await client.delete(session)
    assert failure.value.status_code == 503
    assert len(provider_wire.sandboxes) == 1
    assert not provider_wire.clients[0].is_closed

    await client.delete(session)
    assert all(s["status"] == "stopped" for s in provider_wire.sandboxes.values())
    assert not any(r.method == "DELETE" for r in provider_wire.requests)
    assert all(transport.is_closed for transport in provider_wire.clients)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["provider", "timeout", "cancelled"])
async def test_installed_provider_closes_unadopted_reconnect_client(provider_wire, failure):
    import asyncio

    import httpx2 as httpx

    from agents.extensions.sandbox.vercel import VercelSandboxClientOptions

    client = _agents_client()
    session = await client.create(options=VercelSandboxClientOptions())
    payload = client.serialize_session_state(session.state)
    original_name = session.state.sandbox_name
    provider_wire.sandboxes[original_name]["status"] = "pending"
    if failure == "cancelled":
        provider_wire.block_refresh = True
    else:
        provider_wire.refresh_error = (
            httpx.ConnectError("synthetic polling failure")
            if failure == "provider"
            else asyncio.TimeoutError()
        )
    task = asyncio.create_task(client.resume(client.deserialize_session_state(payload)))
    await provider_wire.refreshing.wait()
    if failure == "cancelled":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider_wire.next_id == 1
    else:
        resumed = await task
        assert resumed.state.sandbox_id == "session-2"
        assert resumed.state.workspace_root_ready is False
        assert not provider_wire.clients[-1].is_closed
        await client.delete(resumed)
    assert provider_wire.clients[1].is_closed
    assert not provider_wire.clients[0].is_closed
    assert original_name in provider_wire.sandboxes
    await client.delete(session)
    assert all(transport.is_closed for transport in provider_wire.clients)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "next_status", ["running", "stopping", "stopped", "failed", "aborted", "snapshotting"]
)
async def test_installed_provider_pending_reconnect_observes_transition(provider_wire, next_status):
    import asyncio

    from agents.extensions.sandbox.vercel import VercelSandboxClientOptions

    client = _agents_client()
    session = await client.create(options=VercelSandboxClientOptions())
    session.state.workspace_root_ready = True
    payload = client.serialize_session_state(session.state)
    original = provider_wire.sandboxes[session.state.sandbox_name]
    original["status"] = "pending"
    provider_wire.block_refresh = True
    task = asyncio.create_task(client.resume(client.deserialize_session_state(payload)))
    await asyncio.wait_for(provider_wire.refreshing.wait(), timeout=5)
    original["status"] = next_status
    provider_wire.release.set()
    # A terminal transition must not consume the 45-second reconnect deadline.
    resumed = await asyncio.wait_for(task, timeout=5)
    if next_status == "running":
        assert resumed.state.sandbox_id == session.state.sandbox_id
        assert resumed.state.workspace_root_ready is True
        assert provider_wire.next_id == 1
    else:
        assert resumed.state.sandbox_id != session.state.sandbox_id
        assert resumed.state.workspace_root_ready is False
        assert provider_wire.next_id == 2
        assert provider_wire.clients[1].is_closed
    await client.delete(resumed)
    await client.delete(session)
    assert all(transport.is_closed for transport in provider_wire.clients)


@pytest.mark.asyncio
async def test_installed_provider_cleanup_preserves_concurrent_replacement(provider_wire):
    import asyncio

    from agents.extensions.sandbox.vercel import VercelSandboxClientOptions

    client = _agents_client()
    session = await client.create(options=VercelSandboxClientOptions())
    name = session.state.sandbox_name
    original = provider_wire.sandboxes[name]
    provider_wire.block_stop = True
    task = asyncio.create_task(client.delete(session))
    await asyncio.wait_for(provider_wire.stopping.wait(), timeout=5)
    replacement = {"name": name, "id": "replacement-session", "status": "running"}
    provider_wire.sandboxes[name] = replacement
    provider_wire.release.set()
    await asyncio.wait_for(task, timeout=5)
    assert original["status"] == "stopped"
    assert provider_wire.sandboxes[name] == replacement
    assert replacement["status"] == "running"
    assert not any(r.method == "DELETE" for r in provider_wire.requests)
    assert all(transport.is_closed for transport in provider_wire.clients)
