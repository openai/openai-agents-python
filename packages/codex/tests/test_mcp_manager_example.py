"""Local HTTP admission for the optional Classic MCP manager example."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx2
import pytest

pytest.importorskip("fastapi", minversion="0.150.0")

from examples.classic.mcp.manager_example import app as manager_app

POSTS = {
    "/run": {"input": "synthetic input"},
    "/add": {"a": 2, "b": 3},
    "/reconnect": {"failed_only": True},
}


@pytest.fixture
def backend(monkeypatch):
    server = SimpleNamespace(
        name="synthetic-local",
        list_tools=AsyncMock(return_value=[SimpleNamespace(name="add")]),
        call_tool=AsyncMock(return_value=SimpleNamespace(model_dump=lambda **kwargs: {"sum": 5})),
    )
    manager = SimpleNamespace(
        active_servers=[server],
        failed_servers=[],
        reconnect=AsyncMock(return_value=[server]),
    )
    agent = Mock(return_value=object())
    run = AsyncMock(return_value=SimpleNamespace(final_output="synthetic output"))
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-not-a-key")
    monkeypatch.setattr(manager_app, "USE_MCP_MANAGER", True)
    monkeypatch.setattr(manager_app, "Agent", agent)
    monkeypatch.setattr(manager_app.Runner, "run", run)
    monkeypatch.setattr(manager_app.app.state, "mcp_manager", manager, raising=False)
    monkeypatch.setattr(manager_app.app.state, "active_servers", [server], raising=False)
    return SimpleNamespace(server=server, manager=manager, agent=agent, run=run)


def assert_no_dispatch(backend):
    backend.agent.assert_not_called()
    backend.run.assert_not_awaited()
    backend.server.list_tools.assert_not_awaited()
    backend.server.call_tool.assert_not_awaited()
    backend.manager.reconnect.assert_not_awaited()


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
@pytest.mark.parametrize("use_manager", [False, True])
async def test_loopback_hosts_preserve_manager_and_direct_flows(
    backend, monkeypatch, host, use_manager
):
    monkeypatch.setattr(manager_app, "USE_MCP_MANAGER", use_manager)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=manager_app.app), base_url=f"http://{host}:9001"
    ) as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"connected_servers": ["synthetic-local"], "failed_servers": []}
        tools = await client.get("/tools")
        assert tools.status_code == 200
        assert tools.json() == {"tools": ["add"]}
        added = await client.post("/add", json=POSTS["/add"])
        assert added.status_code == 200
        assert added.json() == {"result": {"sum": 5}}
        result = await client.post("/run", json=POSTS["/run"])
        assert result.status_code == 200
        assert result.json() == {"output": "synthetic output"}
        reconnected = await client.post("/reconnect", json=POSTS["/reconnect"])
        if use_manager:
            assert reconnected.status_code == 200
            assert reconnected.json() == {"connected_servers": ["synthetic-local"]}
            backend.manager.reconnect.assert_awaited_once_with(failed_only=True)
        else:
            assert reconnected.status_code == 400
            assert reconnected.json() == {"detail": "MCPServerManager is disabled"}
            backend.manager.reconnect.assert_not_awaited()
    backend.run.assert_awaited_once()
    backend.server.call_tool.assert_awaited_once_with("add", {"a": 2, "b": 3})


@pytest.mark.parametrize(
    "host",
    ["attacker.example:9001", "localhost.attacker.example:9001", "localhost@attacker.example", ""],
)
async def test_untrusted_hosts_reject_before_all_endpoint_effects(backend, host):
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=manager_app.app), base_url="http://127.0.0.1:9001"
    ) as client:
        for path in ("/health", "/tools"):
            response = await client.get(path, headers={"host": host})
            assert response.status_code == 400
        for path, payload in POSTS.items():
            response = await client.post(path, json=payload, headers={"host": host})
            assert response.status_code == 400
    assert_no_dispatch(backend)


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/x-www-form-urlencoded"])
async def test_simple_browser_bodies_reject_before_post_effects(backend, content_type):
    import json

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=manager_app.app), base_url="http://127.0.0.1:9001"
    ) as client:
        for path, payload in POSTS.items():
            headers = {"origin": "http://attacker.example"}
            if content_type is not None:
                headers["content-type"] = content_type
            response = await client.post(
                path, content=json.dumps(payload).encode(), headers=headers
            )
            if content_type is None:
                assert "content-type" not in response.request.headers
            assert response.status_code == 422
    assert_no_dispatch(backend)


async def test_cross_origin_json_preflight_has_no_cors_permission(backend):
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=manager_app.app), base_url="http://127.0.0.1:9001"
    ) as client:
        response = await client.options(
            "/run",
            headers={
                "origin": "http://attacker.example",
                "access-control-request-method": "POST",
                "access-control-request-headers": "content-type",
            },
        )
    assert response.status_code == 405
    assert "access-control-allow-origin" not in response.headers
    assert_no_dispatch(backend)
