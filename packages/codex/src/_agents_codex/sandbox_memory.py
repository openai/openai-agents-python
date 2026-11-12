"""Native two-phase sandbox memory generation for one acquired resource."""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, Field

from _agents_shared.sandbox_memory_artifacts import (
    MemoryLayoutConfig as MemoryLayoutConfig,
    RolloutExtractionArtifacts,
    format_raw_memory,
    format_rollout_summary,
    validate_rollout_artifacts,
    validate_rollout_id,
)

from ._public import preserve_module_identity as _preserve_module_identity

if TYPE_CHECKING:
    from collections.abc import Callable

    from agents.classic.sandbox.session.sandbox_session import SandboxSession

    from .agent import Agent
    from .history import CanonicalHistorySnapshot

_MEMORY_OWNER_ATTRIBUTE = "_sdk_sandbox_memory"
_MAX_RECORDS = 256
_MAX_INPUT_BYTES = 1_000_000
_MAX_ROLLOUT_BYTES = 4_000_000
_MemoryText = Annotated[str, Field(max_length=65_536)]


@dataclass(slots=True)
class _MemoryPhaseScope:
    owner: _SandboxMemoryOwner | None


_active_memory_phase: ContextVar[_MemoryPhaseScope | None] = ContextVar(
    "active_native_sandbox_memory_phase", default=None
)


def _current_memory_owner() -> _SandboxMemoryOwner | None:
    scope = _active_memory_phase.get()
    return None if scope is None else scope.owner


@dataclass(frozen=True)
class SandboxMemoryOptions:
    """Configure the two native Agents that extract and consolidate memory.

    Remote workspaces need image-owned Python 3 for bounded, no-follow file reads.
    """

    phase_one: Agent[None, Any]
    phase_two: Agent[None, Any]
    layout: MemoryLayoutConfig = field(default_factory=MemoryLayoutConfig)
    max_raw_memories: int = 16


class _Extraction(RolloutExtractionArtifacts):
    rollout_slug: _MemoryText
    rollout_summary: _MemoryText
    raw_memory: _MemoryText


class _Consolidation(BaseModel):
    summary: Annotated[_MemoryText, Field(min_length=1)]
    memory: Annotated[_MemoryText, Field(min_length=1)]


def _bounded_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _MAX_INPUT_BYTES:
        raise ValueError("Native sandbox memory input exceeds 1000000 bytes")
    return encoded


def _memory_transcript(
    thread_id: str, snapshot: CanonicalHistorySnapshot, sandbox_id: str
) -> dict[str, Any]:
    if snapshot.total > _MAX_RECORDS:
        raise ValueError("Native sandbox memory supports at most 256 original records")
    messages: list[dict[str, Any]] = []
    for item in snapshot.items:
        kind = item.get("type")
        if kind == "reasoning":
            continue
        if kind != "message":
            raise ValueError("Native sandbox memory requires text message history")
        role = item.get("role")
        if role not in ("user", "assistant"):
            continue
        parts = item.get("content")
        if not isinstance(parts, list):
            raise ValueError("Invalid native memory message content")
        if role == "user":
            metadata = item.get("internal_chat_message_metadata_passthrough")
            kinds = metadata.get("content_item_kinds") if isinstance(metadata, dict) else None
            if not isinstance(kinds, list) or len(kinds) != len(parts):
                continue
            # Native context can carry the user role. Only admitted user input belongs here.
            parts = [
                part
                for part, kind in zip(parts, kinds, strict=True)
                if isinstance(kind, str) and kind.startswith("user.")
            ]
            if not parts:
                continue
        content: list[dict[str, str]] = []
        for part in parts:
            if (
                not isinstance(part, dict)
                or part.get("type") not in ("input_text", "output_text")
                or not isinstance(part.get("text"), str)
            ):
                raise ValueError("Native sandbox memory requires text message history")
            content.append({"type": part["type"], "text": part["text"]})
        messages.append({"role": role, "content": content})
    return {
        "rollout_id": thread_id,
        "native_thread_id": thread_id,
        "sandbox_session_id": sandbox_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "terminal_metadata": {
            "terminal_state": "completed",
            "native_history_revision": snapshot.revision,
        },
        "items": messages,
    }


def _relative_directory(value: str) -> str:
    from agents.classic.sandbox.workspace_paths import coerce_posix_path

    path = coerce_posix_path(value)
    if not value or path.is_absolute() or PureWindowsPath(value).drive or ".." in path.parts:
        raise ValueError("Sandbox memory directories must be workspace-relative")
    return path.as_posix()


def _capture_sandbox_memory(
    options: SandboxMemoryOptions,
) -> Callable[[SandboxSession], _SandboxMemoryOwner]:
    limit = options.max_raw_memories
    if not 1 <= limit <= 256:
        raise ValueError("max_raw_memories must be between 1 and 256")
    layout = MemoryLayoutConfig(
        memories_dir=_relative_directory(options.layout.memories_dir),
        sessions_dir=_relative_directory(options.layout.sessions_dir),
    )
    extract = options.phase_one.with_output_type(_Extraction)
    consolidate = options.phase_two.with_output_type(_Consolidation)

    def create(resource: SandboxSession) -> _SandboxMemoryOwner:
        return _SandboxMemoryOwner(resource, layout, extract, consolidate, limit)

    return create


