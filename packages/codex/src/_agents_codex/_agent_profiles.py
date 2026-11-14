"""Capture immutable native Agent graphs before opening native resources."""

from __future__ import annotations

import inspect
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass as dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from . import tools as _tool_types
from ._compiled_dataclasses import dataclass as _sdk_dataclass
from ._function_tools import FunctionTool
from .agent import Agent
from .handoffs import Handoff
from .schema import OutputSpec, _prepare_spec
from .tools import ToolRef

if TYPE_CHECKING:
    from .decision_handoff import DecisionHandoff
    from .realtime_agents import RealtimeHandoff


@_sdk_dataclass(frozen=True)
class _AgentProfile:
    id: str
    agent: Agent[Any, Any]
    declaration: Agent[Any, Any]
    handoffs: Mapping[str, str]
    handoff_callbacks: Mapping[str, int] = field(default_factory=dict)
    handoff_options: Mapping[str, RealtimeHandoff] = field(default_factory=dict)
    decision_handoffs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    native_provider_id: str | None = None
    opening_tools: tuple[_tool_types.Tool[Any], ...] = ()


def _handoff_name(agent: Agent[Any, Any]) -> str:
    if agent.name is None or not agent.name.strip():
        raise ValueError("A handoff target must have a nonempty Agent.name")
    return re.sub(r"[^a-zA-Z0-9_-]", "_", f"transfer_to_{agent.name}").lower()[:64]


def _check_handoff_tool_names(refs: Iterable[ToolRef], handoffs: Mapping[str, str]) -> None:
    if any(ref.namespace in (None, "functions") and ref.name in handoffs for ref in refs):
        raise ValueError("A native handoff name collides with an ordinary function tool")


def _is_scoped_hosted_tool(tool: object) -> bool:
    from ._tool_catalog import _SCOPED_HOSTED_TYPES

    return isinstance(tool, _SCOPED_HOSTED_TYPES)


def _python_tool_slot(tool: object) -> FunctionTool[Any, Any, Any] | None:
    if type(tool) is FunctionTool:
        return tool
    from ._tool_catalog import ToolSearch

    if type(tool) is not FunctionTool and isinstance(tool, _tool_types.HostedMCP):
        return cast(FunctionTool[Any, Any, Any], tool._binding)
    if isinstance(tool, ToolSearch) and tool._manual is not None:
        return tool._manual
    return tool if isinstance(tool, FunctionTool) else None


def _resolve_tool_name_collisions(
    tools: Sequence[_tool_types.Tool[Any]], handoff_names: Sequence[str]
) -> tuple[tuple[_tool_types.Tool[Any], ...], tuple[int, ...]]:
    """Apply the opt-in winner policy only to eager application functions and handoffs."""
    function_indices: dict[str, list[int]] = {}
    handoff_indices: dict[str, list[int]] = {}
    for index, tool in enumerate(tools):
        if isinstance(tool, FunctionTool) and tool.namespace in (None, "functions"):
            function_indices.setdefault(tool.name, []).append(index)
    for index, name in enumerate(handoff_names):
        handoff_indices.setdefault(name, []).append(index)
    discard_tools: set[int] = set()
    discard_handoffs: set[int] = set()
    for name in dict.fromkeys((*function_indices, *handoff_indices)):
        functions = function_indices.get(name, [])
        handoffs = handoff_indices.get(name, [])
        if len(functions) + len(handoffs) < 2:
            continue
        for index in functions:
            tool = tools[index]
            if (
                type(tool) is not FunctionTool
                or tool.namespace is not None
                or tool._bind is None
                or tool.defer_loading
                or tool._custom_format is not None
                or tool.allowed_callers is not None
                and "direct" not in tool.allowed_callers
            ):
                raise ValueError(
                    "Namespaced, deferred and native tool names require unique identities"
                )
        discard_tools.update(functions if handoffs else functions[:-1])
        discard_handoffs.update(handoffs[:-1])
        logging.getLogger("agents.native.tools").warning(
            "Duplicate function or handoff name; keeping the final declaration."
        )
    return (
        tuple(tool for index, tool in enumerate(tools) if index not in discard_tools),
        tuple(index for index in range(len(handoff_names)) if index not in discard_handoffs),
    )


