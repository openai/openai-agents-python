from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from .run_grouping import get_session_id_if_available

if TYPE_CHECKING:
    from ..memory import Session
    from ..run_config import RunConfig


def resolve_nested_agent_tool_run_config(
    *,
    resolved_run_config: RunConfig | None,
    session: Session | None,
    conversation_id: str | None,
    agent_name: str,
    tool_name: str,
) -> RunConfig | None:
    """Ensure nested Agent.as_tool() runs share a stable prompt-cache grouping.

    When the nested run has no conversation id or session id, each invocation would
    otherwise fall back to a random per-run cache key. Derive a stable group id from
    the agent-tool identity (and namespace under a parent group id when inherited) so
    consecutive identical as_tool() calls match the normal runner's cache boundary.
    """
    from ..run_config import RunConfig

    if conversation_id is not None and conversation_id.strip():
        return resolved_run_config
    if get_session_id_if_available(session) is not None:
        return resolved_run_config

    nested_group_id = f"agent-as-tool:{agent_name}:{tool_name}"
    if resolved_run_config is None:
        return RunConfig(group_id=nested_group_id)

    parent_group = resolved_run_config.group_id
    if parent_group is not None and parent_group.strip():
        parent_group = parent_group.strip()
        if parent_group.startswith("agent-as-tool:"):
            nested_group_id = parent_group
        else:
            # Keep nested prompts out of the parent's cache partition.
            nested_group_id = f"{parent_group}:{nested_group_id}"

    if resolved_run_config.group_id == nested_group_id:
        return resolved_run_config
    return dataclasses.replace(resolved_run_config, group_id=nested_group_id)