class _SandboxMemoryOwner:
    def __init__(
        self,
        resource: SandboxSession,
        layout: MemoryLayoutConfig,
        extract: Agent[None, _Extraction],
        consolidate: Agent[None, _Consolidation],
        max_raw_memories: int,
    ) -> None:
        from agents.classic.sandbox.memory.storage import SandboxMemoryStorage

        # Memory owns provider maintenance operations, like the existing memory storage.
        self._resource = resource._inner
        self._flush_lock = self._resource.state._native_memory_flush_lock
        self._storage = SandboxMemoryStorage(session=self._resource, layout=layout, bounded=True)
        self._extract = extract
        self._consolidate = consolidate
        self._max_raw_memories = max_raw_memories
        self._thread: str | None = None
        self._completed_revision: str | None = None

    async def _append_rollout(self, path: Path, line: str, revision: str) -> None:
        from agents.classic.sandbox.errors import WorkspaceReadNotFoundError

        try:
            prior = await self._resource._read_memory_file(path, max_bytes=_MAX_ROLLOUT_BYTES + 1)
        except (FileNotFoundError, WorkspaceReadNotFoundError):
            prior = b""
        next_line = line.encode("utf-8") + b"\n"
        if len(prior) > _MAX_ROLLOUT_BYTES:
            raise ValueError("Sandbox memory rollout exceeds 4000000 bytes")
        if prior:
            previous = json.loads(prior.rstrip(b"\n").rsplit(b"\n", 1)[-1])
            terminal = previous.get("terminal_metadata") if isinstance(previous, dict) else None
            if isinstance(terminal, dict) and terminal.get("native_history_revision") == revision:
                return
            if not prior.endswith(b"\n"):
                prior += b"\n"
        if len(prior) + len(next_line) > _MAX_ROLLOUT_BYTES:
            raise ValueError("Sandbox memory rollout exceeds 4000000 bytes")
        await self._storage.write_text(path, (prior + next_line).decode("utf-8"))

    async def flush(self, thread: str, snapshot: CanonicalHistorySnapshot) -> None:
        thread = validate_rollout_id(thread)
        if self._thread is not None and self._thread != thread:
            raise RuntimeError("Sandbox memory belongs to another native Session")
        if self._completed_revision == snapshot.revision:
            return
        sandbox_id = str(self._resource.state.session_id)
        transcript = _memory_transcript(thread, snapshot, sandbox_id)
        line = _bounded_json(transcript)
        self._thread = thread
        scope = _MemoryPhaseScope(self)
        token = _active_memory_phase.set(scope)
        try:
            async with self._flush_lock:
                await self._generate(thread, snapshot.revision, line, transcript, sandbox_id)
                self._completed_revision = snapshot.revision
        finally:
            scope.owner = None
            _active_memory_phase.reset(token)

    async def _generate(
        self,
        thread: str,
        revision: str,
        line: str,
        transcript: dict[str, Any],
        sandbox_id: str,
    ) -> None:
        extract_input = _bounded_json({"transcript": transcript})
        storage = self._storage
        await storage.ensure_layout()
        rollout_path = storage.sessions_dir / f"{thread}.jsonl"
        await self._append_rollout(rollout_path, line, revision)

        artifacts = (await self._extract.run(extract_input)).final_output
        if not validate_rollout_artifacts(artifacts):
            return
        raw = format_raw_memory(
            updated_at=transcript["updated_at"],
            rollout_id=thread,
            rollout_path=rollout_path.as_posix(),
            rollout_summary_file=f"rollout_summaries/{thread}.md",
            terminal_state="completed",
            raw_memory=artifacts.raw_memory,
        )
        await storage.write_text(storage.raw_memories_dir / f"{thread}.md", raw)
        await storage.write_text(
            storage.rollout_summaries_dir / f"{thread}.md",
            format_rollout_summary(
                updated_at=transcript["updated_at"],
                rollout_path=rollout_path.as_posix(),
                session_id=sandbox_id,
                terminal_state="completed",
                rollout_summary=artifacts.rollout_summary,
            ),
        )
        selection = await storage.build_phase_two_input_selection(
            max_raw_memories_for_consolidation=self._max_raw_memories
        )
        memories: list[dict[str, str]] = []
        input_size = len(b'{"memories":[]}')
        for item in selection.selected:
            rollout_id = validate_rollout_id(item.rollout_id)
            text = await storage.read_text(storage.raw_memories_dir / f"{rollout_id}.md")
            memory = {"rollout_id": rollout_id, "memory": text}
            input_size += len(_bounded_json(memory).encode("utf-8")) + bool(memories)
            if input_size > _MAX_INPUT_BYTES:
                raise ValueError("Native sandbox memory input exceeds 1000000 bytes")
            memories.append(memory)
        output = (await self._consolidate.run(_bounded_json({"memories": memories}))).final_output
        await storage.write_text(
            storage.memories_dir / "raw_memories.md",
            "\n\n".join(memory["memory"].rstrip("\n") for memory in memories),
        )
        await storage.write_text(storage.memories_dir / "MEMORY.md", output.memory)
        await storage.write_text(storage.memories_dir / "memory_summary.md", output.summary)
        await storage.write_phase_two_selection(selected_items=selection.selected)


_preserve_module_identity(globals(), "agents.sandbox")
