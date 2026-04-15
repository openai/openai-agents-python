from __future__ import annotations

import asyncio
import json
import logging
import posixpath
import re
import weakref
from typing import Any

from ...exceptions import UserError
from ...items import TResponseInputItem
from ...run_config import RunConfig, SandboxRunConfig
from ..capabilities.memory import Memory
from ..config import MemoryGenerateConfig
from ..session.base_sandbox_session import BaseSandboxSession
from .phase_one import (
    normalize_rollout_slug,
    render_phase_one_prompt,
    rollout_id_from_rollout_path,
    run_phase_one,
    validate_rollout_artifacts,
)
from .phase_two import run_phase_two
from .rollouts import (
    build_rollout_payload_from_result,
    dump_rollout_json,
    write_rollout,
)
from .storage import SandboxMemoryStorage

logger = logging.getLogger(__name__)

_ROLLOUT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_STOP = object()
_MemoryLayoutKey = tuple[str, str]
_MEMORY_GENERATION_MANAGERS: weakref.WeakKeyDictionary[
    BaseSandboxSession, dict[_MemoryLayoutKey, SandboxMemoryGenerationManager]
] = weakref.WeakKeyDictionary()


class SandboxMemoryGenerationManager:
    """Manage background memory generation for a sandbox session.

    The manager appends run segments to per-rollout JSONL files during the sandbox session, then
    runs phase-1 extraction for each rollout and one phase-2 consolidation when the session closes.
    """

    def __init__(self, *, session: BaseSandboxSession, memory: Memory) -> None:
        if memory.generate is None:
            raise ValueError("SandboxMemoryGenerationManager requires `Memory.generate` to be set.")

        self._session = session
        self._memory = memory
        self._generate_config: MemoryGenerateConfig = memory.generate
        self._storage = SandboxMemoryStorage(session=session, layout=memory.layout)
        self._queue: asyncio.Queue[str | object] = asyncio.Queue()
        self._worker_task: asyncio.Task[None] | None = None
        self._flush_lock = asyncio.Lock()
        self._rollout_files_by_rollout_id: dict[str, str] = {}
        self._pending_phase_two_rollout_ids: list[str] = []
        self._stopped = False
        self._session.register_pre_stop_hook(self.flush)

    @property
    def memory(self) -> Memory:
        """Return the `Memory` capability attached to this session."""

        return self._memory

    async def enqueue_result(
        self,
        result: Any,
        *,
        exception: BaseException | None = None,
        input_override: str | list[TResponseInputItem] | None = None,
        rollout_id: str,
    ) -> None:
        """Serialize a run result and enqueue it for background memory generation."""

        payload = build_rollout_payload_from_result(
            result,
            exception=exception,
            input_override=input_override,
        )
        await self.enqueue_rollout_payload(payload, rollout_id=rollout_id)

    async def enqueue_rollout_payload(
        self,
        payload: dict[str, Any],
        *,
        rollout_id: str,
    ) -> None:
        """Append a run segment to the session rollout file for later memory generation."""

        async with self._flush_lock:
            if self._stopped:
                return
            await self._storage.ensure_layout()
            rollout_id = _validate_rollout_id(rollout_id)
            file_name = _rollout_file_name_for_rollout_id(rollout_id)
            payload = dict(payload)
            updated_at = payload.pop("updated_at", None)
            payload.pop("rollout_id", None)
            ordered_payload: dict[str, Any] = {}
            if updated_at is not None:
                ordered_payload["updated_at"] = updated_at
            ordered_payload["rollout_id"] = rollout_id
            ordered_payload.update(payload)
            rollout_file = await write_rollout(
                session=self._session,
                rollout_contents=dump_rollout_json(ordered_payload),
                rollouts_path=self._memory.layout.sessions_dir,
                file_name=file_name,
            )
            self._rollout_files_by_rollout_id[rollout_id] = rollout_file.name

    async def flush(self) -> None:
        """Process accumulated memory rollouts and run one final phase-2 consolidation."""

        async with self._flush_lock:
            if self._stopped:
                return
            self._stopped = True
            try:
                rollout_files = sorted(set(self._rollout_files_by_rollout_id.values()))
                if not rollout_files:
                    return
                await self._storage.ensure_layout()
                self._ensure_worker()
                for rollout_file in rollout_files:
                    self._queue.put_nowait(rollout_file)
                await self._queue.join()
                if self._worker_task is not None:
                    self._queue.put_nowait(_STOP)
                    await self._worker_task
                    self._worker_task = None
                await self._run_phase_two()
            finally:
                _unregister_memory_generation_manager(session=self._session, manager=self)

    def _ensure_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker())

    async def _worker(self) -> None:
        while True:
            queue_item = await self._queue.get()
            try:
                if queue_item is _STOP:
                    return
                await self._process_rollout_file(str(queue_item))
            except Exception:
                logger.exception("Sandbox memory worker failed")
            finally:
                self._queue.task_done()

    async def _process_rollout_file(self, rollout_file_name: str) -> None:
        rollout_contents = await self._storage.read_text(
            self._storage.sessions_dir / rollout_file_name
        )

        phase_one_prompt = render_phase_one_prompt(rollout_contents=rollout_contents)
        artifacts = await run_phase_one(
            config=self._generate_config,
            prompt=phase_one_prompt,
            run_config=self._memory_run_config(),
        )
        if not validate_rollout_artifacts(artifacts):
            return

        payloads = [json.loads(line) for line in rollout_contents.splitlines() if line.strip()]
        if not payloads:
            return
        payload = payloads[-1]
        updated_at = str(payload.get("updated_at") or "unknown")
        terminal_metadata = payload.get("terminal_metadata")
        terminal_state = "unknown"
        if isinstance(terminal_metadata, dict):
            terminal_state = str(terminal_metadata.get("terminal_state") or "unknown")

        rollout_id = rollout_id_from_rollout_path(rollout_file_name)
        rollout_slug = normalize_rollout_slug(artifacts.rollout_slug)
        rollout_path = str(self._storage.sessions_dir / rollout_file_name)
        rollout_summary_file = f"rollout_summaries/{rollout_id}_{rollout_slug}.md"
        await asyncio.gather(
            self._storage.write_text(
                self._storage.memories_dir / "raw_memories" / f"{rollout_id}.md",
                _format_raw_memory(
                    updated_at=updated_at,
                    rollout_id=rollout_id,
                    rollout_path=rollout_path,
                    rollout_summary_file=rollout_summary_file,
                    terminal_state=terminal_state,
                    raw_memory=artifacts.raw_memory,
                ),
            ),
            self._storage.write_text(
                self._storage.memories_dir / rollout_summary_file,
                _format_rollout_summary(
                    updated_at=updated_at,
                    rollout_path=rollout_path,
                    session_id=str(self._session.state.session_id),
                    terminal_state=terminal_state,
                    rollout_summary=artifacts.rollout_summary,
                ),
            ),
        )
        self._pending_phase_two_rollout_ids.append(rollout_id)

    async def _run_phase_two(self) -> None:
        if not self._pending_phase_two_rollout_ids:
            return

        rollout_ids = list(dict.fromkeys(self._pending_phase_two_rollout_ids))
        selection = await self._storage.build_phase_two_input_selection(
            max_raw_memories_for_consolidation=(
                self._generate_config.max_raw_memories_for_consolidation
            )
        )
        if not await self._storage.rebuild_raw_memories(selected_items=selection.selected):
            return
        try:
            await run_phase_two(
                config=self._generate_config,
                memory_root=self._memory.layout.memories_dir,
                selection=selection,
                run_config=self._memory_run_config(),
            )
        except Exception:
            logger.exception("Sandbox memory phase 2 failed")
            return
        await self._storage.write_phase_two_selection(selected_items=selection.selected)
        self._pending_phase_two_rollout_ids = [
            rollout_id
            for rollout_id in self._pending_phase_two_rollout_ids
            if rollout_id not in set(rollout_ids)
        ]

    def _memory_run_config(self) -> RunConfig:
        return RunConfig(sandbox=SandboxRunConfig(session=self._session))


