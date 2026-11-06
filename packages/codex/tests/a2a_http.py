"""Loopback ASGI peer with synthetic host authentication for adapter tests."""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager

import uvicorn
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.routes.common import ServerCallContextBuilder
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from google.protobuf.json_format import MessageToDict
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from tests.a2a_helpers import Principal


class Authentication:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/rpc":
            headers = dict(scope["headers"])
            principal = {b"Bearer alice-token": "alice", b"Bearer bob-token": "bob"}.get(
                headers.get(b"authorization")
            )
            if principal is None:
                await JSONResponse({"error": "Unauthorized"}, status_code=401)(scope, receive, send)
                return
            scope["a2a_principal"] = principal
        await self.app(scope, receive, send)


class Context(ServerCallContextBuilder):
    def build(self, request):
        return ServerCallContext(
            user=Principal(request.scope["a2a_principal"]),
            state={
                "headers": {"a2a-version": request.headers.get("a2a-version", "")},
                "parent_trace": {
                    key: request.headers[key]
                    for key in ("traceparent", "tracestate")
                    if key in request.headers
                },
            },
        )


def card(base_url, *, rpc_url=None):
    return AgentCard(
        name="Synthetic specialist",
        description="Protocol integration test",
        version="1",
        supported_interfaces=[
            AgentInterface(
                url=rpc_url if rpc_url is not None else base_url + "/rpc",
                protocol_binding="JSONRPC",
                protocol_version="1.0",
            )
        ],
        skills=[
            AgentSkill(
                id="specialist", name="Specialist", description="Synthetic task", tags=["test"]
            )
        ],
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain", "application/json"],
    )


def rpc_service(base_url, rpc, *, discovery=None):
    """Serve a synthetic RPC handler with ordinary discovery by default."""
    if discovery is None:

        def discovery(request):
            return JSONResponse(MessageToDict(card(base_url)))

    return Starlette(
        routes=[
            Route("/.well-known/agent-card.json", discovery),
            Route("/rpc", rpc, methods=["POST"]),
        ]
    )


def service(executor, store, base_url):
    agent_card = card(base_url)
    handler = DefaultRequestHandler(executor, store, agent_card)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await handler.aclose()

    return Starlette(
        routes=[
            *create_agent_card_routes(agent_card),
            *create_jsonrpc_routes(
                handler, "/rpc", context_builder=Context(), enable_v0_3_compat=False
            ),
        ],
        middleware=[Middleware(Authentication)],
        lifespan=lifespan,
    )


@asynccontextmanager
async def serve(factory):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    base_url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(factory(base_url), log_level="error", lifespan="on"))
    running = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(1000):
            if server.started:
                break
            if running.done():
                await running
                raise RuntimeError("Test server exited before startup")
            await asyncio.sleep(0.01)
        else:
            raise RuntimeError("Test server did not start")
        yield base_url
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(running), 10)
        finally:
            if not running.done():
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            sock.close()
