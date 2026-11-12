"""Formatting and validation for sandbox memory artifacts."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

_EXCLUDED_MEMORY_ITEM_TYPES = frozenset(
    {
        "compaction",
        "image_generation_call",
        "reasoning",
    }
)
_INCLUDED_MEMORY_ITEM_TYPES = frozenset(
    {
        "apply_patch_call",
        "apply_patch_call_output",
        "computer_call",
        "computer_call_output",
        "custom_tool_call",
        "custom_tool_call_output",
        "function_call",
        "function_call_output",
        "local_shell_call",
        "local_shell_call_output",
        "mcp_approval_request",
        "mcp_approval_response",
        "mcp_call",
        "program",
        "program_output",
        "shell_call",
        "shell_call_output",
        "tool_search_call",
        "tool_search_output",
        "web_search_call",
    }
)


def _should_include_memory_item(item: Mapping[str, Any]) -> bool:
    role = item.get("role")
    if role in {"developer", "system"}:
        return False
    if role in {"assistant", "tool", "user"}:
        return True

    item_type = item.get("type")
    if item_type in _EXCLUDED_MEMORY_ITEM_TYPES:
        return False
    return item_type in _INCLUDED_MEMORY_ITEM_TYPES


class _MemoryFlushLock(asyncio.Lock):
    def __eq__(self, other: object) -> bool:
        # Runtime synchronization does not change the persisted workspace value.
        return type(self) is type(other)

    def __deepcopy__(self, _memo: dict[int, object]) -> _MemoryFlushLock:
        # A trusted copy still identifies the same live provider workspace.
        return self


@dataclass
class MemoryLayoutConfig:
    """Filesystem layout for sandbox-backed memory generation."""

    memories_dir: str = "memories"
    """Directory used for consolidated memory files."""

    sessions_dir: str = "sessions"
    """Directory used for per-rollout JSONL artifacts."""


class RolloutExtractionArtifacts(BaseModel):
    rollout_slug: str
    rollout_summary: str
    raw_memory: str


# Retain the existing Classic import paths for these shared types.
MemoryLayoutConfig.__module__ = "agents.classic.sandbox.config"
RolloutExtractionArtifacts.__module__ = "agents.classic.sandbox.memory.interface"


_ROLLOUT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_rollout_id(rollout_id: str) -> str:
    normalized_rollout_id = rollout_id.strip()
    if not _ROLLOUT_ID_RE.fullmatch(normalized_rollout_id):
        raise ValueError(
            "Sandbox memory rollout ID must be a file-safe ID containing only "
            "letters, numbers, '.', '_', or '-'."
        )
    return normalized_rollout_id


def validate_rollout_artifacts(artifacts: RolloutExtractionArtifacts) -> bool:
    if (
        artifacts.rollout_slug.strip() == ""
        and artifacts.rollout_summary.strip() == ""
        and artifacts.raw_memory.strip() == ""
    ):
        return False
    if (
        not artifacts.rollout_slug.strip()
        or not artifacts.rollout_summary.strip()
        or not artifacts.raw_memory.strip()
    ):
        raise ValueError("Phase 1 returned partially-empty memory artifacts.")
    return True


def format_raw_memory(
    *,
    updated_at: str,
    rollout_id: str,
    rollout_path: str,
    rollout_summary_file: str,
    terminal_state: str,
    raw_memory: str,
) -> str:
    return (
        f"rollout_id: {rollout_id}\n"
        f"updated_at: {updated_at}\n"
        f"rollout_path: {rollout_path}\n"
        f"rollout_summary_file: {rollout_summary_file}\n"
        f"terminal_state: {terminal_state}\n\n"
        f"{raw_memory.rstrip()}\n"
    )


def format_rollout_summary(
    *,
    updated_at: str,
    rollout_path: str,
    session_id: str,
    terminal_state: str,
    rollout_summary: str,
) -> str:
    return (
        f"session_id: {session_id}\n"
        f"updated_at: {updated_at}\n"
        f"rollout_path: {rollout_path}\n"
        f"terminal_state: {terminal_state}\n\n"
        f"{rollout_summary.rstrip()}\n"
    )
