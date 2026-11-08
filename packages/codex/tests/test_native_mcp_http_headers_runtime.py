"""Real native HTTP header behavior; source-only preparation until qualification."""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agents import Agent, Environment, HttpMCP, NativeError, TurnStoppedError
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_agent import provider
from tests.test_native_child_agents import trust
from tests.test_native_mcp_tools import Peer, mcp_call

PRIVATE = "synthetic-private-header-credential"
CURRENT = "synthetic-current-header-credential"
OBSERVED = {
    "x-tenant",
    "x-empty",
    "x-label",
    "authorization",
    "proxy-authorization",
    "referer",
    "content-type",
}


@dataclass
class HeaderPeer(Peer):
    """The existing HTTP peer with raw field observations and opt-in redirect routes."""

    expected: dict[str, str] = field(default_factory=dict)
    observed: list[dict[str, Any]] = field(default_factory=list)
    transports: set[Any] = field(default_factory=set)
    redirect_status: int | None = None
    redirect_target: str = "/target"
    pending_redirect: dict[str, Any] | None = None

    async def handle(self, request: web.Request) -> web.Response:
        raw = {
            k.decode("ascii").lower(): v.decode("utf-8")
            for k, v in request.raw_headers
            if k.decode("ascii").lower() in OBSERVED
        }
        self.transports.add(request.transport)
        body = await request.read()
        rpc = json.loads(body) if body else self.pending_redirect or {}
        self.observed.append(
            {"path": request.path, "method": request.method, "headers": raw, "body": body.decode()}
        )
        if any(raw.get(name) != value for name, value in self.expected.items()):
            return web.Response(status=401)
        if request.method != "POST" and request.path != "/target":
            return web.Response(status=405)
        if request.path == "/mcp" and rpc.get("method") == "tools/list" and self.redirect_status:
            self.pending_redirect = rpc
            return web.Response(
                status=self.redirect_status, headers={"Location": self.redirect_target}
            )
        if rpc.get("method") == "server/discover":
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": rpc["id"],
                    "error": {"code": -32601, "message": "legacy fixture"},
                }
            )
        if rpc.get("method") == "resources/list":
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": rpc["id"],
                    "result": {"resources": [{"uri": "fixture://headers", "name": "headers"}]},
                }
            )
        if rpc.get("method") == "resources/read":
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": rpc["id"],
                    "result": {
                        "contents": [{"uri": "fixture://headers", "text": "private-resource"}]
                    },
                }
            )
        if request.path == "/target" and request.method == "GET":
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": rpc["id"],
                    "result": {
                        "tools": [
                            {
                                "name": "search",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"key": {"type": "string"}},
                                    "required": ["key"],
                                },
                            }
                        ]
                    },
                }
            )
        result = await super().handle(request)
        if rpc.get("method") == "initialize":
            payload = json.loads(result.body)
            payload["result"]["capabilities"]["resources"] = {}
            return web.json_response(payload)
        return result

    @asynccontextmanager
    async def serve(self):
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.handle)
        server = TestServer(app)
        try:
            await server.start_server(shutdown_timeout=1)
            yield str(server.make_url("/mcp"))
        finally:
            await server.close()
            assert all(transport is None or transport.is_closing() for transport in self.transports)


@asynccontextmanager
async def opened(responses: ResponsesFixture, declaration: HttpMCP[Any], **options: Any):
    native = provider(responses)
    try:
        async with Agent(instructions="Read.", tools=[declaration], provider=native).session(
            **options
        ) as session:
            yield session
    finally:
        await native.aclose()


def headers(secret: str = PRIVATE) -> dict[str, str]:
    return {"X-Tenant": secret, "X-Empty": "", "X-Label": "café 東京\tvalue"}


