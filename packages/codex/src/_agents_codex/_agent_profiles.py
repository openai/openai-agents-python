"""Capture immutable native Agent graphs before opening native resources."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, cast

from ._function_tools import FunctionTool
from .agent import Agent
from .mcp import _MCP
from .native_agents import NativeAgents
from .schema import OutputSpec, _prepare_spec


@dataclass(frozen=True)
class _AgentProfile:
    id: str
    agent: Agent[Any, Any]
    declaration: Agent[Any, Any]
    handoffs: Mapping[str, str]


def _handoff_name(agent: Agent[Any, Any]) -> str:
    if agent.name is None or not agent.name.strip():
        raise ValueError("A handoff target must have a nonempty Agent.name")
    return re.sub(r"[^a-zA-Z0-9_-]", "_", f"transfer_to_{agent.name}").lower()[:64]


def _capture_agent_graph(
    root: Agent[Any, Any], *, root_declaration: Agent[Any, Any] | None = None
) -> tuple[_AgentProfile, ...]:
    """Resolve each target once and retain separate declarations and identities.

    No native resources are opened. Profile IDs are stable for a given ordered
    graph; they are identifiers, not authority to install new declarations.
    """
    agents = [root]
    opening = root if root_declaration is None else root_declaration
    root_output = _prepare_spec(opening.output_type)
    root_roles = tuple(tool for tool in opening.tools if isinstance(tool, NativeAgents))
    runtime_tools = tuple(
        tool for tool in opening.tools if not isinstance(tool, (FunctionTool, _MCP))
    )
    indices = {id(root): 0}
    resolved: dict[int, tuple[Callable[[], Agent[Any, Any]], Agent[Any, Any]]] = {}
    profiles: list[_AgentProfile] = []
    for agent in agents:
        source = opening if agent is root else agent
        declaration = Agent[Any, Any](
            instructions=source.instructions,
            name=source.name,
            tools=source.tools,
            handoffs=source.handoffs,
            model=source.model,
            model_settings=source.model_settings,
            provider=source.provider,
            decisions=source.decisions,
            output_type=cast(OutputSpec[Any], source.output_type),
            programmatic_tool_calling=source.programmatic_tool_calling,
            transform_context=source.transform_context,
            before_compact=source.before_compact,
            before_complete=source.before_complete,
        )
        if declaration.provider is not None and declaration.provider is not opening.provider:
            raise ValueError("Handoff profiles must share the Session provider")
        if declaration.programmatic_tool_calling != opening.programmatic_tool_calling:
            raise ValueError("Handoff profiles must share programmatic tool calling mode")
        output = _prepare_spec(declaration.output_type)
        if (output.mode, output.schema, output.wrapped) != (
            root_output.mode,
            root_output.schema,
            root_output.wrapped,
        ):
            raise ValueError("Handoff profiles must share output mode, schema and wrapping")
        roles = tuple(tool for tool in declaration.tools if isinstance(tool, NativeAgents))
        if roles != root_roles:
            raise ValueError("Handoff profiles must share native child-role configuration")
        if (
            tuple(tool for tool in declaration.tools if not isinstance(tool, (FunctionTool, _MCP)))
            != runtime_tools
        ):
            raise ValueError("Handoff profiles must share native runtime tool configuration")
        targets: dict[str, str] = {}
        for reference in declaration.handoffs:
            if isinstance(reference, Agent):
                target = reference
            else:
                cached = resolved.get(id(reference))
                if cached is None:
                    result = reference()
                    if inspect.isawaitable(result):
                        if inspect.iscoroutine(result):
                            result.close()
                        raise TypeError("Handoff target resolvers must be synchronous")
                    if not isinstance(result, Agent):
                        raise TypeError("A handoff target resolver must return a native Agent")
                    resolved[id(reference)] = (reference, result)
                    target = result
                else:
                    target = cached[1]
            name = _handoff_name(target)
            index = indices.get(id(target))
            if index is None:
                index = len(agents)
                indices[id(target)] = index
                agents.append(target)
            target_id = f"agent_{index}"
            previous = targets.get(name)
            if previous is not None and previous != target_id:
                raise ValueError(f"Duplicate handoff name in Agent {agent.name!r}: {name}")
            targets[name] = target_id
        profiles.append(
            _AgentProfile(
                f"agent_{indices[id(agent)]}", agent, declaration, MappingProxyType(targets)
            )
        )
    return tuple(profiles)
