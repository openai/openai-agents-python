"""MCP Apps retain their native call and selected connection authority."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiohttp import web

from agents import Agent, HttpMCP, OutputValidationError
from agents.mcp_apps import MCPApp
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_agent import provider
from tests.test_native_mcp_tools import Peer, mcp_call

HTML = '<!doctype html><html><body><button id="increment">Increment</button></body></html>'
RESOURCE = {"uri": "ui://counter/app", "mimeType": "text/html;profile=mcp-app", "text": HTML}


def app_result() -> dict[str, Any]:
    return {
        "content": [{"type": "resource", "resource": dict(RESOURCE)}],
        "structuredContent": {"count": 1},
        "isError": False,
    }


class LinkedAppPeer(Peer):
    resource: dict[str, Any] = RESOURCE

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("method") == "resources/read":
            self.requests.append(body)
            assert body["params"]["uri"] == self.resource["uri"]
            return web.json_response(
                {"jsonrpc": "2.0", "id": body["id"], "result": {"contents": [self.resource]}}
            )
        response = await super().handle(request)
        if body.get("method") == "initialize":
            import json

            result = json.loads(response.body)
            result["result"]["capabilities"]["resources"] = {}
            return web.json_response(result)
        return response


async def test_linked_resource_uses_the_existing_native_connection(responses: ResponsesFixture):
    peer = LinkedAppPeer(
        call_result={
            "content": [
                {
                    "type": "resource_link",
                    "uri": RESOURCE["uri"],
                    "name": "Counter",
                    "mimeType": RESOURCE["mimeType"],
                }
            ]
        }
    )
    responses.respond(mcp_call("search", "mcp__counter", "linked-call"))
    responses.respond(assistant_output("Counter opened."))
    async with timeout(20), peer.serve() as url:
        declaration = HttpMCP(
            name="counter", url=url, tools=["search"], approval_mode="preapproved"
        )
        async with Agent(
            instructions="Open counter.", provider=provider(responses), tools=[declaration]
        ).session() as session:
            turn = await session.submit("Open")
            await turn.get_result()
            app = await MCPApp.open(session, turn, "linked-call")
            assert app.view["resource"] == RESOURCE
            assert sum(request.get("method") == "initialize" for request in peer.requests) == 1
            assert sum(request.get("method") == "resources/read" for request in peer.requests) == 1


async def test_embedded_app_view_is_copied_and_rejects_other_tools(responses: ResponsesFixture):
    peer = Peer(call_result=app_result())
    responses.respond(mcp_call("search", "mcp__counter", "counter-call"))
    responses.respond(assistant_output("Counter opened."))
    async with timeout(20), peer.serve() as url:
        agent = Agent(
            instructions="Open counter.",
            provider=provider(responses),
            tools=[
                HttpMCP(
                    name="counter",
                    url=url,
                    tools=["search", "excluded"],
                    approval_mode="preapproved",
                )
            ],
        )
        async with agent.session() as session:
            turn = await session.submit("Open")
            await turn.get_result()
            app = await MCPApp.open(session, turn, "counter-call")
            assert app.view == {
                "callId": "counter-call",
                "toolName": "search",
                "arguments": {"key": "43"},
                "result": app_result(),
                "resource": RESOURCE,
            }
            detached = app.view
            detached["resource"]["text"] = "changed"
            assert app.view["resource"]["text"] == HTML
            with pytest.raises(ValueError, match="originating tool"):
                await app.call_tool("excluded", {"key": "44"})
            app.close()
            with pytest.raises(ValueError, match="closed or stale"):
                await app.call_tool("search", {"key": "44"})
            assert len(peer.calls) == 1


async def test_native_app_action_reuses_connection_and_correlates_result(
    responses: ResponsesFixture,
):
    peer = Peer(call_result=app_result())
    responses.respond(mcp_call("search", "mcp__counter", "counter-call"))
    responses.respond(assistant_output("Counter opened."))
    async with timeout(20), peer.serve() as url:
        agent = Agent(
            instructions="Open counter.",
            provider=provider(responses),
            tools=[HttpMCP(name="counter", url=url, tools=["search"], approval_mode="preapproved")],
        )
        async with agent.session() as session:
            turn = await session.submit("Open")
            await turn.get_result()
            app = await MCPApp.open(session, turn, "counter-call")
            peer.call_result = {
                "content": [{"type": "text", "text": "count=2"}],
                "structuredContent": {"count": 2},
            }
            result = await app.call_tool("search", {"key": "44"})
            assert result["structuredContent"] == {"count": 2}
            assert peer.calls[-1]["name"] == "search"
            assert peer.calls[-1]["arguments"] == {"key": "44"}
            assert sum(request.get("method") == "initialize" for request in peer.requests) == 1
            assert len(responses.requests) == 2  # UI actions do not start inference.


async def test_native_app_action_cancellation_joins_and_retires(responses: ResponsesFixture):
    peer = Peer(call_result=app_result())
    responses.respond(mcp_call("search", "mcp__counter", "counter-call"))
    responses.respond(assistant_output("Counter opened."))
    async with timeout(20), peer.serve() as url:
        async with Agent(
            instructions="Open counter.",
            provider=provider(responses),
            tools=[HttpMCP(name="counter", url=url, tools=["search"], approval_mode="preapproved")],
        ).session() as session:
            turn = await session.submit("Open")
            await turn.get_result()
            app = await MCPApp.open(session, turn, "counter-call")
            peer.release_call = asyncio.Event()
            peer.call_entered.clear()
            action = asyncio.create_task(app.call_tool("search", {"key": "44"}))
            try:
                await peer.call_entered.wait()
                with pytest.raises(RuntimeError, match="busy"):
                    await app.call_tool("search", {"key": "45"})
                action.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await action
                with pytest.raises(RuntimeError):
                    await session.submit("Cannot reuse the retired Session")
                assert len(peer.calls) == 2
            finally:
                peer.release_call.set()
                if not action.done():
                    action.cancel()
                await asyncio.gather(action, return_exceptions=True)


async def test_completed_call_in_failed_turn_cannot_open_app(responses: ResponsesFixture):
    peer = Peer(call_result=app_result())
    responses.respond(mcp_call("search", "mcp__counter", "counter-call"))
    responses.respond(assistant_output("not an integer"))
    async with timeout(20), peer.serve() as url:
        async with Agent(
            instructions="Open counter and return an integer.",
            provider=provider(responses),
            output_type=int,
            tools=[HttpMCP(name="counter", url=url, tools=["search"], approval_mode="preapproved")],
        ).session() as session:
            turn = await session.submit("Open")
            with pytest.raises(OutputValidationError):
                await turn.get_result()
            with pytest.raises(OutputValidationError):
                await MCPApp.open(session, turn, "counter-call")
            assert len(peer.calls) == 1


@pytest.mark.parametrize("variant", ["ambiguous", "wrong-mime", "oversized", "limit"])
async def test_app_resource_admission(responses: ResponsesFixture, variant: str):
    resource = dict(RESOURCE)
    if variant in {"oversized", "limit"}:
        resource["text"] = "é" * (32_769 if variant == "oversized" else 32_768)
    if variant == "wrong-mime":
        resource["mimeType"] = "text/html"
    content = [{"type": "resource", "resource": resource}]
    if variant == "ambiguous":
        content *= 2
    peer: Peer
    if variant in {"oversized", "limit"}:
        peer = LinkedAppPeer(
            call_result={
                "content": [
                    {
                        "type": "resource_link",
                        "name": "Counter",
                        "uri": RESOURCE["uri"],
                        "mimeType": RESOURCE["mimeType"],
                    }
                ]
            }
        )
        peer.resource = resource
    else:
        peer = Peer(call_result={"content": content})
    responses.respond(mcp_call("search", "mcp__counter", "counter-call"))
    responses.respond(assistant_output("Counter opened."))
    async with timeout(20), peer.serve() as url:
        async with Agent(
            instructions="Open counter.",
            provider=provider(responses),
            tools=[HttpMCP(name="counter", url=url, tools=["search"], approval_mode="preapproved")],
        ).session() as session:
            turn = await session.submit("Open")
            await turn.get_result()
            if variant == "limit":
                app = await MCPApp.open(session, turn, "counter-call")
                assert app.view["resource"] == resource
            else:
                with pytest.raises(ValueError, match="exactly one UI|64 KiB"):
                    await MCPApp.open(session, turn, "counter-call")
            assert len(peer.calls) == 1


async def test_later_turn_and_tool_update_revoke_app(responses: ResponsesFixture):
    peer = Peer(call_result=app_result())
    responses.respond(mcp_call("search", "mcp__counter", "counter-call"))
    responses.respond(assistant_output("Counter opened."))
    responses.respond(assistant_output("Later turn."))
    async with timeout(20), peer.serve() as url:
        declaration = HttpMCP(
            name="counter", url=url, tools=["search"], approval_mode="preapproved"
        )
        async with Agent(
            instructions="Open counter.", provider=provider(responses), tools=[declaration]
        ).session() as session:
            turn = await session.submit("Open")
            await turn.get_result()
            app = await MCPApp.open(session, turn, "counter-call")
            await session.update(tools=[declaration])
            with pytest.raises(ValueError, match="closed or stale"):
                await app.call_tool("search", {"key": "44"})
            with pytest.raises(ValueError, match="fixed opening tools"):
                await MCPApp.open(session, turn, "counter-call")
            await (await session.submit("Later")).get_result()
            with pytest.raises(ValueError, match="closed or stale"):
                await app.call_tool("search", {"key": "44"})
            with pytest.raises(ValueError, match="latest settled"):
                await MCPApp.open(session, turn, "counter-call")
            assert len(peer.calls) == 1


@pytest.mark.parametrize("approved", [False, True])
async def test_app_rejects_native_approval_or_application_policy(
    responses: ResponsesFixture, approved: bool
):
    peer = Peer(call_result=app_result())
    responses.respond(mcp_call("search", "mcp__counter", "counter-call"))
    responses.respond(assistant_output("Counter opened."))
    async with timeout(20), peer.serve() as url:
        options: dict[str, Any] = {"approval_mode": "preapproved" if approved else "native"}
        if approved:
            options["approval"] = lambda context, call: True
        declaration = HttpMCP(name="counter", url=url, tools=["search"], **options)
        async with Agent(
            instructions="Open counter.", provider=provider(responses), tools=[declaration]
        ).session() as session:
            turn = await session.submit("Open")
            await turn.get_result()
            with pytest.raises(ValueError, match="without callbacks"):
                await MCPApp.open(session, turn, "counter-call")
            assert len(peer.calls) == 1
