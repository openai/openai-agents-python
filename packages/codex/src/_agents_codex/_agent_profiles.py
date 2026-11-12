"""Capture immutable native Agent graphs before opening native resources."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, cast

from ._function_tools import FunctionTool
from .agent import Agent
from .handoffs import Handoff
from .mcp import _MCP
from .native_agents import NativeAgents
from .realtime_agents import RealtimeHandoff, _handoff_tool
from .schema import OutputSpec, _prepare_spec
from .tools import ToolRef, ToolSearch


@dataclass(frozen=True)
class _AgentProfile:
    id: str
    agent: Agent[Any, Any]
    declaration: Agent[Any, Any]
    handoffs: Mapping[str, str]
    handoff_callbacks: Mapping[str, int] = field(default_factory=dict)
    handoff_options: Mapping[str, RealtimeHandoff] = field(default_factory=dict)


def _handoff_name(agent: Agent[Any, Any]) -> str:
    if agent.name is None or not agent.name.strip():
        raise ValueError("A handoff target must have a nonempty Agent.name")
    return re.sub(r"[^a-zA-Z0-9_-]", "_", f"transfer_to_{agent.name}").lower()[:64]


def _check_handoff_tool_names(refs: Iterable[ToolRef], handoffs: Mapping[str, str]) -> None:
    if any(ref.namespace in (None, "functions") and ref.name in handoffs for ref in refs):
        raise ValueError("A native handoff name collides with an ordinary function tool")


def _python_tool_slot(tool: object) -> FunctionTool[Any, Any, Any] | None:
    if isinstance(tool, ToolSearch) and tool._manual is not None:
        return tool._manual
    return tool if isinstance(tool, FunctionTool) else None


def _capture_agent_graph(
    root: Agent[Any, Any],
    *,
    root_declaration: Agent[Any, Any] | None = None,
    id_prefix: str = "agent_",
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
        handoff_bindings = {
            id(reference.callback)
            for reference in source.handoffs
            if isinstance(reference, Handoff)
        }
        instructions = (
            source.instructions if getattr(source, "_hosted_instructions_provided", True) else None
        )
        declaration = Agent[Any, Any](
            instructions=cast(str, instructions),
            realtime=source.realtime,
            hosted_prompt=source.hosted_prompt,
            name=source.name,
            tools=tuple(tool for tool in source.tools if id(tool) not in handoff_bindings)
            if getattr(source, "_hosted_tools_provided", True)
            else None,
            handoffs=source.handoffs,
            model=source.model,
            model_settings=source.model_settings,
            provider=source.provider,
            decisions=source.decisions,
            output_type=cast(OutputSpec[Any], source.output_type),
            programmatic_tool_calling=source.programmatic_tool_calling,
            transform_context=source.transform_context,
            before_compact=source.before_compact,
            compaction=source.compaction,
            before_complete=source.before_complete,
            tool_batch_completion=source.tool_batch_completion,
            input_guardrails=source.input_guardrails,
            output_guardrails=source.output_guardrails,
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
        callbacks: dict[str, int] = {}
        options: dict[str, RealtimeHandoff] = {}
        callback_tools: list[FunctionTool[Any, Any, Any]] = []
        python_slots = sum(_python_tool_slot(tool) is not None for tool in declaration.tools)
        for reference in declaration.handoffs:
            if isinstance(reference, Handoff):
                target = reference.target
            elif isinstance(reference, RealtimeHandoff):
                target = reference.agent
            elif isinstance(reference, Agent):
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
            name = (
                reference.callback.name
                if isinstance(reference, Handoff)
                else reference.name
                if isinstance(reference, RealtimeHandoff) and reference.name is not None
                else _handoff_name(target)
            )
            if not name or len(name) > 64 or re.fullmatch(r"[a-zA-Z0-9_-]+", name) is None:
                raise ValueError("A handoff name must contain 1 to 64 letters, digits, '_' or '-'")
            index = indices.get(id(target))
            if index is None:
                index = len(agents)
                indices[id(target)] = index
                agents.append(target)
            target_id = f"{id_prefix}{index}"
            previous = targets.get(name)
            if previous is not None and isinstance(reference, Handoff):
                slot = callbacks.get(name)
                base = python_slots
                if (
                    previous == target_id
                    and slot is not None
                    and callback_tools[slot - base] is reference.callback
                ):
                    continue
            if previous is not None and (
                previous != target_id
                or name in callbacks
                or isinstance(reference, (Handoff, RealtimeHandoff))
            ):
                raise ValueError(f"Duplicate handoff name in Agent {agent.name!r}: {name}")
            targets[name] = target_id
            if isinstance(reference, (Handoff, RealtimeHandoff)):
                if isinstance(reference, RealtimeHandoff):
                    options[name] = reference
                callbacks[name] = python_slots + len(callback_tools)
                callback_tools.append(
                    reference.callback
                    if isinstance(reference, Handoff)
                    else _handoff_tool(reference, agent, name)
                )
        _check_handoff_tool_names(
            (tool.ref for tool in declaration.tools if isinstance(tool, FunctionTool)), targets
        )
        if callback_tools:
            declaration = replace(
                declaration,
                instructions=cast(str, instructions),
                tools=(*declaration.tools, *callback_tools),
            )
        profiles.append(
            _AgentProfile(
                f"{id_prefix}{indices[id(agent)]}",
                agent,
                declaration,
                MappingProxyType(targets),
                MappingProxyType(callbacks),
                MappingProxyType(options),
            )
        )
    return tuple(profiles)
