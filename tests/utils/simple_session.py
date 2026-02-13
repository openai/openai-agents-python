from __future__ import annotations

from typing import cast

from agents.items import TResponseInputItem
from agents.memory.session import Session


class SimpleListSession(Session):
    """A minimal in-memory session implementation for tests."""

    def __init__(
        self,
        session_id: str = "test",
        history: list[TResponseInputItem] | None = None,
    ) -> None:
        self.session_id = session_id
        self._items: list[TResponseInputItem] = list(history) if history else []
        # Some session implementations strip IDs on write; tests can opt-in via attribute.
        self._ignore_ids_for_matching = False
        # Mirror saved_items used by some tests for inspection.
        self.saved_items: list[TResponseInputItem] = self._items

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


class CountingSession(SimpleListSession):
    """Session that tracks how many times pop_item is invoked (for rewind tests)."""

    def __init__(
        self,
        session_id: str = "test",
        history: list[TResponseInputItem] | None = None,
    ) -> None:
        super().__init__(session_id=session_id, history=history)
        self.pop_calls = 0

    async def pop_item(self) -> TResponseInputItem | None:
        self.pop_calls += 1
        return await super().pop_item()


class IdStrippingSession(CountingSession):
    """Session that strips IDs on add to mimic hosted stores that reassign IDs."""

    def __init__(
        self,
        session_id: str = "test",
        history: list[TResponseInputItem] | None = None,
    ) -> None:
        super().__init__(session_id=session_id, history=history)
        self._ignore_ids_for_matching = True

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        sanitized: list[TResponseInputItem] = []
        for item in items:
            if isinstance(item, dict):
                clean = dict(item)
                clean.pop("id", None)
                sanitized.append(cast(TResponseInputItem, clean))
            else:
                sanitized.append(item)
        await super().add_items(sanitized)
