"""
Simple file-backed session implementation for examples.

Persists conversation history as JSON on disk so runs can resume across processes.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from agents.memory.session import Session


class FileSession(Session):
    """Persist session items to a JSON file on disk."""

    def __init__(self, *, dir: str | Path | None = None, session_id: str | None = None) -> None:
        self._dir = Path(dir) if dir is not None else Path.cwd() / ".agents-sessions"
        self.session_id = session_id or ""
        # Ensure the directory exists up front so subsequent file operations do not race.
        self._dir.mkdir(parents=True, exist_ok=True)

    async def _ensure_session_id(self) -> str:
        if not self.session_id:
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            # Prefix with wall-clock time so recent sessions are easy to spot on disk.
            self.session_id = f"{timestamp}-{uuid4().hex[:12]}"
        await asyncio.to_thread(self._dir.mkdir, parents=True, exist_ok=True)
        file_path = self._items_path(self.session_id)
        if not file_path.exists():
            await asyncio.to_thread(file_path.write_text, "[]", encoding="utf-8")
        return self.session_id

    async def get_session_id(self) -> str:
        """Return the session id, creating one if needed."""
        return await self._ensure_session_id()

    async def get_items(self, limit: int | None = None) -> list[Any]:
        session_id = await self._ensure_session_id()
        items = await self._read_items(session_id)
        if limit is not None and limit >= 0:
            return items[-limit:]
        return items

    async def add_items(self, items: list[Any]) -> None:
        if not items:
            return
        session_id = await self._ensure_session_id()
        current = await self._read_items(session_id)
        # Deep-copy via JSON to avoid persisting live references that might mutate later.
        cloned = json.loads(json.dumps(items))
        await self._write_items(session_id, current + cloned)

    async def pop_item(self) -> Any | None:
        session_id = await self._ensure_session_id()
        items = await self._read_items(session_id)
        if not items:
            return None
        popped = items.pop()
        await self._write_items(session_id, items)
        return popped

    async def clear_session(self) -> None:
        if not self.session_id:
            return
        file_path = self._items_path(self.session_id)
        state_path = self._state_path(self.session_id)
        try:
            await asyncio.to_thread(file_path.unlink)
        except FileNotFoundError:
            pass
        try:
            await asyncio.to_thread(state_path.unlink)
        except FileNotFoundError:
            pass
        self.session_id = ""

    def _items_path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.json"

    def _state_path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}-state.json"

    async def _read_items(self, session_id: str) -> list[Any]:
        file_path = self._items_path(session_id)
        try:
            data = await asyncio.to_thread(file_path.read_text, "utf-8")
            parsed = json.loads(data)
            return parsed if isinstance(parsed, list) else []
        except FileNotFoundError:
            return []

    async def _write_items(self, session_id: str, items: list[Any]) -> None:
        file_path = self._items_path(session_id)
        payload = json.dumps(items, indent=2, ensure_ascii=False)
        await asyncio.to_thread(self._dir.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(file_path.write_text, payload, encoding="utf-8")

    async def load_state_json(self) -> dict[str, Any] | None:
        """Load a previously saved RunState JSON payload, if present."""
        session_id = await self._ensure_session_id()
        state_path = self._state_path(session_id)
        try:
            data = await asyncio.to_thread(state_path.read_text, "utf-8")
            parsed = json.loads(data)
            return parsed if isinstance(parsed, dict) else None
        except FileNotFoundError:
            return None

    async def save_state_json(self, state: dict[str, Any]) -> None:
        """Persist the serialized RunState JSON payload alongside session items."""
        session_id = await self._ensure_session_id()
        state_path = self._state_path(session_id)
        payload = json.dumps(state, indent=2, ensure_ascii=False)
        await asyncio.to_thread(self._dir.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(state_path.write_text, payload, encoding="utf-8")
