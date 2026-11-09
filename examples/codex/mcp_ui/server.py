"""Run a synthetic native model/MCP counter on loopback only."""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
from contextlib import AsyncExitStack
from pathlib import Path

from aiohttp import web

from agents import Agent, HttpMCP, NativeProvider
from agents.mcp_apps import MCPApp
from agents.ui import vercel_stream

ROOT = Path(__file__).parent
STATE = web.AppKey("fixture_state", dict)


def create_app() -> web.Application:
    state = {"count": 0, "app": None, "token": None, "started": False}
    owner = secrets.token_urlsafe(24)

    @web.middleware
    async def boundary(request, handler):
        if request.host != state.get("host"):
            raise web.HTTPForbidden(text="Unexpected host")
        if request.path.startswith("/api/"):
            if request.cookies.get("synthetic-owner") != owner:
                raise web.HTTPForbidden(text="Choose the local synthetic user first")
            if request.method == "POST" and (
                request.headers.get("Origin") != state["origin"]
                or request.headers.get("X-MCP-Example") != "1"
            ):
                raise web.HTTPForbidden(text="Unexpected action origin")
        return await handler(request)

    app = web.Application(middlewares=[boundary], client_max_size=8192)

    async def index(request):
        response = web.FileResponse(ROOT / "index.html")
        response.set_cookie("synthetic-owner", owner, httponly=True, samesite="Strict")
        return response

    async def asset(request):
        response = web.FileResponse(ROOT / "dist" / request.match_info["asset"])
        # The opaque sandbox can load this public, synthetic fixture script.
        if request.match_info["asset"] == "app.js":
            response.headers["Access-Control-Allow-Origin"] = "*"
        return response

    def result():
        html = (
            '<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            f"script-src {state['origin']}; connect-src 'none'\">"
            '<h1>Counter</h1><output id="count">Loading</output>'
            '<button id="increment">Increment</button><pre id="error"></pre>'
            f'<script type="module" src="{state["origin"]}/app.js"></script></html>'
        )
        return {
            "content": [
                {
                    "type": "resource",
                    "resource": {
                        "uri": "ui://counter/app",
                        "mimeType": "text/html;profile=mcp-app",
                        "text": html,
                    },
                }
            ],
            "structuredContent": {"count": state["count"]},
            "isError": False,
        }

    async def mcp(request):
        # Native traffic has no browser Origin. The browser cannot call this peer.
        if request.headers.get("Origin") is not None:
            raise web.HTTPForbidden()
        rpc = await request.json()
        method = rpc.get("method")
        if method == "notifications/initialized":
            return web.Response(status=202)
        if method == "initialize":
            value = {
                "protocolVersion": rpc["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "synthetic-counter", "version": "1"},
            }
        elif method == "tools/list":
            value = {
                "tools": [
                    {
                        "name": "increment",
                        "description": "Increment synthetic counter",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"amount": {"type": "integer"}},
                            "required": ["amount"],
                            "additionalProperties": False,
                        },
                    }
                ]
            }
        elif method == "tools/call":
            params = rpc["params"]
            if params.get("name") != "increment" or params.get("arguments") != {"amount": 1}:
                return web.json_response(
                    {
                        "jsonrpc": "2.0",
                        "id": rpc["id"],
                        "error": {"code": -32602, "message": "Only increment(1) is supported"},
                    }
                )
            state["count"] += 1
            value = result()
        elif method == "ping":
            value = {}
        else:
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": rpc.get("id"),
                    "error": {"code": -32601, "message": "Unsupported method"},
                }
            )
        return web.json_response({"jsonrpc": "2.0", "id": rpc["id"], "result": value})

    async def responses(request):
        if request.headers.get("Origin") is not None:
            raise web.HTTPForbidden()
        await request.json()
        state["model_requests"] = state.get("model_requests", 0) + 1
        number = state["model_requests"]
        response_id = f"counter-response-{number}"
        events = [{"type": "response.created", "response": {"id": response_id}}]
        if number == 1:
            events.append(
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "type": "function_call",
                        "call_id": "counter-call",
                        "name": "increment",
                        "namespace": "mcp__counter",
                        "arguments": '{"amount":1}',
                    },
                }
            )
        elif number == 2:
            item = {
                "type": "message",
                "id": "counter-message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Counter opened."}],
            }
            events.extend(
                [
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {**item, "content": []},
                    },
                    {
                        "type": "response.content_part.added",
                        "output_index": 0,
                        "content_index": 0,
                        "item_id": item["id"],
                        "part": {"type": "output_text", "text": ""},
                    },
                    {
                        "type": "response.output_text.delta",
                        "output_index": 0,
                        "content_index": 0,
                        "item_id": item["id"],
                        "delta": "Counter opened.",
                    },
                    {
                        "type": "response.output_text.done",
                        "output_index": 0,
                        "content_index": 0,
                        "item_id": item["id"],
                        "text": "Counter opened.",
                    },
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                ]
            )
        else:
            raise web.HTTPConflict(text="Unexpected model request")
        events.append({"type": "response.completed", "response": {"id": response_id}})
        return web.Response(
            body="".join(f"data: {json.dumps(event)}\n\n" for event in events),
            content_type="text/event-stream",
        )

    async def chat(request):
        if state["started"]:
            raise web.HTTPConflict(text="This fixture supports one counter per process")
        state["started"] = True
        state["view_ready"] = asyncio.get_running_loop().create_future()
        try:
            stack = state["stack"]
            session = await stack.enter_async_context(
                Agent(
                    instructions="Open the counter",
                    provider=NativeProvider(
                        base_url=state["origin"] + "/v1",
                        api_key="synthetic-counter-key",
                        supports_websockets=False,
                    ),
                    tools=[
                        HttpMCP(
                            name="counter",
                            url=state["origin"] + "/mcp",
                            tools=["increment"],
                            approval_mode="preapproved",
                        )
                    ],
                ).session()
            )
            turn = await session.submit("Open synthetic counter")
            response = web.StreamResponse(
                headers={
                    "Content-Type": "text/event-stream",
                    "x-vercel-ai-ui-message-stream": "v1",
                    "Cache-Control": "no-store",
                }
            )
            await response.prepare(request)
            async for chunk in vercel_stream(turn, session=session, include_tool_data=True):
                await response.write(chunk)
            state["app"] = await MCPApp.open(session, turn, "counter-call")
            state["token"] = secrets.token_urlsafe(24)
            state["view_ready"].set_result(None)
        except BaseException as error:
            state["view_ready"].set_exception(error)
            state["view_ready"].exception()
            raise
        await response.write_eof()
        return response

    async def view(request):
        if not state["started"]:
            raise web.HTTPConflict(text="Start the native turn first")
        await asyncio.shield(state["view_ready"])
        return web.json_response(
            {"view": state["app"].view, "token": state["token"]},
            headers={"Cache-Control": "no-store"},
        )

    async def action(request):
        if state["app"] is None or request.headers.get("X-View") != state["token"]:
            raise web.HTTPForbidden(text="Action does not belong to this counter view")
        params = await request.json()
        if not isinstance(params, dict) or set(params) != {"name", "arguments"}:
            raise web.HTTPBadRequest(text="Expected tools/call name and arguments")
        try:
            value = await state["app"].call_tool(params["name"], params["arguments"])
        except (ValueError, TypeError) as error:
            raise web.HTTPBadRequest(text=str(error)) from None
        return web.json_response(value)

    async def cleanup(application):
        async with AsyncExitStack() as stack:
            state["stack"] = stack
            yield
            if state["app"] is not None:
                state["app"].close()

    app.cleanup_ctx.append(cleanup)
    app.router.add_get("/", index)
    app.router.add_get("/{asset:client.js|app.js}", asset)
    app.router.add_post("/mcp", mcp)
    app.router.add_post("/v1/responses", responses)
    app.router.add_post("/api/chat", chat)
    app.router.add_get("/api/view", view)
    app.router.add_post("/api/action", action)
    app[STATE] = state
    return app


async def start(port=0):
    app = create_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    actual = site._server.sockets[0].getsockname()[1]
    app[STATE].update(host=f"127.0.0.1:{actual}", origin=f"http://127.0.0.1:{actual}")
    return runner, app[STATE]["origin"]


async def main(port):
    import asyncio

    runner, url = await start(port)
    print(url, flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    import asyncio

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8081)
    asyncio.run(main(parser.parse_args().port))
