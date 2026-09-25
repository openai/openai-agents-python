"""Helpers for preserving the user-visible agent identity during execution rewrites."""

from __future__ import annotations

from typing import Any, TypeVar, cast

from .agent import Agent, AgentBase

TAgent = TypeVar("TAgent", bound=AgentBase[Any])

_PUBLIC_AGENT_ATTR = "_agents_public_agent"


def set_public_agent(execution_agent: Agent, public_agent: Agent) -> Agent:
    """Tag an execution-only clone with the agent identity exposed to hooks and results."""
    setattr(execution_agent, _PUBLIC_AGENT_ATTR, public_agent)
    return execution_agent


def get_public_agent(agent: TAgent) -> TAgent:
    """Return the user-visible agent identity for hooks, tool execution, and results."""
    public_agent = getattr(agent, _PUBLIC_AGENT_ATTR, None)
    if isinstance(public_agent, Agent):
        return cast(TAgent, public_agent)
    return agent
