from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import cast

from .._public_agent import get_public_agent, set_public_agent
from ..agent import Agent
from ..run_context import RunContextWrapper, TContext
from .capabilities import Capability
from .manifest import Manifest
from .sandbox_agent import SandboxAgent
from .session.base_sandbox_session import BaseSandboxSession


def clone_capabilities(capabilities: list[Capability]) -> list[Capability]:
    return [capability.clone() for capability in capabilities]


def prepare_sandbox_agent(
    *,
    agent: SandboxAgent[TContext],
    session: BaseSandboxSession,
    capabilities: list[Capability],
) -> Agent[TContext]:
    manifest = session.state.manifest
    for capability in capabilities:
        capability.bind(session)

    capability_tools = [tool for capability in capabilities for tool in capability.tools()]
    prepared_agent = agent.clone(
        instructions=build_sandbox_instructions(
            agent.instructions,
            agent.developer_instructions,
            capabilities,
            manifest,
        ),
        tools=[*agent.tools, *capability_tools],
        capabilities=capabilities,
    )
    set_public_agent(prepared_agent, agent)
    return prepared_agent


def build_sandbox_instructions(
    base_instructions: str
    | Callable[[RunContextWrapper[TContext], Agent[TContext]], Awaitable[str | None] | str | None]
    | None,
    developer_instructions: str | None,
    capabilities: list[Capability],
    manifest: Manifest | None,
) -> Callable[[RunContextWrapper[TContext], Agent[TContext]], Awaitable[str | None]]:
    async def _instructions(
        run_context: RunContextWrapper[TContext],
        current_agent: Agent[TContext],
    ) -> str | None:
        parts: list[str] = []
        public_agent = cast(Agent[TContext], get_public_agent(current_agent))

        base = await resolve_base_instructions(
            instructions=base_instructions,
            run_context=run_context,
            agent=public_agent,
        )
        if base:
            parts.append(base)

        if developer_instructions:
            parts.append(developer_instructions)

        if manifest is not None:
            for capability in capabilities:
                fragment = await capability.instructions(manifest)
                if fragment:
                    parts.append(fragment)

        return "\n\n".join(parts) if parts else None

    return _instructions


async def resolve_base_instructions(
    *,
    instructions: str
    | Callable[[RunContextWrapper[TContext], Agent[TContext]], Awaitable[str | None] | str | None]
    | None,
    run_context: RunContextWrapper[TContext],
    agent: Agent[TContext],
) -> str | None:
    if isinstance(instructions, str):
        return instructions
    if callable(instructions):
        result = instructions(run_context, agent)
        if inspect.isawaitable(result):
            return await result
        return result
    return None