@pytest.mark.parametrize("authentication", ["custom", "bearer"])
async def test_native_http_delivers_copied_authenticated_empty_unicode_headers(
    responses: ResponsesFixture, authentication: str
) -> None:
    supplied = headers()
    options: dict[str, Any] = {}
    expected = {k.lower(): v for k, v in supplied.items()}
    if authentication == "custom":
        supplied["aUtHoRiZaTiOn"] = "Custom synthetic-header-auth"
        expected["authorization"] = supplied["aUtHoRiZaTiOn"]
    else:
        options["bearer_token"] = "synthetic-bearer-auth"
        expected["authorization"] = "Bearer synthetic-bearer-auth"
    peer = HeaderPeer(expected=expected)
    async with timeout(30), peer.serve() as url:
        descriptor = HttpMCP(name="headers", url=url, tools=["search"], headers=supplied, **options)
        supplied["X-Tenant"] = "mutated-after-capture"
        responses.respond(mcp_call("search", "mcp__headers", "read"))
        responses.respond(assistant_output("done"))
        async with opened(responses, descriptor) as session:
            assert (await (await session.submit("read")).get_result()).final_output == "done"
            for secret in (PRIVATE, expected["authorization"]):
                assert secret not in (await session.save_state()).to_json()
        assert peer.calls and peer.observed
        assert all(
            all(row["headers"].get(k) == v for k, v in expected.items()) for row in peer.observed
        )
    for secret in (PRIVATE, expected["authorization"]):
        assert secret not in json.dumps(responses.requests)
        assert secret not in json.dumps(responses.request_headers)


async def test_http_restores_current_headers_over_poisoned_saved_config(
    responses: ResponsesFixture,
) -> None:
    old = HeaderPeer(expected={"x-tenant": PRIVATE})
    current = HeaderPeer(expected={"x-tenant": CURRENT})
    async with timeout(35), old.serve() as old_url, current.serve() as current_url:
        first = HttpMCP(
            name="headers", url=old_url, tools=["search"], headers={"X-Tenant": PRIVATE}
        )
        supplied = {"X-Tenant": CURRENT}
        selected = HttpMCP(name="headers", url=current_url, tools=["search"], headers=supplied)
        supplied["X-Tenant"] = "mutated-current"
        responses.respond(mcp_call("search", "mcp__headers", "old"))
        responses.respond(assistant_output("old"))
        async with opened(responses, first) as session:
            await (await session.submit("old")).get_result()
            saved = await session.save_state()
            assert PRIVATE not in saved.to_json()
        old_count = len(old.observed)
        poisoned = saved.model_copy(
            update={
                "agent_options": {
                    "config": {
                        "mcp_servers": {
                            "headers": {
                                "url": old_url,
                                "http_headers": {"X-Tenant": "poisoned-header"},
                            }
                        }
                    }
                }
            }
        )
        responses.respond(mcp_call("search", "mcp__headers", "current"))
        responses.respond(assistant_output("current"))
        async with opened(responses, selected, state=poisoned) as restored:
            assert (
                await (await restored.submit("continue")).get_result()
            ).final_output == "current"
            assert CURRENT not in (await restored.save_state()).to_json()
        assert len(old.observed) == old_count
        assert current.calls and all(
            row["headers"].get("x-tenant") == CURRENT for row in current.observed
        )
    assert all(
        secret not in json.dumps(responses.requests)
        for secret in [PRIVATE, CURRENT, "poisoned-header"]
    )
    assert all(
        secret not in json.dumps(responses.request_headers)
        for secret in [PRIVATE, CURRENT, "poisoned-header"]
    )


async def test_resource_only_http_projection_retains_headers_without_model_requests(
    responses: ResponsesFixture,
) -> None:
    peer = HeaderPeer(expected={"x-tenant": PRIVATE})
    async with timeout(30), peer.serve() as url:
        async with opened(
            responses, HttpMCP(name="headers", url=url, tools=[], headers={"X-Tenant": PRIVATE})
        ) as session:
            assert (await session.list_mcp_resources("headers"))["resources"][0][
                "uri"
            ] == "fixture://headers"
            assert (await session.read_mcp_resource("headers", "fixture://headers"))["contents"][0][
                "text"
            ] == "private-resource"
            assert PRIVATE not in (await session.save_state()).to_json()
    assert peer.observed and responses.requests == []


