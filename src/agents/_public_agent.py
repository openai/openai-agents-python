"""Helpers for preserving the user-visible agent identity during execution rewrites."""

from __future__ import annotations

from .agent import Agent

_PUBLIC_AGENT_ATTR = "_agents_public_agent"


def set_public_agent(execution_agent: Agent, public_agent: Agent) -> Agent:
    """Tag an execution-only clone with the agent identity exposed to hooks and results."""
    setattr(execution_agent, _PUBLIC_AGENT_ATTR, public_agent)
    return execution_agent


def get_public_agent(agent: Agent) -> Agent:
    """Return the user-visible agent identity for hooks, tool execution, and results."""
    public_agent = getattr(agent, _PUBLIC_AGENT_ATTR, None)
    if isinstance(public_agent, Agent):
        return public_agent
    return agent
