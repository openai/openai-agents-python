"""Caller-owned MCP resource access through public Session APIs and real transports."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

import agents
from agents import Agent, CapabilityError, HttpMCP, NativeError, SessionBusyError, StdioMCP
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.native_mcp_resource_fixtures import BLOB, CURSOR, TEXT, URI, resource_result
from tests.test_native_agent import provider
from tests.test_native_mcp_stdio import assert_stopped, records
from tests.test_native_mcp_tools import Peer, declarations, mcp_call


@dataclass
class ResourcePeer(Peer):
    resources: bool = True
    tools: bool = False

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json()
        method = body.get("method", "")
        if method == "initialize":
            self.requests.append(body)
            self.ready_entered.set()
            if self.release_ready is not None:
                await self.release_ready.wait()
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {
                            **({"resources": {}} if self.resources else {}),
                            **({"tools": {}} if self.tools else {}),
                        },
                        "serverInfo": {"name": "resource-fixture", "version": "1"},
                    },
                }
            )
        if method.startswith("resources/"):
            self.requests.append(body)
            params = body.get("params", {})
            if (method == "resources/read" and params["uri"] == "fixture://missing") or (
                method in ("resources/list", "resources/templates/list")
                and params.get("cursor") == "fixture-error"
            ):
                return web.json_response(
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {"code": -32002, "message": "Resource missing"},
                    }
                )
            return web.json_response(
                {"jsonrpc": "2.0", "id": body["id"], "result": resource_result(method, params)}
            )
        return await super().handle(request)


def stdio(log: Path, *args: str) -> StdioMCP[Any]:
    return StdioMCP(
        name="docs",
        command=sys.executable,
        tools=[],
        args=(
            "-I",
            "-B",
            str(Path(__file__).with_name("native_mcp_resource_fixtures.py")),
            "--log",
            str(log),
            *args,
        ),
    )


@pytest.mark.parametrize("transport", ["http", "stdio"])
async def test_resource_pages_contents_and_no_model_insertion(
    responses: ResponsesFixture, tmp_path: Path, transport: str
) -> None:
    peer = ResourcePeer()
    log = tmp_path / "resources"
    async with timeout(30), peer.serve() as url:
        descriptor = HttpMCP(name="docs", url=url, tools=[]) if transport == "http" else stdio(log)
        async with Agent(
            instructions="Answer normally.", tools=[descriptor], provider=provider(responses)
        ).session() as session:
            before = (await session.model_context()).items
            assert await session.list_mcp_resources("docs") == resource_result("resources/list", {})
            assert await session.list_mcp_resources("docs", CURSOR) == resource_result(
                "resources/list", {"cursor": CURSOR}
            )
            assert await session.list_mcp_resources("docs", "") == resource_result(
                "resources/list", {"cursor": ""}
            )
            assert await session.list_mcp_resource_templates("docs") == resource_result(
                "resources/templates/list", {}
            )
            assert await session.list_mcp_resource_templates("docs", CURSOR) == resource_result(
                "resources/templates/list", {"cursor": CURSOR}
            )
            assert await session.list_mcp_resource_templates("docs", "") == resource_result(
                "resources/templates/list", {"cursor": ""}
            )
            assert await session.read_mcp_resource("docs", URI) == resource_result(
                "resources/read", {"uri": URI}
            )
            wire = (
                peer.requests
                if transport == "http"
                else [row["message"] for row in records(log) if row["event"] == "request"]
            )
            calls = [row for row in wire if row.get("method", "").startswith("resources/")]
            assert [row["method"] for row in calls] == [
                "resources/list",
                "resources/list",
                "resources/list",
                "resources/templates/list",
                "resources/templates/list",
                "resources/templates/list",
                "resources/read",
            ]
            assert [row.get("params", {}).get("cursor") for row in calls[:6]] == [
                None,
                CURSOR,
                "",
                None,
                CURSOR,
                "",
            ]
            assert calls[-1]["params"]["uri"] == URI
            assert not any(row.get("method") == "tools/list" for row in wire)
            assert (await session.model_context()).items == before
            assert not responses.requests
            responses.respond(assistant_output("done"))
            assert (await (await session.submit("Hello")).get_result()).final_output == "done"
            assert not declarations(responses.requests[0])
            history = [item async for item in session.items()]
            for marker in (TEXT, BLOB, URI, CURSOR):
                assert marker not in json.dumps(responses.requests, ensure_ascii=False)
                assert marker not in json.dumps(history, ensure_ascii=False)
        if transport == "stdio":
            await assert_stopped(log)


async def test_resource_peers_start_lazily_and_reuse_existing_connections(
    responses: ResponsesFixture,
) -> None:
    first, second = ResourcePeer(), ResourcePeer()
    async with timeout(30), first.serve() as first_url, second.serve() as second_url:
        selected = [
            HttpMCP(name="first", url=first_url, tools=[]),
            HttpMCP(name="second", url=second_url, tools=[]),
        ]
        async with Agent(
            instructions="Read.", tools=selected, provider=provider(responses)
        ).session() as session:
            assert not first.requests and not second.requests
            assert (await session.list_mcp_resources("first"))["resources"]
            assert [row["method"] for row in first.requests].count("initialize") == 1
            assert not second.requests
            assert (await session.list_mcp_resource_templates("second"))["resourceTemplates"]
            assert [row["method"] for row in first.requests].count("initialize") == 1
            assert [row["method"] for row in second.requests].count("initialize") == 1
            assert await session.read_mcp_resource("first", URI) == resource_result(
                "resources/read", {"uri": URI}
            )
            assert [row["method"] for row in first.requests].count("initialize") == 1
            assert [row["method"] for row in second.requests].count("initialize") == 1
            assert not any(
                row.get("method") == "tools/list" for row in first.requests + second.requests
            )
            assert not responses.requests


async def test_no_resources_capability_returns_empty_without_wire_list(
    responses: ResponsesFixture,
) -> None:
    peer = ResourcePeer(resources=False)
    async with timeout(30), peer.serve() as url:
        async with Agent(
            instructions="Read.",
            tools=[HttpMCP(name="docs", url=url, tools=[])],
            provider=provider(responses),
        ).session() as session:
            assert await session.list_mcp_resources("docs", CURSOR) == {
                "resources": [],
                "resultType": "complete",
            }
            assert await session.list_mcp_resource_templates("docs", CURSOR) == {
                "resourceTemplates": [],
                "resultType": "complete",
            }
        assert not any(row.get("method", "").startswith("resources/") for row in peer.requests)
        assert not any(row.get("method") == "tools/list" for row in peer.requests)
        assert not responses.requests


async def test_resource_error_settles_and_selected_tool_still_works(
    responses: ResponsesFixture,
) -> None:
    peer = ResourcePeer(tools=True)
    async with timeout(30), peer.serve() as url:
        async with Agent(
            instructions="Read.",
            tools=[HttpMCP(name="docs", url=url, tools=["search"])],
            provider=provider(responses),
        ).session() as session:
            for method, wire_method in (
                (session.list_mcp_resources, "resources/list"),
                (session.list_mcp_resource_templates, "resources/templates/list"),
            ):
                with pytest.raises(NativeError, match="Resource missing"):
                    await method("docs", "fixture-error")
                assert await method("docs") == resource_result(wire_method, {})
            with pytest.raises(NativeError, match="Resource missing"):
                await session.read_mcp_resource("docs", "fixture://missing")
            assert await session.read_mcp_resource("docs", URI) == resource_result(
                "resources/read", {"uri": URI}
            )
            responses.respond(mcp_call("search", "mcp__docs", "coexist"))
            responses.respond(assistant_output("done"))
            assert (await (await session.submit("Search")).get_result()).final_output == "done"
            assert ("mcp__docs", "search") in declarations(responses.requests[0])
            assert len(peer.calls) == 1


async def test_resource_only_rebinding_disable_and_restore_keep_opening_authority(
    responses: ResponsesFixture,
) -> None:
    first, replacement, latest = ResourcePeer(), ResourcePeer(), ResourcePeer(tools=True)
    async with (
        timeout(30),
        first.serve() as first_url,
        replacement.serve() as next_url,
        latest.serve() as restore_url,
    ):
        original = HttpMCP(name="docs", url=first_url, tools=[])
        current = HttpMCP(name="docs", url=next_url, tools=[])
        restored_server = HttpMCP(name="docs", url=restore_url, tools=["search"])
        agent = Agent(instructions="Read.", tools=[original], provider=provider(responses))
        async with agent.session() as session:
            await session.list_mcp_resources("docs")
            for unauthorized in (
                HttpMCP(name="other", url=next_url, tools=[]),
                HttpMCP(name="docs", url=next_url, tools=["search"]),
            ):
                with pytest.raises(ValueError):
                    await session.update(tools=[unauthorized])
            await session.update(tools=[current])
            assert await session.read_mcp_resource("docs", URI) == resource_result(
                "resources/read", {"uri": URI}
            )
            assert not any(row.get("method") == "resources/read" for row in first.requests)
            assert any(row.get("method") == "resources/read" for row in replacement.requests)
            responses.respond(assistant_output("replacement"))
            await (await session.submit("Hello")).get_result()
            assert not declarations(responses.requests[-1])
            # A turn must retain the explicitly selected resource-only default.
            assert (await session.list_mcp_resources("docs"))["resources"]
            state = await session.save_state()
            await session.update(tools=[])
            responses.respond(assistant_output("disabled"))
            await (await session.submit("Hello without resources")).get_result()
            with pytest.raises(ValueError, match="currently selected"):
                await session.list_mcp_resources("docs")
            deselected = await session.save_state()
        async with Agent(
            instructions="Read.", tools=[restored_server], provider=provider(responses)
        ).session(state=state) as restored:
            assert (await restored.list_mcp_resources("docs"))["resources"]
            assert any(row.get("method") == "resources/list" for row in latest.requests)
            responses.respond(assistant_output("restored"))
            await (await restored.submit("Hello again")).get_result()
            assert not declarations(responses.requests[-1])
            assert (await restored.read_mcp_resource("docs", URI))["contents"]
        count = len(latest.requests)
        async with Agent(
            instructions="Read.", tools=[restored_server], provider=provider(responses)
        ).session(state=deselected) as restored:
            with pytest.raises(ValueError, match="currently selected"):
                await restored.list_mcp_resources("docs")
            responses.respond(assistant_output("still disabled"))
            await (await restored.submit("Hello")).get_result()
            assert not declarations(responses.requests[-1])
            with pytest.raises(ValueError, match="currently selected"):
                await restored.read_mcp_resource("docs", URI)
        assert len(latest.requests) == count
        with pytest.raises(CapabilityError, match="current ordinary MCP"):
            async with Agent(instructions="Read.", provider=provider(responses)).session(
                state=state
            ):
                pytest.fail("Restored a resource selector without a current server")
        assert not any(
            row.get("method") == "tools/list" for row in first.requests + replacement.requests
        )


async def test_resource_activation_cancellation_joins_pending_initialize(
    responses: ResponsesFixture,
) -> None:
    release = asyncio.Event()
    peer = ResourcePeer(release_ready=release)
    async with timeout(20), peer.serve() as url:
        async with Agent(
            instructions="Read.",
            tools=[HttpMCP(name="docs", url=url, tools=[], startup_timeout=10)],
            provider=provider(responses),
        ).session() as session:
            pending = asyncio.create_task(session.list_mcp_resources("docs"))
            try:
                await peer.ready_entered.wait()
                assert not pending.done()
                assert not release.is_set()
                pending.cancel()
                # Cancellation must settle while the peer still withholds initialization.
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(pending), 5)
                assert pending.done()
                assert not any(
                    row.get("method", "").startswith("resources/") for row in peer.requests
                )
                assert not responses.requests
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("ending", ["cancel", "close"])
async def test_pending_resource_read_blocks_work_and_joins_stdio_shutdown(
    responses: ResponsesFixture, tmp_path: Path, ending: str
) -> None:
    log = tmp_path / "pending"
    async with timeout(30):
        async with Agent(
            instructions="Read.", tools=[stdio(log, "--hold")], provider=provider(responses)
        ).session() as session:
            pending = asyncio.create_task(session.read_mcp_resource("docs", URI))
            try:
                while not any(
                    row.get("message", {}).get("method") == "resources/read" for row in records(log)
                ):
                    await asyncio.sleep(0.01)
                with pytest.raises(SessionBusyError):
                    await session.submit("overlap")
                with pytest.raises(RuntimeError, match="busy"):
                    await session.update(tools=[])
                with pytest.raises(RuntimeError, match="checkpoint.*mutation"):
                    await session.save_state()
                if ending == "cancel":
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                else:
                    await session.aclose()
                    assert pending.done()
                    with pytest.raises((RuntimeError, NativeError, asyncio.CancelledError)):
                        await pending
            finally:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        await assert_stopped(log)
        assert not responses.requests


async def test_resource_read_elicitation_without_turn_rejects_without_hanging(
    responses: ResponsesFixture,
    tmp_path: Path,
) -> None:
    log = tmp_path / "elicitation"
    async with timeout(15):
        async with Agent(
            instructions="Read.", tools=[stdio(log, "--elicit")], provider=provider(responses)
        ).session() as session:
            with pytest.raises(
                (RuntimeError, NativeError), match="interaction|active turn|startup"
            ):
                await session.read_mcp_resource("docs", URI)
        await assert_stopped(log)
        assert not responses.requests


def test_resource_types_are_exported_from_both_public_modules() -> None:
    from agents import mcp

    for name in (
        "MCPResource",
        "MCPResourceTemplate",
        "MCPTextResourceContents",
        "MCPBlobResourceContents",
        "MCPListResourcesResult",
        "MCPListResourceTemplatesResult",
        "MCPReadResourceResult",
    ):
        assert name in agents.__all__
        assert getattr(agents, name) is getattr(mcp, name)