def _capture_agent_graph(
    root: Agent[Any, Any],
    *,
    root_declaration: Agent[Any, Any] | None = None,
    id_prefix: str = "agent_",
    restored_ids: Mapping[str, str] | None = None,
) -> tuple[_AgentProfile, ...]:
    """Resolve each target once and retain separate declarations and identities.

    No native resources are opened. Profile IDs are stable for a given ordered
    graph; they are identifiers, not authority to install new declarations.
    """
    agents = [root]
    opening = root if root_declaration is None else root_declaration
    root_output = _prepare_spec(opening.output_type)
    root_roles = tuple(
        tool
        for tool in opening.tools
        if (type(tool) is not FunctionTool and isinstance(tool, _tool_types.NativeAgents))
    )
    runtime_tools = tuple(
        tool
        for tool in opening.tools
        if not (
            type(tool) is FunctionTool
            or isinstance(tool, (FunctionTool, _tool_types._MCP, _tool_types.HostedMCP))
            or opening.realtime is None
            and _is_scoped_hosted_tool(tool)
        )
    )
    # Names locate current reachable declarations; native graph validation proves identity.
    restored_ids = dict(restored_ids or {})
    if root.name in restored_ids:
        raise ValueError("Runtime enrollment requires the original root Agent")
    indices = {id(root): 0}
    next_base_index = 1
    restored_seen: dict[str, int] = {}

    def capture_target(target: Agent[Any, Any]) -> int:
        nonlocal next_base_index
        existing = indices.get(id(target))
        if existing is not None:
            return existing
        if target.name in restored_ids:
            assert target.name is not None
            if target.name in restored_seen and restored_seen[target.name] != id(target):
                raise ValueError("Runtime enrollment requires unique current Agent names")
            if any(type(tool) is not FunctionTool or tool._bind is None for tool in target.tools):
                raise ValueError("Runtime enrollment requires actual current Python callbacks")
            restored_seen[target.name] = id(target)
            # Existing graph identities must not shift when a late leaf becomes reachable.
            index = 1024 + len(restored_seen)
        else:
            index = next_base_index
            next_base_index += 1
        indices[id(target)] = index
        agents.append(target)
        return index

    def profile_id(target: Agent[Any, Any]) -> str:
        if target.name in restored_ids:
            assert target.name is not None
            return restored_ids[target.name]
        return f"{id_prefix}{indices[id(target)]}"

    resolved: dict[int, tuple[Callable[[], Agent[Any, Any]], Agent[Any, Any]]] = {}
    provider_ids: dict[int, str] = {}

    def resolve(reference: Callable[[], Agent[Any, Any]]) -> Agent[Any, Any]:
        cached = resolved.get(id(reference))
        if cached is not None:
            return cached[1]
        result = reference()
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise TypeError("Handoff target resolvers must be synchronous")
        if not isinstance(result, Agent):
            raise TypeError("A handoff target resolver must return a native Agent")
        resolved[id(reference)] = (reference, result)
        return result

    def name_for(
        reference: Agent[Any, Any] | Handoff[Any, Any] | DecisionHandoff | RealtimeHandoff,
    ) -> str:
        from .decision_handoff import DecisionHandoff
        from .realtime_agents import RealtimeHandoff

        if isinstance(reference, DecisionHandoff):
            return reference.name
        if isinstance(reference, Handoff):
            return reference.callback.name
        if isinstance(reference, RealtimeHandoff):
            return reference.name if reference.name is not None else _handoff_name(reference.agent)
        return _handoff_name(reference)

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
        tools = tuple(tool for tool in source.tools if id(tool) not in handoff_bindings)
        handoffs = source.handoffs
        if source.tool_name_collision_policy == "warn":
            references = tuple(
                resolve(reference) if callable(reference) else reference for reference in handoffs
            )
            tools, indices_to_keep = _resolve_tool_name_collisions(
                tools, tuple(name_for(reference) for reference in references)
            )
            handoffs = tuple(references[index] for index in indices_to_keep)
        declaration = Agent[Any, Any](
            instructions=cast(str, instructions),
            realtime=source.realtime,
            hosted_prompt=source.hosted_prompt,
            name=source.name,
            tools=tools if getattr(source, "_hosted_tools_provided", True) else None,
            handoffs=handoffs,
            tool_name_collision_policy=source.tool_name_collision_policy,
            model=source.model,
            model_settings=source.model_settings,
            provider=source.provider,
            decisions=source.decisions,
            output_type=cast(OutputSpec[Any], source.output_type),
            programmatic_tool_calling=source.programmatic_tool_calling,
            transform_context=source.transform_context,
            after_model=source.after_model,
            prepare_handoff_input=source.prepare_handoff_input,
            before_compact=source.before_compact,
            compaction=source.compaction,
            environment=source.environment,
            before_complete=source.before_complete,
            on_lifecycle=source.on_lifecycle,
            on_tool_lifecycle=source.on_tool_lifecycle,
            tool_batch_completion=source.tool_batch_completion,
            input_guardrails=source.input_guardrails,
            output_guardrails=source.output_guardrails,
        )
        target_provider = declaration.provider
        native_provider_id = None
        if target_provider is not None and target_provider is not opening.provider:
            root_provider = opening.provider
            borrowed = (
                root_provider is not None
                and root_provider.model_backend is not None
                and target_provider.model_backend is not None
            )
            native = (
                target_provider.auth is None
                and target_provider.model_backend is None
                and (
                    root_provider is None
                    and target_provider.response_mode == "streaming"
                    or root_provider is not None
                    and root_provider.auth is None
                    and root_provider.model_backend is None
                    and root_provider.response_mode == target_provider.response_mode
                )
            )
            if opening.realtime is not None or not (borrowed or native):
                raise ValueError(
                    "Distinct handoff providers require compatible text inference providers"
                )
            if native:
                native_provider_id = provider_ids.setdefault(
                    id(target_provider), f"sdk_profile_{indices[id(agent)]}"
                )
        if declaration.programmatic_tool_calling != opening.programmatic_tool_calling:
            raise ValueError("Handoff profiles must share programmatic tool calling mode")
        output = _prepare_spec(declaration.output_type)
        if opening.realtime is not None and (
            output.mode,
            output.schema,
            output.wrapped,
            output.strict,
        ) != (
            root_output.mode,
            root_output.schema,
            root_output.wrapped,
            root_output.strict,
        ):
            raise ValueError("Realtime handoff profiles must share output mode and prompted schema")
        roles = tuple(
            tool
            for tool in declaration.tools
            if (type(tool) is not FunctionTool and isinstance(tool, _tool_types.NativeAgents))
        )
        if roles != root_roles:
            raise ValueError("Handoff profiles must share native child-role configuration")
        if (
            tuple(
                tool
                for tool in declaration.tools
                if not (
                    type(tool) is FunctionTool
                    or isinstance(tool, (FunctionTool, _tool_types._MCP, _tool_types.HostedMCP))
                    or opening.realtime is None
                    and _is_scoped_hosted_tool(tool)
                )
            )
            != runtime_tools
        ):
            raise ValueError("Handoff profiles must share native runtime tool configuration")
        targets: dict[str, str] = {}
        callbacks: dict[str, int] = {}
        options: dict[str, RealtimeHandoff] = {}
        callback_tools: list[FunctionTool[Any, Any, Any]] = []
        decision_handoffs: dict[str, Mapping[str, Any]] = {}
        python_slots = sum(_python_tool_slot(tool) is not None for tool in declaration.tools)
        for reference in declaration.handoffs:
            from .decision_handoff import DecisionHandoff, _decision_handoff_tool
            from .realtime_agents import RealtimeHandoff, _handoff_tool

            if isinstance(reference, DecisionHandoff):
                if opening.realtime is not None or declaration.realtime is not None:
                    raise ValueError("DecisionHandoff does not support Realtime")
                name = reference.name
                if name in targets or name in decision_handoffs:
                    raise ValueError(f"Duplicate handoff name in Agent {agent.name!r}: {name}")
                selected_targets: dict[str, Any] = {}
                factories = []
                for key, target in reference.targets.items():
                    if callable(target):
                        factories.append(key)
                        selected_targets[key] = target
                        continue
                    capture_target(target)
                    selected_targets[key] = profile_id(target)
                decision_handoffs[name] = {
                    "slot": python_slots + len(callback_tools),
                    "targets": tuple(
                        dict.fromkeys(
                            value for value in selected_targets.values() if isinstance(value, str)
                        )
                    ),
                    **({"factories": tuple(factories)} if factories else {}),
                }
                callback_tools.append(_decision_handoff_tool(reference, selected_targets))
                continue
            if isinstance(reference, Handoff):
                target = reference.target
            elif isinstance(reference, RealtimeHandoff):
                target = reference.agent
            elif isinstance(reference, Agent):
                target = reference
            else:
                target = resolve(reference)
            name = name_for(reference) if not callable(reference) else _handoff_name(target)
            if not name or len(name) > 64 or re.fullmatch(r"[a-zA-Z0-9_-]+", name) is None:
                raise ValueError("A handoff name must contain 1 to 64 letters, digits, '_' or '-'")
            capture_target(target)
            target_id = profile_id(target)
            if name in decision_handoffs:
                raise ValueError(f"Duplicate handoff name in Agent {agent.name!r}: {name}")
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
            (tool.ref for tool in declaration.tools if isinstance(tool, FunctionTool)),
            {**targets, **dict.fromkeys(decision_handoffs, "")},
        )
        if callback_tools:
            declaration = replace(
                declaration,
                instructions=cast(str, instructions),
                tools=(*declaration.tools, *callback_tools),
            )
        profiles.append(
            _AgentProfile(
                profile_id(agent),
                agent,
                declaration,
                MappingProxyType(targets),
                MappingProxyType(callbacks),
                MappingProxyType(options),
                MappingProxyType(decision_handoffs),
                native_provider_id,
                tools,
            )
        )
    return tuple(profiles)
