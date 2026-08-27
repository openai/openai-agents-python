from __future__ import annotations

import asyncio
import io
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import MemoryLayoutConfig
from ..errors import WorkspaceReadNotFoundError
from ..session.base_sandbox_session import BaseSandboxSession


def decode_payload(payload: object) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, bytes | bytearray):
        return bytes(payload).decode("utf-8", errors="replace")
    return str(payload)


@dataclass(frozen=True)
class PhaseTwoSelectionItem:
    rollout_id: str
    updated_at: str
    rollout_path: str
    rollout_summary_file: str
    terminal_state: str

    def to_dict(self) -> dict[str, str]:
        return {
            "rollout_id": self.rollout_id,
            "updated_at": self.updated_at,
            "rollout_path": self.rollout_path,
            "rollout_summary_file": self.rollout_summary_file,
            "terminal_state": self.terminal_state,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PhaseTwoSelectionItem | None:
        rollout_id = str(payload.get("rollout_id") or "").strip()
        rollout_summary_file = str(payload.get("rollout_summary_file") or "").strip()
        if not rollout_id or not rollout_summary_file:
            return None
        return cls(
            rollout_id=rollout_id,
            updated_at=str(payload.get("updated_at") or "").strip(),
            rollout_path=str(payload.get("rollout_path") or "").strip(),
            rollout_summary_file=rollout_summary_file,
            terminal_state=str(payload.get("terminal_state") or "").strip(),
        )


@dataclass(frozen=True)
class PhaseTwoInputSelection:
    selected: list[PhaseTwoSelectionItem]
    retained_rollout_ids: set[str]
    removed: list[PhaseTwoSelectionItem]


class SandboxMemoryStorage:
    """Read and write sandbox memory files using a configured layout."""

    def __init__(self, *, session: BaseSandboxSession, layout: MemoryLayoutConfig) -> None:
        self._session = session
        self._layout = layout
        self._layout_lock = asyncio.Lock()

    @property
    def sessions_dir(self) -> Path:
        """Return the session artifact directory relative to the sandbox workspace root."""

        return Path(self._layout.sessions_dir)

    @property
    def memories_dir(self) -> Path:
        """Return the memory directory relative to the sandbox workspace root."""

        return Path(self._layout.memories_dir)

    @property
    def raw_memories_dir(self) -> Path:
        return self.memories_dir / "raw_memories"

    @property
    def rollout_summaries_dir(self) -> Path:
        return self.memories_dir / "rollout_summaries"

    @property
    def phase_two_selection_path(self) -> Path:
        return self.memories_dir / "phase_two_selection.json"

    async def ensure_layout(self) -> None:
        async with self._layout_lock:
            await asyncio.gather(
                self._session.mkdir(self.sessions_dir, parents=True),
                self._session.mkdir(self.memories_dir, parents=True),
                self._session.mkdir(self.memories_dir / "raw_memories", parents=True),
                self._session.mkdir(self.memories_dir / "rollout_summaries", parents=True),
                self._session.mkdir(self.memories_dir / "skills", parents=True),
            )
            await self.ensure_text_file(self.memories_dir / "MEMORY.md")
            await self.ensure_text_file(self.memories_dir / "memory_summary.md")

    async def ensure_text_file(self, path: Path) -> None:
        absolute = self._session.normalize_path(path)
        exists = await self._session.exec("test", "-f", str(absolute), shell=False)
        if exists.ok():
            return
        await self._session.write(path, io.BytesIO(b""))

    async def read_text(self, path: Path) -> str:
        handle = await self._session.read(path)
        try:
            return decode_payload(handle.read())
        finally:
            handle.close()

    async def write_text(self, path: Path, text: str) -> None:
        await self._session.write(path, io.BytesIO(text.encode("utf-8")))

    async def build_phase_two_input_selection(
        self,
        *,
        max_raw_memories_for_consolidation: int,
    ) -> PhaseTwoInputSelection:
        current_items = await self._list_current_selection_items()
        selected = current_items[:max_raw_memories_for_consolidation]
        prior_selected = await self.read_phase_two_selection()
        selected_rollout_ids = {item.rollout_id for item in selected}
        prior_rollout_ids = {item.rollout_id for item in prior_selected}
        return PhaseTwoInputSelection(
            selected=selected,
            retained_rollout_ids=selected_rollout_ids & prior_rollout_ids,
            removed=[
                item for item in prior_selected if item.rollout_id not in selected_rollout_ids
            ],
        )

    async def rebuild_raw_memories(
        self,
        *,
        selected_items: list[PhaseTwoSelectionItem],
    ) -> bool:
        chunks: list[str] = []
        for item in selected_items:
            raw_memory_path = self.raw_memories_dir / f"{item.rollout_id}.md"
            try:
                chunks.append((await self.read_text(raw_memory_path)).rstrip("\n"))
            except (FileNotFoundError, WorkspaceReadNotFoundError):
                continue
        if not chunks:
            return False
        await self.write_text(
            self.memories_dir / "raw_memories.md",
            "\n\n".join(chunks),
        )
        return True

    async def read_phase_two_selection(self) -> list[PhaseTwoSelectionItem]:
        try:
            raw_payload = await self.read_text(self.phase_two_selection_path)
        except (FileNotFoundError, WorkspaceReadNotFoundError):
            return []

        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError:
            return []

        if not isinstance(payload, dict):
            return []

        selected = payload.get("selected")
        if not isinstance(selected, list):
            return []

        items: list[PhaseTwoSelectionItem] = []
        for entry in selected:
            if not isinstance(entry, dict):
                continue
            item = PhaseTwoSelectionItem.from_dict(entry)
            if item is not None:
                items.append(item)
        return items

    async def write_phase_two_selection(
        self,
        *,
        selected_items: list[PhaseTwoSelectionItem],
    ) -> None:
        payload = {
            "version": 1,
            "updated_at": datetime.now(tz=timezone.utc).isoformat(),
            "selected": [item.to_dict() for item in selected_items],
        }
        await self.write_text(self.phase_two_selection_path, json.dumps(payload, indent=2) + "\n")

    async def _list_current_selection_items(self) -> list[PhaseTwoSelectionItem]:
        try:
            entries = await self._session.ls(self.raw_memories_dir)
        except Exception:
            return []

        items: list[tuple[tuple[int, str], str, PhaseTwoSelectionItem]] = []
        for entry in entries:
            if entry.is_dir():
                continue
            path = Path(entry.path)
            if path.suffix != ".md":
                continue
            try:
                raw_memory = (await self.read_text(self.raw_memories_dir / path.name)).rstrip("\n")
            except (FileNotFoundError, WorkspaceReadNotFoundError):
                continue
            item = _extract_selection_item(raw_memory)
            if item is None:
                continue
            items.append((_updated_at_sort_key(raw_memory), item.rollout_id, item))
        items.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in items]


def _updated_at_sort_key(raw_memory: str) -> tuple[int, str]:
    for line in raw_memory.splitlines():
        if line.startswith("updated_at:"):
            _, value = line.split(":", maxsplit=1)
            updated_at = value.strip()
            if not updated_at or updated_at == "unknown":
                return (0, "")
            return (1, updated_at)
    return (0, "")


def _extract_selection_item(raw_memory: str) -> PhaseTwoSelectionItem | None:
    rollout_id = _extract_metadata_value(raw_memory, "rollout_id")
    rollout_summary_file = _extract_metadata_value(raw_memory, "rollout_summary_file")
    if not rollout_id or not rollout_summary_file:
        return None
    return PhaseTwoSelectionItem(
        rollout_id=rollout_id,
        updated_at=_extract_metadata_value(raw_memory, "updated_at"),
        rollout_path=_extract_metadata_value(raw_memory, "rollout_path"),
        rollout_summary_file=rollout_summary_file,
        terminal_state=_extract_metadata_value(raw_memory, "terminal_state"),
    )


def _extract_metadata_value(raw_memory: str, key: str) -> str:
    prefix = f"{key}:"
    for line in raw_memory.splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix).strip()
    return ""
