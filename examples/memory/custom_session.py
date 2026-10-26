"""Adapt a synchronous history store using only public SDK interfaces.

Run offline with: uv run python examples/memory/custom_session.py
The in-memory store stands in for a synchronous database client. A real client
must provide atomic batches and thread-safe operations, and set its own timeouts.
The adapter can live in a separate distribution that depends on openai-agents.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from threading import Lock
from typing import Any

from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from agents import Agent, RunConfig, Runner, SessionSettings, TResponseInputItem
from agents.memory import (
    await_session_mutation,
    coerce_session_settings,
    resolve_session_limit,
)
from agents.testing import ScriptedModel


class HistoryStore:
    """Small synchronous demonstration store; data is lost when the process exits."""

    def __init__(self) -> None:
        self._items: dict[str, list[TResponseInputItem]] = {}
        self._lock = Lock()

    def read(self, key: str, limit: int | None) -> list[TResponseInputItem]:
        with self._lock:
            items = self._items.get(key, [])
            return deepcopy(items if limit is None else items[-limit:] if limit > 0 else [])

    def append(self, key: str, items: list[TResponseInputItem]) -> None:
        batch = deepcopy(items)
        with self._lock:
            self._items.setdefault(key, []).extend(batch)

    def pop(self, key: str) -> TResponseInputItem | None:
        with self._lock:
            items = self._items.get(key, [])
            return items.pop() if items else None

    def clear(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)


class CustomSession:
    """Session adapter for a caller-owned store; no SDK base class is required.

    This adapter does not close the store. The application owns its lifetime.
    Individual store operations are atomic; overlapping runs for the same session
    still require application-level coordination.
    """

    def __init__(
        self,
        session_id: str,
        store: HistoryStore,
        *,
        session_settings: SessionSettings | dict[str, Any] | None = None,
    ) -> None:
        self.session_id = session_id
        self.session_settings = (
            SessionSettings()
            if session_settings is None
            else coerce_session_settings(session_settings)
        )
        self._store = store

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        effective_limit = resolve_session_limit(limit, self.session_settings)
        return await asyncio.to_thread(self._store.read, self.session_id, effective_limit)

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        await await_session_mutation(asyncio.to_thread(self._store.append, self.session_id, items))

    async def pop_item(self) -> TResponseInputItem | None:
        return await await_session_mutation(asyncio.to_thread(self._store.pop, self.session_id))

    async def clear_session(self) -> None:
        await await_session_mutation(asyncio.to_thread(self._store.clear, self.session_id))


async def main() -> None:
    store = HistoryStore()
    session = CustomSession("conversation-1", store, session_settings={"limit": 10})
    model = ScriptedModel(
        [
            [
                ResponseOutputMessage(
                    id=f"message-{index}",
                    role="assistant",
                    status="completed",
                    content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
                    type="message",
                )
            ]
            for index, text in enumerate(("Hello!", "You said hello."))
        ]
    )
    agent = Agent(name="Assistant", model=model)
    config = RunConfig(tracing_disabled=True)
    await Runner.run(agent, "Hello", session=session, run_config=config)
    result = await Runner.run(agent, "What did I say?", session=session, run_config=config)
    print(result.final_output)
    print(f"Stored {len(await session.get_items())} history items.")


if __name__ == "__main__":
    asyncio.run(main())
