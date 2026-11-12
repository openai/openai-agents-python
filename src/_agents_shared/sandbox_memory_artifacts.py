"""Formatting and validation for sandbox memory artifacts."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from pydantic import BaseModel


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