def get_or_create_memory_generation_manager(
    *,
    session: BaseSandboxSession,
    memory: Memory,
) -> SandboxMemoryGenerationManager:
    """Return the session- and layout-scoped memory generation manager, creating one if needed.

    A sandbox session can host multiple generating `Memory` capabilities when they use different
    memory layouts. Capabilities that share a layout also share a memory generation manager.
    """

    managers_by_layout = _MEMORY_GENERATION_MANAGERS.get(session)
    layout_key = _memory_layout_key(memory)
    existing = managers_by_layout.get(layout_key) if managers_by_layout is not None else None
    if existing is not None:
        if existing.memory.generate != memory.generate:
            raise UserError(
                "Sandbox session already has a different Memory generation config attached "
                "for this memory layout."
            )
        return existing

    if managers_by_layout is not None:
        memories_dir, sessions_dir = layout_key
        for existing_layout_key in managers_by_layout:
            if existing_layout_key[0] == memories_dir:
                raise UserError(
                    "Sandbox session already has a Memory generation capability for "
                    f"memories_dir={memories_dir!r}. Use a different memories_dir for isolated "
                    "memories, or the same layout to share memory."
                )
            if existing_layout_key[1] == sessions_dir:
                raise UserError(
                    "Sandbox session already has a Memory generation capability for "
                    f"sessions_dir={sessions_dir!r}. Use a different sessions_dir for isolated "
                    "memories, or the same layout to share memory."
                )

    manager = SandboxMemoryGenerationManager(session=session, memory=memory)
    if managers_by_layout is None:
        managers_by_layout = {}
        _MEMORY_GENERATION_MANAGERS[session] = managers_by_layout
    managers_by_layout[layout_key] = manager
    return manager


def _unregister_memory_generation_manager(
    *,
    session: BaseSandboxSession,
    manager: SandboxMemoryGenerationManager,
) -> None:
    managers_by_layout = _MEMORY_GENERATION_MANAGERS.get(session)
    if managers_by_layout is None:
        return
    layout_key = _memory_layout_key(manager.memory)
    existing = managers_by_layout.get(layout_key)
    if existing is manager:
        managers_by_layout.pop(layout_key, None)
    if not managers_by_layout:
        _MEMORY_GENERATION_MANAGERS.pop(session, None)


def _memory_layout_key(memory: Memory) -> _MemoryLayoutKey:
    return (
        posixpath.normpath(memory.layout.memories_dir),
        posixpath.normpath(memory.layout.sessions_dir),
    )


def _validate_rollout_id(rollout_id: str) -> str:
    normalized_rollout_id = rollout_id.strip()
    if not _ROLLOUT_ID_RE.fullmatch(normalized_rollout_id):
        raise ValueError(
            "Sandbox memory rollout ID must be a file-safe ID containing only "
            "letters, numbers, '.', '_', or '-'."
        )
    return normalized_rollout_id


def _rollout_file_name_for_rollout_id(rollout_id: str) -> str:
    return f"{_validate_rollout_id(rollout_id)}.jsonl"


def _format_raw_memory(
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


def _format_rollout_summary(
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
