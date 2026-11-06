from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING, Any

from ..exceptions import UserError
from ..tool import Tool

if TYPE_CHECKING:
    from ..agent import AgentBase


@dataclass(eq=False)
class _ToolResolution:
    agent: AgentBase[Any]
    initial_tools: tuple[Tool, ...]
    tools_assigned: bool = False
    conflicted: bool = False


@dataclass
class _ToolConfigurationRun:
    resolutions: dict[int, _ToolResolution] = field(default_factory=dict)
    closed: bool = False


_current_run: ContextVar[_ToolConfigurationRun | None] = ContextVar(
    "agent_tool_configuration_run", default=None
)
_resolutions: dict[int, list[_ToolResolution]] = {}
_resolution_lock = Lock()


def _observe_tools(active: list[_ToolResolution], tools: tuple[Tool, ...]) -> None:
    if len(active) > 1 and any(
        resolution.tools_assigned
        or len(resolution.initial_tools) != len(tools)
        or any(
            before is not after
            for before, after in zip(resolution.initial_tools, tools, strict=False)
        )
        for resolution in active
    ):
        for resolution in active:
            resolution.conflicted = True


def _raise_conflict() -> None:
    raise UserError(
        "Agent.tools changed while concurrent runs were using the same Agent. "
        "Use a separate agent per run, for example agent.clone(tools=[*agent.tools]), "
        "or keep Agent.tools unchanged and use context-based tool enablement."
    )


@contextmanager
def agent_tool_configuration_run(agent: AgentBase[Any]) -> Iterator[None]:
    """Own configuration checks across all turns without persisting runtime locks or state."""
    owner = _ToolConfigurationRun()
    token = _current_run.set(owner)
    try:
        register_agent_tool_configuration(agent)
        yield
    finally:
        with _resolution_lock:
            owner.closed = True
            for agent_id, resolution in owner.resolutions.items():
                active = _resolutions[agent_id]
                # Preserve changes from failed/cancelled callbacks for surviving runs.
                _observe_tools(active, tuple(resolution.agent.tools))
                active.remove(resolution)
                if not active:
                    del _resolutions[agent_id]
            owner.resolutions.clear()
        _current_run.reset(token)


def register_agent_tool_configuration(agent: AgentBase[Any]) -> None:
    owner = _current_run.get()
    if owner is None:
        return
    agent_id = id(agent)
    with _resolution_lock:
        if owner.closed or agent_id in owner.resolutions:
            return
        active = _resolutions.setdefault(agent_id, [])
        resolution = _ToolResolution(agent, tuple(agent.tools))
        active.append(resolution)
        owner.resolutions[agent_id] = resolution
        _observe_tools(active, resolution.initial_tools)


def assign_agent_tools(agent: AgentBase[Any], tools: list[Tool]) -> None:
    """Retain replacements even if a competing hook restores the original list."""
    with _resolution_lock:
        active = _resolutions.get(id(agent), [])
        if active and tools is not agent.tools:
            for resolution in active:
                resolution.tools_assigned = True
            if len(active) > 1:
                for resolution in active:
                    resolution.conflicted = True
        object.__setattr__(agent, "tools", tools)


def snapshot_agent_tools(agent: AgentBase[Any]) -> tuple[Tool, ...]:
    """Capture one tool sequence and reject conflicts with overlapping resolution scopes."""
    with _resolution_lock:
        tools = tuple(agent.tools)
        active = _resolutions.get(id(agent), [])
        _observe_tools(active, tools)
        conflicted = any(resolution.conflicted for resolution in active)
    if conflicted:
        _raise_conflict()
    return tools


@contextmanager
def guard_agent_tool_configuration(agent: AgentBase[Any]) -> Iterator[None]:
    """Check both sides of hooks/discovery; the run owner retains the record between turns."""
    register_agent_tool_configuration(agent)
    snapshot_agent_tools(agent)
    yield
    snapshot_agent_tools(agent)
