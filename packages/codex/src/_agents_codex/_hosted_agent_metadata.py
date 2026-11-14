"""Bounded observations from exact admitted provider tool occurrences."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass as dataclass
from typing import Any

from ._compiled_dataclasses import dataclass as _sdk_dataclass
from ._public import preserve_module_identity as _preserve_module_identity

_MAX_BYTES = 1024 * 1024


@_sdk_dataclass(frozen=True, slots=True)
class HostedAgentMetadata:
    agent_name: str
    phase: str | None = None


def _field(value: object, name: str) -> Any:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def _capture(name: object, phase: object) -> HostedAgentMetadata | None:
    if not isinstance(name, str) or not name or len(name) > _MAX_BYTES:
        return None
    if isinstance(phase, str) and len(phase) > _MAX_BYTES:
        return None
    try:
        if (
            len(name.encode("utf-8"))
            + (len(phase.encode("utf-8")) if isinstance(phase, str) else 0)
            > _MAX_BYTES
        ):
            return None
    except UnicodeError:
        return None
    return HostedAgentMetadata(name, phase if isinstance(phase, str) else None)


def _bind_hosted_agent_metadata(call: object, raw: object) -> None:
    if not isinstance(raw, Mapping):
        return
    value = _capture(raw.get("agent_name"), raw.get("phase"))
    if value is not None:
        object.__setattr__(call, "_hosted_agent_metadata", value)


def get_hosted_agent_metadata(value: object) -> HostedAgentMetadata | None:
    """Read attribution from a native tool context or provider item without routing effects."""
    call = _field(value, "call")
    if call is None:
        call = _field(value, "tool_call")
    item = call if call is not None else value
    known = getattr(item, "_hosted_agent_metadata", None)
    if isinstance(known, HostedAgentMetadata):
        return known
    agent = _field(item, "agent")
    return _capture(_field(agent, "agent_name"), _field(item, "phase"))


_preserve_module_identity(globals(), "agents.models")