@pytest.mark.parametrize("status", [303, 307])
async def test_legacy_same_origin_redirect_preserves_custom_auth_and_strips_owned_fields(
    responses: ResponsesFixture, status: int
) -> None:
    peer = HeaderPeer(
        expected={"x-tenant": PRIVATE, "authorization": "Custom redirect-auth"},
        redirect_status=status,
    )
    async with timeout(30), peer.serve() as url:
        descriptor = HttpMCP(
            name="headers",
            url=url,
            tools=["search"],
            headers={
                "X-Tenant": PRIVATE,
                "Authorization": "Custom redirect-auth",
                "Proxy-Authorization": "synthetic-proxy",
                "Referer": "https://synthetic.invalid/private",
            },
        )
        responses.respond(assistant_output("done"))
        async with opened(responses, descriptor) as session:
            await (await session.submit("read")).get_result()
        assert any(
            row["path"] == "/mcp" and row["headers"].get("proxy-authorization") == "synthetic-proxy"
            for row in peer.observed
        )
        targets = [row for row in peer.observed if row["path"] == "/target"]
        assert targets
        for row in targets:
            assert row["headers"]["x-tenant"] == PRIVATE
            assert row["headers"]["authorization"] == "Custom redirect-auth"
            assert "proxy-authorization" not in row["headers"]
            assert row["headers"]["referer"] == url
            assert row["method"] == ("GET" if status == 303 else "POST")
            if status == 303:
                assert row["body"] == "" and "content-type" not in row["headers"]
            else:
                assert json.loads(row["body"])["method"] == "tools/list"


async def test_cross_origin_redirect_is_blocked_before_target_request(
    responses: ResponsesFixture,
) -> None:
    target = HeaderPeer()
    async with timeout(30), target.serve() as target_url:
        origin = HeaderPeer(redirect_status=307, redirect_target=target_url)
        async with origin.serve() as url:
            with pytest.raises(NativeError):
                async with opened(
                    responses, HttpMCP(name="headers", url=url, tools=["search"], headers=headers())
                ) as session:
                    await (await session.submit("read")).get_result()
                    pytest.fail("cross-origin discovery unexpectedly completed")
        assert origin.pending_redirect is not None
        assert target.observed == []
        assert responses.requests == []


async def test_host_http_headers_stay_off_unrelated_wss_executor(
    responses: ResponsesFixture, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer = HeaderPeer(expected={"x-tenant": PRIVATE})
    async with (
        timeout(40),
        ExecutorPeer(tmp_path / "executor", capture_handshake_headers=True).serve() as executor,
        peer.serve() as url,
    ):
        trust(executor, tmp_path, monkeypatch)
        environment = Environment(
            url=executor.url,
            cwd=str(executor.root),
            headers={"Authorization": "Bearer " + executor.token},
            tools=["exec_command"],
            **({"write_policy": "legacy"} if sys.platform == "win32" else {}),
        )
        responses.respond(function_call("exec_command", {"cmd": "pwd"}, "executor"))
        responses.respond(mcp_call("search", "mcp__headers", "http"))
        responses.respond(assistant_output("done"))
        async with opened(
            responses,
            HttpMCP(name="headers", url=url, tools=["search"], headers={"X-Tenant": PRIVATE}),
            environment=environment,
        ) as session:
            assert (await (await session.submit("read")).get_result()).final_output == "done"
        assert executor.requests and peer.calls
        assert executor.handshake_headers
        for handshake in executor.handshake_headers:
            assert handshake.get("authorization") == "Bearer " + executor.token
            assert "x-tenant" not in handshake
            assert PRIVATE not in json.dumps(handshake)
        assert PRIVATE not in json.dumps(executor.requests)
        assert PRIVATE not in json.dumps(responses.requests)
        assert PRIVATE not in json.dumps(responses.request_headers)


async def test_native_http_close_settles_held_call_without_replay(
    responses: ResponsesFixture,
) -> None:
    peer = HeaderPeer(expected={"x-tenant": PRIVATE}, release_call=asyncio.Event())
    async with timeout(30), peer.serve() as url:
        responses.respond(mcp_call("search", "mcp__headers", "held"))
        async with opened(
            responses,
            HttpMCP(name="headers", url=url, tools=["search"], headers={"X-Tenant": PRIVATE}),
        ) as session:
            turn = await session.submit("held")
            result = asyncio.create_task(turn.get_result())
            try:
                await peer.call_entered.wait()
                await turn.cancel()
                with pytest.raises((TurnStoppedError, NativeError)):
                    await result
            finally:
                peer.release_call.set()
                if not result.done():
                    result.cancel()
                await asyncio.gather(result, return_exceptions=True)
        assert len(peer.calls) == 1
        assert len(responses.requests) == 1
