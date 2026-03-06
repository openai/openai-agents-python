"""
Tool-use tracking utilities. Hosts AgentToolUseTracker and helpers to serialize/deserialize
its state plus lightweight tool-call type utilities. Internal use only.
"""

from __future__ import annotations

from typing import Any, get_args, get_origin

from ..agent import Agent
from ..items import ToolCallItemTypes
from ..run_state import _build_agent_map
from .run_steps import ToolRunFunction

__all__ = [
    "AgentToolUseTracker",
    "serialize_tool_use_tracker",
    "hydrate_tool_use_tracker",
    "get_tool_call_types",
    "TOOL_CALL_TYPES",
]


class AgentToolUseTracker:
    """Track which tools an agent has used to support model_settings resets."""

    def __init__(self) -> None:
        # Name-keyed map is used for serialization/hydration only.
        self.agent_map: dict[str, set[str]] = {}
        # Instance-keyed list is used for runtime checks.
        self.agent_to_tools: list[tuple[Agent[Any], list[str]]] = []

    def record_used_tools(self, agent: Agent[Any], tools: list[ToolRunFunction]) -> None:
        tool_names = [tool.function_tool.name for tool in tools]
        self.add_tool_use(agent, tool_names)

    def add_tool_use(self, agent: Agent[Any], tool_names: list[str]) -> None:
        """Maintain compatibility for callers that append tool usage directly."""
        agent_name = getattr(agent, "name", agent.__class__.__name__)
        names_set = self.agent_map.setdefault(agent_name, set())
        names_set.update(tool_names)

        existing = next((item for item in self.agent_to_tools if item[0] is agent), None)
        if existing:
            existing[1].extend(tool_names)
        else:
            self.agent_to_tools.append((agent, list(tool_names)))

    def has_used_tools(self, agent: Agent[Any]) -> bool:
        existing = next((item for item in self.agent_to_tools if item[0] is agent), None)
        return bool(existing and existing[1])

    def as_serializable(self) -> dict[str, list[str]]:
        if self.agent_map:
            return {name: sorted(tool_names) for name, tool_names in self.agent_map.items()}

        snapshot: dict[str, set[str]] = {}
        for agent, names in self.agent_to_tools:
            agent_name = getattr(agent, "name", agent.__class__.__name__)
            snapshot.setdefault(agent_name, set()).update(names)
        return {name: sorted(tool_names) for name, tool_names in snapshot.items()}

    @classmethod
    def from_serializable(cls, data: dict[str, list[str]]) -> AgentToolUseTracker:
        tracker = cls()
        tracker.agent_map = {name: set(tools) for name, tools in data.items()}
        return tracker


def serialize_tool_use_tracker(tool_use_tracker: AgentToolUseTracker) -> dict[str, list[str]]:
    """Convert the AgentToolUseTracker into a serializable snapshot."""
    snapshot: dict[str, list[str]] = {}
    for agent, tool_names in tool_use_tracker.agent_to_tools:
        snapshot[agent.name] = list(tool_names)
    return snapshot


def hydrate_tool_use_tracker(
    tool_use_tracker: AgentToolUseTracker,
    run_state: Any,
    starting_agent: Agent[Any],
) -> None:
    """Seed a fresh AgentToolUseTracker using the snapshot stored on the RunState."""
    snapshot = run_state.get_tool_use_tracker_snapshot()
    if not snapshot:
        return

    agent_map = _build_agent_map(starting_agent)
    for agent_name, tool_names in snapshot.items():
        agent = agent_map.get(agent_name)
        if agent is None:
            continue
        tool_use_tracker.add_tool_use(agent, list(tool_names))


def get_tool_call_types() -> tuple[type, ...]:
    """Return the concrete classes that represent tool call outputs."""
    normalized_types: list[type] = []
    for type_hint in get_args(ToolCallItemTypes):
        origin = get_origin(type_hint)
        candidate = origin or type_hint
        if isinstance(candidate, type):
            normalized_types.append(candidate)
    return tuple(normalized_types)


TOOL_CALL_TYPES: tuple[type, ...] = get_tool_call_types()
