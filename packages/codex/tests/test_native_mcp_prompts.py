"""Caller-owned MCP prompts through public Session APIs and native transports."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

import agents
from agents import Agent, HttpMCP, NativeError, SessionBusyError, StdioMCP
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.native_mcp_prompt_fixtures import CURSOR, PROMPT, TEXT, prompt_result
from tests.test_native_agent import provider
from tests.test_native_mcp_stdio import assert_stopped, records
from tests.test_native_mcp_tools import Peer, declarations

PROMPT_BYTE_LIMIT = 8 * 1024 * 1024


@dataclass
class PromptPeer(Peer):
    prompts: bool = True
    tools: bool = False
    mode: str = "normal"
    page_count: int = 0
    catalog_pages: int = 4
    later_entered: asyncio.Event = field(default_factory=asyncio.Event)
    release_later: asyncio.Event | None = None

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json()
        method = body.get("method", "")
        if method != "initialize" and not method.startswith("prompts/"):
            return await super().handle(request)
        self.requests.append(body)
        result: dict[str, Any]
        if method == "initialize":
            self.ready_entered.set()
            if self.release_ready is not None:
                await self.release_ready.wait()
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {
                    **({"prompts": {}} if self.prompts else {}),
                    **({"tools": {}} if self.tools else {}),
                },
                "serverInfo": {"name": "prompt-fixture", "version": "1"},
            }
        else:
            params = body.get("params", {})
            if self.mode == "oversized-response":
                assert request.headers.get("MCP-Protocol-Version") == "2025-06-18"
                payload = bytearray(
                    b'{"jsonrpc":"2.0","id":' + json.dumps(body["id"]).encode() + b',"result":'
                )
                if method == "prompts/list":
                    payload.extend(b'{"prompts":[{"name":"large","_meta":{"padding":"')
                else:
                    payload.extend(b'{"messages":[{"role":"user","content":{"type":"text","text":"')
                # Build just over 8 MiB without retaining multiple large JSON strings.
                chunk = b"x" * (64 * 1024)
                for _ in range(PROMPT_BYTE_LIMIT // len(chunk)):
                    payload.extend(chunk)
                payload.extend(b'"}}]}}')
                return web.Response(body=payload, content_type="application/json")
            if method == "prompts/list":
                self.page_count += 1
                if "cursor" in params:
                    self.later_entered.set()
                    if self.release_later is not None:
                        await self.release_later.wait()
                if self.mode == "error" and "cursor" in params:
                    return web.json_response(
                        {
                            "jsonrpc": "2.0",
                            "id": body["id"],
                            "error": {"code": -32002, "message": "Prompt page failed"},
                        }
                    )
                if self.mode == "repeated":
                    result = {"prompts": [PROMPT], "nextCursor": "again"}
                elif self.mode == "cycle":
                    result = {"prompts": [], "nextCursor": "a" if self.page_count % 2 else "b"}
                elif self.mode in ("100", "101"):
                    result = {"prompts": [{"name": str(self.page_count)}]}
                    if self.page_count < int(self.mode):
                        result["nextCursor"] = str(self.page_count)
                elif self.mode == "items":
                    result = {"prompts": [{"name": "many"}] * 2049}
                elif self.mode == "cursor":
                    result = {"prompts": [], "nextCursor": "x" * (64 * 1024 + 1)}
                elif self.mode == "catalog-bytes":
                    result = {
                        "prompts": [
                            {
                                "name": str(self.page_count),
                                "_meta": {"padding": "x" * (3 * 1024 * 1024)},
                            }
                        ]
                    }
                    if self.page_count < self.catalog_pages:
                        result["nextCursor"] = str(self.page_count)
                elif self.mode == "malformed" and "cursor" in params:
                    result = {"prompts": [{"description": "missing name"}]}
                else:
                    result = prompt_result(method, params)
            elif params["name"] == "missing":
                return web.json_response(
                    {
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "error": {"code": -32602, "message": "Prompt missing"},
                    }
                )
            else:
                result = prompt_result(method, params)
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": result})


def stdio(log: Path, *args: str) -> StdioMCP[Any]:
    return StdioMCP(
        name="catalog",
        command=sys.executable,
        tools=[],
        args=(
            "-I",
            "-B",
            str(Path(__file__).with_name("native_mcp_prompt_fixtures.py")),
            "--log",
            str(log),
            *args,
        ),
    )


@pytest.mark.parametrize("transport", ["http", "stdio"])
async def test_prompts_are_complete_rich_caller_data(
    responses: ResponsesFixture,
    tmp_path: Path,
    transport: str,
) -> None:
    peer = PromptPeer()
    log = tmp_path / "prompts"
    async with timeout(30), peer.serve() as url:
        server = HttpMCP(name="catalog", url=url, tools=[]) if transport == "http" else stdio(log)
        async with Agent(
            instructions="Answer normally.", tools=[server], provider=provider(responses)
        ).session() as session:
            before = (await session.model_context()).items
            assert await session.list_mcp_prompts("catalog") == [
                PROMPT,
                PROMPT,
                {"name": "welcome"},
            ]
            for name, args in (
                ("review", None),
                ("", {}),
                (" padded ☃ ", {"language": " 日本語 "}),
                ("complete", None),
            ):
                params: dict[str, Any] = {"name": name}
                if args is not None:
                    params["arguments"] = args
                assert await session.get_mcp_prompt("catalog", name, args) == prompt_result(
                    "prompts/get", params
                )
            assert (await session.model_context()).items == before
            assert not responses.requests
            requests = (
                peer.requests
                if transport == "http"
                else [row["message"] for row in records(log) if row["event"] == "request"]
            )
            listed = [
                row.get("params", {}).get("cursor")
                for row in requests
                if row.get("method") == "prompts/list"
            ]
            assert listed == [None, "", CURSOR]
            assert sum(row.get("method") == "initialize" for row in requests) == 1
            assert not any(row.get("method") == "tools/list" for row in requests)
            responses.respond(assistant_output("done"))
            assert (await (await session.submit("Hello")).get_result()).final_output == "done"
            assert TEXT not in json.dumps(responses.requests)
            assert not declarations(responses.requests[0])
        if transport == "stdio":
            await assert_stopped(log)


@pytest.mark.parametrize(
    ("mode", "message", "pages"),
    [
        ("error", "Prompt page failed", 2),
        ("repeated", "repeated.*cursor", 2),
        ("cycle", "repeated.*cursor", 3),
        ("101", "pagination limit.*100", 100),
        ("items", "catalog limit.*2048", 1),
        ("cursor", "cursor.*65536", 1),
        ("malformed", "Unexpected response type", 2),
    ],
)
async def test_prompt_listing_fails_without_partial_inventory(
    responses: ResponsesFixture,
    mode: str,
    message: str,
    pages: int,
) -> None:
    peer = PromptPeer(mode=mode)
    async with timeout(30), peer.serve() as url:
        async with Agent(
            instructions="Preview.",
            tools=[HttpMCP(name="catalog", url=url, tools=[])],
            provider=provider(responses),
        ).session() as session:
            with pytest.raises(NativeError, match=message):
                await session.list_mcp_prompts("catalog")
            assert peer.page_count == pages
            assert not responses.requests


async def test_terminal_page_100_succeeds(responses: ResponsesFixture) -> None:
    peer = PromptPeer(mode="100")
    async with timeout(30), peer.serve() as url:
        async with Agent(
            instructions="Preview.",
            tools=[HttpMCP(name="catalog", url=url, tools=[])],
            provider=provider(responses),
        ).session() as session:
            assert await session.list_mcp_prompts("catalog") == [
                {"name": str(index)} for index in range(1, 101)
            ]
            assert peer.page_count == 100


@pytest.mark.parametrize("pages", [2, 4])
async def test_prompt_catalog_byte_budget_preserves_session_usability(
    responses: ResponsesFixture, pages: int
) -> None:
    peer = PromptPeer(mode="catalog-bytes", catalog_pages=pages)
    async with timeout(30), peer.serve() as url:
        async with Agent(
            instructions="Preview.",
            tools=[HttpMCP(name="catalog", url=url, tools=[])],
            provider=provider(responses),
        ).session() as session:
            if pages == 2:
                prompts = await session.list_mcp_prompts("catalog")
                assert [prompt["name"] for prompt in prompts] == ["1", "2"]
                assert all(
                    prompt.get("_meta", {}).get("padding") == "x" * (3 * 1024 * 1024)
                    for prompt in prompts
                )
                expected_cursors = [None, "1"]
            else:
                # Each response is below 8 MiB; the third page exceeds the total budget.
                with pytest.raises(
                    NativeError,
                    match=r"prompts/list exceeded the catalog byte limit of 8388608 bytes",
                ):
                    await session.list_mcp_prompts("catalog")
                expected_cursors = [None, "1", "2"]
            peer.mode = "normal"
            assert await session.get_mcp_prompt("catalog", "review") == prompt_result(
                "prompts/get", {"name": "review"}
            )
            assert [
                row.get("params", {}).get("cursor")
                for row in peer.requests
                if row.get("method") == "prompts/list"
            ] == expected_cursors
            assert sum(row.get("method") == "initialize" for row in peer.requests) == 1
            assert not responses.requests


@pytest.mark.parametrize("method", ["prompts/list", "prompts/get"])
async def test_legacy_http_prompt_responses_enforce_byte_limit(
    responses: ResponsesFixture, method: str
) -> None:
    peer = PromptPeer(mode="oversized-response")
    async with timeout(30), peer.serve() as url:
        async with Agent(
            instructions="Preview.",
            tools=[HttpMCP(name="catalog", url=url, tools=[])],
            provider=provider(responses),
        ).session() as session:
            with pytest.raises(NativeError, match=r"MCP response body exceeds\s*8388608\s*bytes"):
                if method == "prompts/list":
                    await session.list_mcp_prompts("catalog")
                else:
                    await session.get_mcp_prompt("catalog", "large")
            assert [
                row["method"]
                for row in peer.requests
                if row.get("method", "").startswith("prompts/")
            ] == [method]
            peer.mode = "normal"
            assert await session.list_mcp_prompts("catalog") == [
                PROMPT,
                PROMPT,
                {"name": "welcome"},
            ]
            assert await session.get_mcp_prompt("catalog", "review") == prompt_result(
                "prompts/get", {"name": "review"}
            )
            assert sum(row.get("method") == "initialize" for row in peer.requests) == 1
            assert not responses.requests


async def test_missing_capability_and_advertised_get_error(responses: ResponsesFixture) -> None:
    missing, advertised = PromptPeer(prompts=False), PromptPeer()
    async with timeout(20), missing.serve() as absent_url, advertised.serve() as present_url:
        for url, peer in ((absent_url, missing), (present_url, advertised)):
            async with Agent(
                instructions="Preview.",
                tools=[HttpMCP(name="catalog", url=url, tools=[])],
                provider=provider(responses),
            ).session() as session:
                if peer is missing:
                    assert await session.list_mcp_prompts("catalog") == []
                    expected = "unsupported"
                else:
                    expected = "Prompt missing"
                with pytest.raises(NativeError, match=expected):
                    await session.get_mcp_prompt("catalog", "missing")
                assert not any(row.get("method") == "prompts/list" for row in advertised.requests)
        assert not any(row.get("method", "").startswith("prompts/") for row in missing.requests)


async def test_arguments_copy_before_await(
    responses: ResponsesFixture,
) -> None:
    release = asyncio.Event()
    peer = PromptPeer(release_ready=release)
    async with timeout(20), peer.serve() as url:
        async with Agent(
            instructions="Preview.",
            tools=[HttpMCP(name="catalog", url=url, tools=[])],
            provider=provider(responses),
        ).session() as session:
            args = {"language": "before"}
            pending = asyncio.create_task(session.get_mcp_prompt("catalog", "review", args))
            await peer.ready_entered.wait()
            args["language"] = "after"
            release.set()
            rendered = await pending
            assert "_meta" in rendered
            assert rendered["_meta"]["request"]["arguments"] == {"language": "before"}
            assert not any(row.get("method") == "prompts/list" for row in peer.requests)


@pytest.mark.parametrize("ending", ["cancel", "close"])
async def test_later_page_owns_mutation_and_joins_shutdown(
    responses: ResponsesFixture,
    tmp_path: Path,
    ending: str,
) -> None:
    log = tmp_path / "held"
    async with timeout(20):
        async with Agent(
            instructions="Preview.", tools=[stdio(log, "--hold")], provider=provider(responses)
        ).session() as session:
            pending = asyncio.create_task(session.list_mcp_prompts("catalog"))
            try:
                while not any(
                    row.get("message", {}).get("params", {}).get("cursor") == ""
                    for row in records(log)
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


async def test_idle_prompt_elicitation_rejects_and_joins(
    responses: ResponsesFixture, tmp_path: Path
) -> None:
    log = tmp_path / "elicit"
    async with timeout(20):
        async with Agent(
            instructions="Preview.", tools=[stdio(log, "--elicit")], provider=provider(responses)
        ).session() as session:
            with pytest.raises(
                (RuntimeError, NativeError), match="interaction|active turn|startup"
            ):
                await session.get_mcp_prompt("catalog", "review")
        await assert_stopped(log)


async def test_prompt_selection_restore_uses_current_declarations(
    responses: ResponsesFixture,
) -> None:
    first, sibling, latest = PromptPeer(), PromptPeer(), PromptPeer(tools=True)
    async with (
        timeout(30),
        first.serve() as first_url,
        sibling.serve() as sibling_url,
        latest.serve() as latest_url,
    ):
        descriptor = HttpMCP(name="catalog", url=first_url, tools=[])
        other = HttpMCP(name="sibling", url=sibling_url, tools=[])
        async with Agent(
            instructions="Preview.", tools=[descriptor, other], provider=provider(responses)
        ).session() as session:
            assert not first.requests and not sibling.requests
            await session.list_mcp_prompts("catalog")
            assert not sibling.requests
            with pytest.raises(ValueError, match="currently selected"):
                await session.get_mcp_prompt("unknown", "review")
            await session.list_mcp_prompts("sibling")
            await session.get_mcp_prompt("catalog", "review")
            assert sum(row.get("method") == "initialize" for row in first.requests) == 1
            # Explicit selections persist; unchanged defaults come from the restoring Agent.
            await session.update(tools=[descriptor, other])
            responses.respond(assistant_output("done"))
            await (await session.submit("Hello")).get_result()
            state = await session.save_state()
            await session.update(tools=[])
            with pytest.raises(ValueError, match="currently selected"):
                await session.list_mcp_prompts("catalog")
            deselected = await session.save_state()
        current = HttpMCP(name="catalog", url=latest_url, tools=["search"])
        async with Agent(
            instructions="Preview.", tools=[current, other], provider=provider(responses)
        ).session(state=state) as restored:
            await restored.get_mcp_prompt("catalog", "review")
            assert any(row.get("method") == "prompts/get" for row in latest.requests)
            responses.respond(assistant_output("restored"))
            await (await restored.submit("Hello again")).get_result()
            assert not declarations(responses.requests[-1])
        count = len(latest.requests)
        async with Agent(
            instructions="Preview.", tools=[current, other], provider=provider(responses)
        ).session(state=deselected) as restored:
            with pytest.raises(ValueError, match="currently selected"):
                await restored.list_mcp_prompts("catalog")
        assert len(latest.requests) == count


async def test_prompt_collection_timeout_fails_on_a_later_page(responses: ResponsesFixture) -> None:
    release = asyncio.Event()
    peer = PromptPeer(release_later=release)
    async with timeout(15), peer.serve() as url:
        try:
            async with Agent(
                instructions="Preview.",
                tools=[HttpMCP(name="catalog", url=url, tools=[], execution_timeout=0.2)],
                provider=provider(responses),
            ).session() as session:
                with pytest.raises(NativeError, match="timed out"):
                    await session.list_mcp_prompts("catalog")
                assert peer.later_entered.is_set()
                assert not responses.requests
        finally:
            release.set()


async def test_cancel_prompt_activation_joins_withheld_initialize(
    responses: ResponsesFixture,
) -> None:
    release = asyncio.Event()
    peer = PromptPeer(release_ready=release)
    async with timeout(20), peer.serve() as url:
        try:
            async with Agent(
                instructions="Preview.",
                tools=[HttpMCP(name="catalog", url=url, tools=[])],
                provider=provider(responses),
            ).session() as session:
                pending = asyncio.create_task(session.list_mcp_prompts("catalog"))
                await peer.ready_entered.wait()
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(pending), 5)
                assert not release.is_set()
                assert not any(
                    row.get("method", "").startswith("prompts/") for row in peer.requests
                )
        finally:
            release.set()


def test_prompt_types_have_public_identity() -> None:
    from agents import mcp

    for name in (
        "MCPPrompt",
        "MCPPromptArgument",
        "MCPPromptMessage",
        "MCPPromptContent",
        "MCPGetPromptResult",
        "MCPPromptTextContent",
        "MCPPromptImageContent",
        "MCPPromptAudioContent",
        "MCPPromptResourceContent",
        "MCPPromptResourceLink",
    ):
        assert name in agents.__all__
        assert getattr(agents, name) is getattr(mcp, name)
