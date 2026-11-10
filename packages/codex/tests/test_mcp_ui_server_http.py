"""Check the counter example's public HTTP asset and error boundaries."""

from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from examples.codex.mcp_ui import server as counter


@asynccontextmanager
async def running():
    app = counter.create_app()
    async with TestServer(app) as server:
        origin = str(server.make_url("/")).rstrip("/")
        app[counter.STATE].update(host=server.make_url("/").authority, origin=origin)
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
            yield app, server, client, origin


async def test_serves_only_fixed_assets_with_app_cors(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "client.js").write_bytes(b"// synthetic host client\n")
    (dist / "app.js").write_bytes(b"// synthetic iframe app\n")
    # These exist so an accidental wildcard route would actually expose them.
    (dist / "clientXjs").write_text("private adjacent file")
    (dist / "appXjs").write_text("private adjacent file")
    (tmp_path / "private.txt").write_text("private parent file")
    monkeypatch.setattr(counter, "ROOT", tmp_path)
    async with running() as (_, server, client, _):
        for name, content in [
            ("client.js", "// synthetic host client\n"),
            ("app.js", "// synthetic iframe app\n"),
        ]:
            async with client.get(server.make_url("/" + name)) as response:
                assert response.status == 200
                assert await response.text() == content
                assert response.headers.get("Access-Control-Allow-Origin") == (
                    "*" if name == "app.js" else None
                )
        for path in [
            "/clientXjs",
            "/appXjs",
            "/private.txt",
            "/dist/client.js",
            "/%2e%2e/private.txt",
        ]:
            async with client.get(server.make_url(path)) as response:
                assert response.status == 404
                assert "Access-Control-Allow-Origin" not in response.headers
                assert "private" not in await response.text()


@pytest.mark.parametrize("error_type", [ValueError, TypeError])
async def test_action_errors_do_not_expose_exception_details(error_type):
    calls = []
    detail = "synthetic-private-path/internal.py:42: credential detail"

    class RejectedApp:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            raise error_type(detail)

        def close(self):
            pass

    async with running() as (app, server, client, origin):
        async with client.get(server.make_url("/")) as response:
            assert response.status == 200
        app[counter.STATE].update(app=RejectedApp(), token="synthetic-view-token")
        async with client.post(
            server.make_url("/api/action"),
            headers={"Origin": origin, "X-MCP-Example": "1", "X-View": "synthetic-view-token"},
            json={"name": "increment", "arguments": {"amount": 1}},
        ) as response:
            assert response.status == 400
            assert await response.text() == "Invalid counter action"
            assert detail not in str(response.headers)
        assert calls == [("increment", {"amount": 1})]
