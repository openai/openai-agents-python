"""Compose the native executor with official A2A routes and host infrastructure.

Install a2a-sdk[http-server]==1.3.0. The application supplies a NativeA2AStore
and authenticated ServerCallContextBuilder. This module does not create tokens,
a database or a recovery scheduler. See README.md for the storage contract.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager

from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.routes.common import ServerCallContextBuilder
from a2a.types import AgentCard
from starlette.applications import Starlette

from agents.a2a import (
    NativeA2AAgent,
    NativeA2ABinding,
    NativeA2AExecutor,
    NativeA2AStore,
)


def create_app(
    *,
    agent_factory: Callable[[NativeA2ABinding, ServerCallContext], NativeA2AAgent],
    store: NativeA2AStore,
    card: AgentCard,
    context_builder: ServerCallContextBuilder,
    rpc_path: str = "/rpc",
) -> Starlette:
    """The context builder must derive identity from authenticated host state.

    Authenticate requests before the A2A routes run. Do not copy caller-selected
    identity, tenant or Agent configuration from protocol metadata. Return current
    callbacks, application context and optional parent trace from agent_factory.
    """
    executor = NativeA2AExecutor(agent_factory=agent_factory, store=store)
    handler = DefaultRequestHandler(executor, store, card)

    @asynccontextmanager
    async def lifespan(app: Starlette):
        # The host must reconcile retained publications before accepting traffic,
        # with the previous owner and handler stopped. Never replay native work.
        yield
        await handler.aclose()

    return Starlette(
        routes=[
            *create_agent_card_routes(card),
            *create_jsonrpc_routes(
                handler, rpc_path, context_builder=context_builder, enable_v0_3_compat=False
            ),
        ],
        lifespan=lifespan,
    )
