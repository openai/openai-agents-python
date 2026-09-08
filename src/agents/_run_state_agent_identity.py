"""Stable agent graph identities shared by RunState, tool tracking, and sandbox resume."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import threading
from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from ._tool_identity import get_function_tool_namespace, get_function_tool_qualified_name
from .agent import Agent
from .handoffs import Handoff
from .logger import logger
from .sandbox.capabilities.capability import Capability
from .sandbox.session.base_sandbox_session import BaseSandboxSession
from .tool import (
    ApplyPatchTool,
    ComputerTool,
    FunctionTool,
    HostedMCPTool,
    LocalShellTool,
    ShellTool,
)


def _iter_agent_graph(initial_agent: Agent[Any]) -> Iterator[Agent[Any]]:
    """Yield agents reachable from the starting agent in breadth-first order."""
    queue: deque[Agent[Any]] = deque([initial_agent])
    seen_agent_ids: set[int] = set()

    while queue:
        current = queue.popleft()
        current_id = id(current)
        if current_id in seen_agent_ids:
            continue
        seen_agent_ids.add(current_id)
        yield current

        for handoff_item in current.handoffs:
            handoff_agent: Any | None = None
            handoff_agent_name: str | None = None

            if isinstance(handoff_item, Handoff):
                # Some custom/mocked Handoff subclasses bypass dataclass initialization.
                # Prefer agent_name, then legacy name fallback used in tests.
                candidate_name = getattr(handoff_item, "agent_name", None) or getattr(
                    handoff_item, "name", None
                )
                if isinstance(candidate_name, str):
                    handoff_agent_name = candidate_name

                handoff_ref = getattr(handoff_item, "_agent_ref", None)
                handoff_agent = handoff_ref() if callable(handoff_ref) else None
                if handoff_agent is None:
                    # Backward-compatibility fallback for custom legacy handoff objects that store
                    # the target directly on `.agent`. New code should prefer `handoff()` objects.
                    legacy_agent = getattr(handoff_item, "agent", None)
                    if legacy_agent is not None:
                        handoff_agent = legacy_agent
                        logger.debug(
                            "Using legacy handoff `.agent` fallback while building agent map. "
                            "This compatibility path is not recommended for new code."
                        )
                if handoff_agent_name is None:
                    candidate_name = getattr(handoff_agent, "name", None)
                    handoff_agent_name = candidate_name if isinstance(candidate_name, str) else None
                if handoff_agent is None or not hasattr(handoff_agent, "handoffs"):
                    if handoff_agent_name:
                        logger.debug(
                            "Skipping unresolved handoff target while building agent map: %s",
                            handoff_agent_name,
                        )
                    continue
            else:
                # Backward-compatibility fallback for custom legacy handoff wrappers that expose
                # the target directly on `.agent` without inheriting from `Handoff`.
                legacy_agent = getattr(handoff_item, "agent", None)
                if legacy_agent is not None:
                    handoff_agent = legacy_agent
                    logger.debug(
                        "Using legacy non-`Handoff` `.agent` fallback while building agent map."
                    )
                else:
                    handoff_agent = handoff_item
                candidate_name = getattr(handoff_agent, "name", None)
                handoff_agent_name = candidate_name if isinstance(candidate_name, str) else None

            if handoff_agent is not None and handoff_agent_name:
                queue.append(cast(Agent[Any], handoff_agent))

        # Include agent-as-tool instances so nested approvals can be restored.
        tools = getattr(current, "tools", None)
        if tools:
            for tool in tools:
                if not getattr(tool, "_is_agent_tool", False):
                    continue
                tool_agent = getattr(tool, "_agent_instance", None)
                tool_agent_name = getattr(tool_agent, "name", None)
                if tool_agent is not None and tool_agent_name:
                    queue.append(tool_agent)


def _allocate_unique_agent_identity(agent_name: str, used_identities: set[str]) -> str:
    """Return a deterministic identity key without colliding with literal agent names."""
    candidate = agent_name
    next_index = 1
    while candidate in used_identities:
        next_index += 1
        candidate = f"{agent_name}#{next_index}"
    used_identities.add(candidate)
    return candidate


def _identity_type_name(value: Any) -> str:
    return f"{type(value).__module__}.{type(value).__qualname__}"


def _callable_identity_name(value: Any) -> str:
    module = getattr(value, "__module__", type(value).__module__)
    qualname = getattr(value, "__qualname__", type(value).__qualname__)
    return f"{module}.{qualname}"


def _normalize_identity_value(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, bytes | bytearray):
        return {"type": "bytes", "length": len(value)}
    if callable(value):
        return {"callable": _callable_identity_name(value)}
    if dataclasses.is_dataclass(value):
        return {
            "dataclass": _identity_type_name(value),
            "value": _normalize_identity_value(dataclasses.asdict(cast(Any, value))),
        }
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump(exclude_unset=True)
        except TypeError:
            dumped = value.model_dump()
        return {
            "model": _identity_type_name(value),
            "value": _normalize_identity_value(dumped),
        }
    if isinstance(value, Mapping):
        return {
            str(key): _normalize_identity_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_normalize_identity_value(item) for item in value]

    value_name = getattr(value, "name", None)
    if isinstance(value_name, str):
        return {"type": _identity_type_name(value), "name": value_name}
    return {"type": _identity_type_name(value)}


def _stable_identity_text(value: Any) -> str:
    return json.dumps(
        _normalize_identity_value(value),
        sort_keys=True,
        separators=(",", ":"),
    )


def _tool_identity_signature(tool: Any) -> dict[str, Any]:
    signature: dict[str, Any] = {
        "type": _identity_type_name(tool),
        "name": getattr(tool, "name", None),
    }
    namespace = get_function_tool_namespace(tool)
    if namespace is not None:
        signature["namespace"] = namespace
    qualified_name = get_function_tool_qualified_name(tool)
    if qualified_name is not None:
        signature["qualified_name"] = qualified_name
    if hasattr(tool, "environment"):
        signature["environment"] = _normalize_identity_value(tool.environment)
    if getattr(tool, "_is_agent_tool", False):
        nested_agent = getattr(tool, "_agent_instance", None)
        signature["agent_tool_target"] = getattr(nested_agent, "name", None)
    return signature


_THREADING_LOCK_TYPES = (type(threading.Lock()), type(threading.RLock()))


def _is_capability_runtime_only_value(value: Any) -> bool:
    return isinstance(
        value,
        (
            BaseSandboxSession,
            asyncio.Event,
            asyncio.Lock,
            asyncio.Semaphore,
            asyncio.Condition,
            threading.Event,
            *_THREADING_LOCK_TYPES,
        ),
    )


def _normalize_capability_identity_value(
    value: Any,
    *,
    seen: set[int] | None = None,
) -> Any:
    if seen is None:
        seen = set()

    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, bytes | bytearray):
        return {"type": "bytes", "length": len(value)}
    if callable(value):
        return {"callable": _callable_identity_name(value)}
    if _is_capability_runtime_only_value(value):
        return {"runtime_only": _identity_type_name(value)}
    if isinstance(
        value,
        ApplyPatchTool | ComputerTool | FunctionTool | HostedMCPTool | LocalShellTool | ShellTool,
    ):
        return _tool_identity_signature(value)

    object_id = id(value)
    if object_id in seen:
        return {"recursive": _identity_type_name(value)}

    if dataclasses.is_dataclass(value):
        seen.add(object_id)
        try:
            merged_fields = {
                field.name: getattr(value, field.name) for field in dataclasses.fields(value)
            }
            if hasattr(value, "__dict__"):
                for name, item in vars(value).items():
                    if name.startswith("_") or name in merged_fields:
                        continue
                    merged_fields[name] = item
            return {
                "dataclass": _identity_type_name(value),
                "value": {
                    name: _normalize_capability_identity_value(
                        item,
                        seen=seen,
                    )
                    for name, item in sorted(merged_fields.items())
                },
            }
        finally:
            seen.remove(object_id)

    if isinstance(value, Capability):
        seen.add(object_id)
        try:
            merged_fields = {}
            for name, field_info in value.__class__.model_fields.items():
                if field_info.exclude or name.startswith("_") or name == "session":
                    continue
                merged_fields[name] = getattr(value, name)
            return {
                "capability": _identity_type_name(value),
                "value": {
                    name: _normalize_capability_identity_value(
                        item,
                        seen=seen,
                    )
                    for name, item in sorted(merged_fields.items())
                },
            }
        finally:
            seen.remove(object_id)

    if hasattr(value, "model_dump"):
        seen.add(object_id)
        try:
            try:
                dumped = value.model_dump(mode="json", round_trip=True)
            except TypeError:
                dumped = value.model_dump(mode="json")
            return {
                "model": _identity_type_name(value),
                "value": _normalize_capability_identity_value(dumped, seen=seen),
            }
        finally:
            seen.remove(object_id)

    if isinstance(value, Mapping):
        seen.add(object_id)
        try:
            return {
                str(key): _normalize_capability_identity_value(item, seen=seen)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        finally:
            seen.remove(object_id)

    if isinstance(value, set | frozenset):
        seen.add(object_id)
        try:
            normalized_items = [
                _normalize_capability_identity_value(item, seen=seen) for item in value
            ]
            return sorted(normalized_items, key=_stable_identity_text)
        finally:
            seen.remove(object_id)

    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        seen.add(object_id)
        try:
            return [_normalize_capability_identity_value(item, seen=seen) for item in value]
        finally:
            seen.remove(object_id)

    if hasattr(value, "__dict__"):
        seen.add(object_id)
        try:
            return {
                "object": _identity_type_name(value),
                "value": {
                    name: _normalize_capability_identity_value(item, seen=seen)
                    for name, item in sorted(vars(value).items())
                    if not name.startswith("_")
                },
            }
        finally:
            seen.remove(object_id)

    value_name = getattr(value, "name", None)
    if isinstance(value_name, str):
        return {"type": _identity_type_name(value), "name": value_name}
    return {"type": _identity_type_name(value)}


def _capability_identity_signature(capability: Any) -> dict[str, Any]:
    return {
        "type": _identity_type_name(capability),
        "value": _normalize_capability_identity_value(capability),
    }


def _handoff_identity_signature(handoff_item: Agent[Any] | Handoff[Any, Any]) -> dict[str, Any]:
    if isinstance(handoff_item, Handoff):
        tool_name = getattr(handoff_item, "tool_name", None)
        if not isinstance(tool_name, str):
            tool_name = getattr(handoff_item, "name", None)
        agent_name = getattr(handoff_item, "agent_name", None)
        return {
            "type": _identity_type_name(handoff_item),
            "tool_name": tool_name,
            "agent_name": agent_name if isinstance(agent_name, str) else None,
            "input_filter": _normalize_identity_value(getattr(handoff_item, "input_filter", None)),
            "nest_handoff_history": getattr(handoff_item, "nest_handoff_history", None),
        }

    return {
        "type": _identity_type_name(handoff_item),
        "agent_name": getattr(handoff_item, "name", None),
    }


def _agent_identity_signature(agent: Agent[Any]) -> str:
    signature: dict[str, Any] = {
        "agent_type": _identity_type_name(agent),
        "handoff_description": getattr(agent, "handoff_description", None),
        "instructions": _normalize_identity_value(getattr(agent, "instructions", None)),
        "prompt": _normalize_identity_value(getattr(agent, "prompt", None)),
        "model": _normalize_identity_value(getattr(agent, "model", None)),
        "model_settings": _normalize_identity_value(getattr(agent, "model_settings", None)),
        "mcp_config": _normalize_capability_identity_value(getattr(agent, "mcp_config", None)),
        "hooks": _normalize_capability_identity_value(getattr(agent, "hooks", None)),
        "input_guardrails": sorted(
            _stable_identity_text(_normalize_capability_identity_value(guardrail))
            for guardrail in getattr(agent, "input_guardrails", [])
        ),
        "output_guardrails": sorted(
            _stable_identity_text(_normalize_capability_identity_value(guardrail))
            for guardrail in getattr(agent, "output_guardrails", [])
        ),
        "output_type": _normalize_identity_value(getattr(agent, "output_type", None)),
        "tool_use_behavior": _normalize_capability_identity_value(
            getattr(agent, "tool_use_behavior", None)
        ),
        "reset_tool_choice": getattr(agent, "reset_tool_choice", None),
        "tools": sorted(
            _stable_identity_text(_tool_identity_signature(tool))
            for tool in getattr(agent, "tools", [])
        ),
        "handoffs": sorted(
            _stable_identity_text(_handoff_identity_signature(handoff_item))
            for handoff_item in getattr(agent, "handoffs", [])
        ),
        "mcp_servers": sorted(
            _stable_identity_text(server) for server in getattr(agent, "mcp_servers", [])
        ),
    }

    default_manifest = getattr(agent, "default_manifest", None)
    if default_manifest is not None:
        signature["default_manifest"] = _normalize_capability_identity_value(default_manifest)

    base_instructions = getattr(agent, "base_instructions", None)
    if base_instructions is not None:
        signature["base_instructions"] = _normalize_identity_value(base_instructions)

    capabilities = getattr(agent, "capabilities", None)
    if isinstance(capabilities, Sequence):
        signature["capabilities"] = sorted(
            _stable_identity_text(_capability_identity_signature(capability))
            for capability in capabilities
        )

    return _stable_identity_text(signature)


def _agent_identity_sort_key(
    agent: Agent[Any],
    *,
    root_agent: Agent[Any],
    original_index: int,
) -> tuple[int, str, int]:
    return (
        0 if agent is root_agent else 1,
        _agent_identity_signature(agent),
        original_index,
    )


def _build_agent_identity_map(initial_agent: Agent[Any]) -> dict[str, Agent[Any]]:
    """Build a stable identity map that preserves duplicate agent names."""
    ordered_agents = list(_iter_agent_graph(initial_agent))
    original_indices = {id(agent): index for index, agent in enumerate(ordered_agents)}
    literal_names = {agent.name for agent in ordered_agents}
    agents_by_name: dict[str, list[Agent[Any]]] = {}
    for agent in ordered_agents:
        agents_by_name.setdefault(agent.name, []).append(agent)

    agent_identity_map: dict[str, Agent[Any]] = {}
    used_identities: set[str] = set()
    processed_names: set[str] = set()

    for agent in ordered_agents:
        agent_name = agent.name
        if agent_name in processed_names:
            continue
        processed_names.add(agent_name)

        group = agents_by_name[agent_name]
        sorted_group = sorted(
            group,
            key=lambda candidate: _agent_identity_sort_key(
                candidate,
                root_agent=initial_agent,
                original_index=original_indices[id(candidate)],
            ),
        )

        base_agent = sorted_group[0]
        used_identities.add(agent_name)
        agent_identity_map[agent_name] = base_agent

        next_index = 2
        for duplicate_agent in sorted_group[1:]:
            candidate = f"{agent_name}#{next_index}"
            while candidate in used_identities or candidate in literal_names:
                next_index += 1
                candidate = f"{agent_name}#{next_index}"
            used_identities.add(candidate)
            agent_identity_map[candidate] = duplicate_agent
            next_index += 1

    return agent_identity_map


def _build_agent_identity_keys_by_id(initial_agent: Agent[Any]) -> dict[int, str]:
    """Build stable identity keys for the reachable agent graph."""
    return {
        id(agent): identity for identity, agent in _build_agent_identity_map(initial_agent).items()
    }


def _build_agent_map(initial_agent: Agent[Any]) -> dict[str, Agent[Any]]:
    """Build a map of agent names to agents by traversing handoffs.

    Args:
        initial_agent: The starting agent.

    Returns:
        Dictionary mapping agent names to agent instances.
    """
    agent_map: dict[str, Agent[Any]] = {}
    for agent in _iter_agent_graph(initial_agent):
        agent_map.setdefault(agent.name, agent)

    return agent_map
