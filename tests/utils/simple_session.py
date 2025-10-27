from __future__ import annotations

from agents.items import TResponseInputItem
from agents.memory.session import Session


class SimpleListSession(Session):
    """A minimal in-memory session implementation for tests."""

    def __init__(self, session_id: str = "test") -> None:
        self.session_id = session_id
        self._items: list[TResponseInputItem] = []

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        if limit is None:
            return list(self._items)
        if limit <= 0:
            return []
        return self._items[-limit:]

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        self._items.extend(items)

    async def pop_item(self) -> TResponseInputItem | None:
        if not self._items:
            return None
        return self._items.pop()

    async def clear_session(self) -> None:
        self._items.clear()
