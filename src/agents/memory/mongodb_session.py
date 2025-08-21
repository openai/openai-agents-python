from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .session_abc import SessionABC

try:
    from pymongo import DESCENDING, AsyncMongoClient
    from pymongo.asynchronous.database import AsyncDatabase
except ImportError as _e:
    raise ImportError(
        "`pymongo` is required to use the MongoDBSession. You can install it via the optional "
        "dependency group: `pip install 'openai-agents[pymongo]'`."
    ) from _e

if TYPE_CHECKING:
    from agents.items import TResponseInputItem


class MongoDBSession(SessionABC):
    """MongoDB-based implementation of session storage."""

    _initialized = False

    def __init__(
        self,
        session_id: str,
        db: AsyncDatabase,
        sessions_table: str = "agent_sessions",
        messages_table: str = "agent_messages",
    ):
        self.session_id = session_id
        self.db = db
        self.sessions_table = sessions_table
        self.messages_table = messages_table
        self.sessions_collection = db[sessions_table]
        self.messages_collection = db[messages_table]

    @classmethod
    def from_connection_string(
        cls,
        session_id: str,
        conn_str: str,
        db_name: str,
        sessions_table: str = "agent_sessions",
        messages_table: str = "agent_messages",
        **kwargs: Any,
    ) -> MongoDBSession:
        client = AsyncMongoClient(conn_str, **kwargs)
        db = client[db_name]
        return cls(session_id, db, sessions_table, messages_table)

    async def _init_db(self) -> None:
        """Initialize the database collections."""
        collection_names = await self.db.list_collection_names()

        if self.sessions_table not in collection_names:
            await self.db.create_collection(self.sessions_table)
            await self.sessions_collection.create_index("session_id", unique=True)

        if self.messages_table not in collection_names:
            await self.db.create_collection(self.messages_table)
            await self.messages_collection.create_index(["session_id", "created_at"])

    async def _ensure_initialized(self) -> None:
        """Ensure the database schema is initialized."""
        if not self._initialized:
            await self._init_db()

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve the conversation history for this session.

        Args:
            limit: Maximum number of items to retrieve. If None, retrieves all items.
                   When specified, returns the latest N items in chronological order.

        Returns:
            List of input items representing the conversation history
        """
        await self._ensure_initialized()

        documents = (
            self.messages_collection.find({}, {"_id": False, "message_data": True}).sort(
                "created_at", DESCENDING
            )
            if limit is None
            else self.messages_collection.find({}, {"_id": False, "message_data": True})
            .sort("created_at", DESCENDING)
            .limit(limit)
        )

        items = []
        async for doc in documents:
            try:
                item = json.loads(doc["message_data"])
                items.append(item)
            except json.JSONDecodeError:
                # Skip invalid JSON entries
                continue

        return items

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Add new items to the conversation history.

        Args:
            items: List of input items to add to the history
        """
        if not items:
            return

        await self._ensure_initialized()

        existing_session_entity = await self.sessions_collection.find_one(
            {"session_id": self.session_id}
        )
        if not existing_session_entity:
            await self.sessions_collection.insert_one(
                {
                    "session_id": self.session_id,
                    "created_at": datetime.now(UTC),
                    "updated_at": datetime.now(UTC),
                }
            )

        message_data = [
            {
                "session_id": self.session_id,
                "message_data": json.dumps(item),
                "created_at": datetime.now(UTC),
            }
            for item in items
        ]

        await self.messages_collection.insert_many(message_data)

        await self.sessions_collection.update_one(
            {"session_id": self.session_id}, {"$set": {"updated_at": datetime.now(UTC)}}
        )

    async def pop_item(self) -> TResponseInputItem | None:
        await self._ensure_initialized()

        last_message = await self.messages_collection.find_one(
            {"session_id": self.session_id}, sort=[("created_at", DESCENDING)]
        )

        if last_message:
            await self.messages_collection.delete_one({"_id": last_message[""]})
            message_data = last_message["message_data"]
            try:
                item = json.loads(message_data)
                return item
            except json.JSONDecodeError:
                return None

        return None

    async def clear_session(self) -> None:
        await self._ensure_initialized()

        await self.messages_collection.delete_many({"session_id": self.session_id})
        await self.sessions_collection.delete_one({"session_id": self.session_id})

    async def close(self) -> None:
        await self.db.client.close()

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()
