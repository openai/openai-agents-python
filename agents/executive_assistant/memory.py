from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class ShortTermMemory:
    """In-memory store for conversation turns."""

    def __init__(self) -> None:
        self._messages: list[dict[str, str]] = []

    def add(self, role: str, content: str) -> None:
        """Add a message to memory."""
        self._messages.append({"role": role, "content": content})

    def to_list(self) -> list[dict[str, str]]:
        """Return the last 20 messages."""
        return self._messages[-20:]


class LongTermMemory:
    """Simple file backed memory store."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if self._path.exists():
            self._data = json.loads(self._path.read_text())
        else:
            self._data = []

    def add(self, item: Any) -> None:
        """Persist an item to disk."""
        self._data.append(item)
        self._path.write_text(json.dumps(self._data))

    def all(self) -> list[Any]:
        """Return all persisted items."""
        return list(self._data)
