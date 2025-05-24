from __future__ import annotations

# Removed abc import as it's no longer needed by SessionMemory
import sqlite3
import json
import time
from typing import TYPE_CHECKING, Protocol, runtime_checkable # Added Protocol, runtime_checkable

if TYPE_CHECKING:
    from .items import TResponseInputItem


@runtime_checkable
class SessionMemory(Protocol): # Changed from abc.ABC to Protocol
    """Protocol for session memory implementations."""

    async def get_history(self) -> list[TResponseInputItem]:
        """Returns the conversation history as a list of input items."""
        ... # Changed from pass to ...

    async def add_message(self, item: TResponseInputItem) -> None:
        """Adds a single message/item to the history."""
        ... # Changed from pass to ...

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Adds a list of items to the history."""
        ... # Changed from pass to ...

    async def clear(self) -> None:
        """Clears the entire history."""
        ... # Changed from pass to ...


class SQLiteSessionMemory(SessionMemory): # SQLiteSessionMemory still "implements" the protocol
    """
    A SessionMemory implementation that uses an SQLite database to store conversation history.
    Each message is stored as a JSON string in the database.
    """

    def __init__(self, db_path: str | None = None, *, table_name: str = "chat_history"):
        """
        Initializes the SQLite session memory.

        Args:
            db_path: Path to the SQLite database file. If None, an in-memory database is used.
            table_name: The name of the table to store chat history.
        """
        self.db_path = db_path if db_path else ":memory:"
        self.table_name = table_name
        self._init_db()

    def _get_conn(self):
        # For a simple default, synchronous sqlite3 is okay.
        # For production async, aiosqlite would be better.
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self._get_conn() as conn:
            cursor = conn.cursor()
            # Added session_id to allow for multiple conversations, though not used in this version
            cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.table_name} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                item_json TEXT NOT NULL
            )
            """)
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_timestamp ON {self.table_name} (timestamp)")
            conn.commit()

    async def get_history(self) -> list[TResponseInputItem]:
        """Returns the conversation history, ordered by timestamp."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT item_json FROM {self.table_name} ORDER BY timestamp ASC")
            rows = cursor.fetchall()
            history = []
            for row in rows:
                try:
                    item = json.loads(row[0])
                    history.append(item) 
                except json.JSONDecodeError as e:
                    # In a real app, use logging
                    print(f"Warning: SQLiteSessionMemory - Could not decode JSON from database: {row[0]}. Error: {e}") 
            return history

    async def add_message(self, item: TResponseInputItem) -> None:
        """Adds a single message/item to the history."""
        # This can be implemented more efficiently if needed, but for simplicity:
        await self.add_items([item])

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Adds a list of items to the history."""
        current_timestamp = time.time()
        with self._get_conn() as conn:
            cursor = conn.cursor()
            for i, item in enumerate(items):
                # Ensure unique timestamp for ordering within a batch
                item_timestamp = current_timestamp + (i * 1e-7) # Small offset for ordering
                try:
                    item_json = json.dumps(item)
                except TypeError as e:
                    print(f"Warning: SQLiteSessionMemory - Error serializing item to JSON: {item}. Error: {e}")
                    continue 
                
                cursor.execute(
                    f"INSERT INTO {self.table_name} (timestamp, item_json) VALUES (?, ?)",
                    (item_timestamp, item_json),
                )
            conn.commit()

    async def clear(self) -> None:
        """Clears the entire history from the table."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(f"DELETE FROM {self.table_name}")
            conn.commit()
