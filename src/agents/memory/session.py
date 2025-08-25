from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..items import ModelResponse, TResponseInputItem

from ..tracing import get_current_span

# Registry mapping tool call IDs to their exact function span (trace_id, span_id)
_TOOL_CALL_SPAN_REGISTRY: dict[str, tuple[str | None, str | None]] = {}

# Registry mapping response IDs to their model span (trace_id, span_id)
_RESPONSE_SPAN_REGISTRY: dict[str, tuple[str | None, str | None]] = {}

# Registry mapping trace_id to the "last" model response span seen in that trace
_LAST_RESPONSE_SPAN_BY_TRACE: dict[str | None, tuple[str | None, str | None]] = {}


def register_tool_call_span(call_id: str, trace_id: str | None, span_id: str | None) -> None:
    """Registers a mapping between a tool-call ID and the span that executed it."""
    _TOOL_CALL_SPAN_REGISTRY[call_id] = (trace_id, span_id)


def pop_tool_call_span(call_id: str) -> tuple[str | None, str | None] | None:
    """Retrieve & remove a span mapping for the given tool-call ID, if present."""
    return _TOOL_CALL_SPAN_REGISTRY.pop(call_id, None)


def register_response_span(
    response_id: str | None, trace_id: str | None, span_id: str | None
) -> None:  # noqa: E501
    """Registers a mapping between a model response ID and its response/generation span.

    If response_id is None (provider doesn't return one), only the per-trace cache is updated.
    """
    _LAST_RESPONSE_SPAN_BY_TRACE[trace_id] = (trace_id, span_id)
    if response_id:
        _RESPONSE_SPAN_REGISTRY[response_id] = (trace_id, span_id)


def get_response_span(response_id: str) -> tuple[str | None, str | None] | None:
    """Retrieve a span mapping for the given response ID, if present."""
    return _RESPONSE_SPAN_REGISTRY.get(response_id)


def get_last_response_span_for_trace(trace_id: str | None) -> tuple[str | None, str | None] | None:
    """Retrieve the last seen model response span for the given trace ID, if present."""
    return _LAST_RESPONSE_SPAN_BY_TRACE.get(trace_id)


# Registry for model names (by response_id and by trace)
_RESPONSE_MODEL_REGISTRY: dict[str, str | None] = {}
_LAST_MODEL_BY_TRACE: dict[str | None, str | None] = {}


def register_response_model(
    response_id: str | None, trace_id: str | None, model: str | None
) -> None:  # noqa: E501
    """Registers a mapping for model names by response_id and by trace."""
    _LAST_MODEL_BY_TRACE[trace_id] = model
    if response_id:
        _RESPONSE_MODEL_REGISTRY[response_id] = model


def get_response_model(response_id: str) -> str | None:
    return _RESPONSE_MODEL_REGISTRY.get(response_id)


def get_last_model_for_trace(trace_id: str | None) -> str | None:
    return _LAST_MODEL_BY_TRACE.get(trace_id)


@runtime_checkable
class Session(Protocol):
    """Protocol for session implementations.

    Session stores conversation history for a specific session, allowing
    agents to maintain context without requiring explicit manual memory management.
    """

    session_id: str

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve the conversation history for this session.

        Args:
            limit: Maximum number of items to retrieve. If None, retrieves all items.
                   When specified, returns the latest N items in chronological order.

        Returns:
            List of input items representing the conversation history
        """
        ...

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Add new items to the conversation history.

        Args:
            items: List of input items to add to the history
        """
        ...

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Returns:
            The most recent item if it exists, None if the session is empty
        """
        ...

    async def clear_session(self) -> None:
        """Clear all items for this session."""
        ...


class SessionABC(ABC):
    """Abstract base class for session implementations.

    Session stores conversation history for a specific session, allowing
    agents to maintain context without requiring explicit manual memory management.

    This ABC is intended for internal use and as a base class for concrete implementations.
    Third-party libraries should implement the Session protocol instead.
    """

    session_id: str

    @abstractmethod
    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve the conversation history for this session.

        Args:
            limit: Maximum number of items to retrieve. If None, retrieves all items.
                   When specified, returns the latest N items in chronological order.

        Returns:
            List of input items representing the conversation history
        """
        ...

    @abstractmethod
    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Add new items to the conversation history.

        Args:
            items: List of input items to add to the history
        """
        ...

    @abstractmethod
    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Returns:
            The most recent item if it exists, None if the session is empty
        """
        ...

    @abstractmethod
    async def clear_session(self) -> None:
        """Clear all items for this session."""
        ...


class SQLiteSession(SessionABC):
    """SQLite-based implementation of session storage.

    This implementation stores conversation history in a SQLite database.
    By default, uses an in-memory database that is lost when the process ends.
    For persistent storage, provide a file path.
    """

    def __init__(
        self,
        session_id: str,
        db_path: str | Path = ":memory:",
        sessions_table: str = "agent_sessions",
        messages_table: str = "agent_messages",
        *,
        structured_metadata: bool = False,
        conversation_table: str = "agent_conversation_messages",
        tool_calls_table: str = "agent_tool_calls",
        usage_table: str = "agent_usage",
    ):
        """Initialize the SQLite session.

        Args:
            session_id: Unique identifier for the conversation session
            db_path: Path to the SQLite database file. Defaults to ':memory:' (in-memory database)
            sessions_table: Name of the table to store session metadata. Defaults to
                'agent_sessions'
            messages_table: Name of the table to store message data. Defaults to 'agent_messages'
            structured_metadata: If True, enables structured storage mode, creating
                additional tables for messages and tool calls. Defaults to False.
            conversation_table: Name for the structured conversation messages table.
                Defaults to 'agent_conversation_messages'.
            tool_calls_table: Name for the structured tool calls table.
                Defaults to 'agent_tool_calls'.
            usage_table: Name for the structured usage table.
                Defaults to 'agent_usage'.
        """
        self.session_id = session_id
        self.db_path = db_path
        self.sessions_table = sessions_table
        self.messages_table = messages_table
        self.structured_metadata = structured_metadata
        self.conversation_table = conversation_table
        self.tool_calls_table = tool_calls_table
        self.usage_table = usage_table
        self._local = threading.local()
        self._lock = threading.Lock()

        # For in-memory databases, we need a shared connection to avoid thread isolation
        # For file databases, we use thread-local connections for better concurrency
        self._is_memory_db = str(db_path) == ":memory:"
        if self._is_memory_db:
            self._shared_connection = sqlite3.connect(":memory:", check_same_thread=False)
            self._shared_connection.execute("PRAGMA journal_mode=WAL")
            self._shared_connection.execute("PRAGMA foreign_keys=ON")
            self._init_db_for_connection(self._shared_connection)
        else:
            # For file databases, initialize the schema once since it persists
            init_conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            init_conn.execute("PRAGMA journal_mode=WAL")
            init_conn.execute("PRAGMA foreign_keys=ON")
            self._init_db_for_connection(init_conn)
            init_conn.close()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection."""
        if self._is_memory_db:
            # Use shared connection for in-memory database to avoid thread isolation
            return self._shared_connection
        else:
            # Use thread-local connections for file databases
            if not hasattr(self._local, "connection"):
                self._local.connection = sqlite3.connect(
                    str(self.db_path),
                    check_same_thread=False,
                )
                self._local.connection.execute("PRAGMA journal_mode=WAL")
                self._local.connection.execute("PRAGMA foreign_keys=ON")
            assert isinstance(self._local.connection, sqlite3.Connection), (
                f"Expected sqlite3.Connection, got {type(self._local.connection)}"
            )
            return self._local.connection

    def _init_db_for_connection(self, conn: sqlite3.Connection) -> None:
        """Initialize the database schema for a specific connection."""
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.sessions_table} (
                session_id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        )

        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self.messages_table} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                message_data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES {self.sessions_table} (session_id)
                    ON DELETE CASCADE
            )
        """
        )

        conn.execute(
            f"""
            CREATE INDEX IF NOT EXISTS idx_{self.messages_table}_session_id
            ON {self.messages_table} (session_id, created_at)
        """
        )

        conn.commit()

        # Create additional structured tables if enabled
        if self.structured_metadata:
            # Conversation messages table
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.conversation_table} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    raw_event_id INTEGER NOT NULL,
                    role TEXT,
                    content TEXT,
                    parent_raw_event_id INTEGER,
                    trace_id TEXT,
                    span_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES {self.sessions_table} (session_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (raw_event_id) REFERENCES {self.messages_table} (id)
                        ON DELETE CASCADE
                )
            """
            )

            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{self.conversation_table}_session_id
                ON {self.conversation_table} (session_id, created_at)
            """
            )

            # Tool calls table
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.tool_calls_table} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    raw_event_id INTEGER NOT NULL,
                    call_id TEXT,
                    tool_name TEXT,
                    arguments JSON,
                    output JSON,
                    status TEXT,
                    trace_id TEXT,
                    span_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES {self.sessions_table} (session_id)
                        ON DELETE CASCADE,
                    FOREIGN KEY (raw_event_id) REFERENCES {self.messages_table} (id)
                        ON DELETE CASCADE
                )
            """
            )

            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{self.tool_calls_table}_session_id
                ON {self.tool_calls_table} (session_id, created_at)
            """
            )

            # Usage table (per LLM response)
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.usage_table} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    response_id TEXT,
                    model TEXT,
                    requests INTEGER,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    total_tokens INTEGER,
                    input_tokens_details JSON,
                    output_tokens_details JSON,
                    trace_id TEXT,
                    span_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (session_id) REFERENCES {self.sessions_table} (session_id)
                        ON DELETE CASCADE
                )
            """
            )

            # Indexes for faster queries
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{self.conversation_table}_trace
                ON {self.conversation_table} (trace_id, created_at)
            """
            )
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{self.conversation_table}_span
                ON {self.conversation_table} (span_id, created_at)
            """
            )
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{self.tool_calls_table}_trace
                ON {self.tool_calls_table} (trace_id, created_at)
            """
            )
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{self.tool_calls_table}_span
                ON {self.tool_calls_table} (span_id, created_at)
            """
            )
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{self.usage_table}_trace
                ON {self.usage_table} (trace_id, created_at)
            """
            )
            conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{self.usage_table}_response
                ON {self.usage_table} (response_id)
            """
            )

    async def get_items(self, limit: int | None = None) -> list[TResponseInputItem]:
        """Retrieve the conversation history for this session.

        Args:
            limit: Maximum number of items to retrieve. If None, retrieves all items.
                   When specified, returns the latest N items in chronological order.

        Returns:
            List of input items representing the conversation history
        """

        def _get_items_sync():
            conn = self._get_connection()
            with self._lock if self._is_memory_db else threading.Lock():
                if limit is None:
                    # Fetch all items in chronological order
                    cursor = conn.execute(
                        f"""
                        SELECT message_data FROM {self.messages_table}
                        WHERE session_id = ?
                        ORDER BY created_at ASC
                    """,
                        (self.session_id,),
                    )
                else:
                    # Fetch the latest N items in chronological order
                    cursor = conn.execute(
                        f"""
                        SELECT message_data FROM {self.messages_table}
                        WHERE session_id = ?
                        ORDER BY created_at DESC
                        LIMIT ?
                        """,
                        (self.session_id, limit),
                    )

                rows = cursor.fetchall()

                # Reverse to get chronological order when using DESC
                if limit is not None:
                    rows = list(reversed(rows))

                items = []
                for (message_data,) in rows:
                    try:
                        item = json.loads(message_data)
                        items.append(item)
                    except json.JSONDecodeError:
                        # Skip invalid JSON entries
                        continue

                return items

        return await asyncio.to_thread(_get_items_sync)

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Add new items to the conversation history.

        Args:
            items: List of input items to add to the history
        """
        if not items:
            return

        def _add_items_sync():
            conn = self._get_connection()

            with self._lock if self._is_memory_db else threading.Lock():
                # Ensure session exists
                conn.execute(
                    f"""
                    INSERT OR IGNORE INTO {self.sessions_table} (session_id) VALUES (?)
                """,
                    (self.session_id,),
                )

                # Add items
                if not self.structured_metadata:
                    # Flat storage: bulk insert for performance
                    message_data = [(self.session_id, json.dumps(item)) for item in items]
                    conn.executemany(
                        f"""
                        INSERT INTO {self.messages_table} (session_id, message_data) VALUES (?, ?)
                    """,
                        message_data,
                    )
                else:
                    # Structured storage: insert each item individually so we can capture rowid
                    current_span = get_current_span()
                    _trace_id = current_span.trace_id if current_span else None
                    _span_id = current_span.span_id if current_span else None

                    last_user_raw_event_id: int | None = None
                    assistant_seen_count = 0
                    for item in items:
                        raw_json = json.dumps(item)
                        cursor = conn.execute(
                            f"""
                            INSERT INTO {self.messages_table} (session_id, message_data)
                            VALUES (?, ?)
                            RETURNING id
                        """,
                            (self.session_id, raw_json),
                        )
                        raw_event_id = cursor.fetchone()[0]

                        # Handle structured inserts
                        if "role" in item:
                            role = item.get("role")
                            content_val = item.get("content")
                            try:
                                content_str = (
                                    json.dumps(content_val) if content_val is not None else None
                                )
                            except TypeError:
                                content_str = str(content_val)

                            parent_raw_event_id = (
                                last_user_raw_event_id if role == "assistant" else None
                            )

                            # Attribute assistant messages to the model response span if available
                            _msg_trace_id = _trace_id
                            _msg_span_id = _span_id
                            if role == "assistant":
                                try:
                                    maybe_span = get_last_response_span_for_trace(_trace_id)
                                    if maybe_span:
                                        _msg_trace_id, _msg_span_id = maybe_span
                                except Exception:
                                    pass

                            conn.execute(
                                f"""
                                INSERT INTO {self.conversation_table}
                                (
                                    session_id, raw_event_id, role, content,
                                    parent_raw_event_id, trace_id, span_id
                                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                                (
                                    self.session_id,
                                    raw_event_id,
                                    role,
                                    content_str,
                                    parent_raw_event_id,
                                    _msg_trace_id,
                                    _msg_span_id,
                                ),
                            )

                            if role == "user":
                                last_user_raw_event_id = raw_event_id
                            elif role == "assistant":
                                assistant_seen_count += 1

                        event_type = item.get("type")
                        if event_type == "function_call":
                            call_id = item.get("call_id")
                            tool_name = item.get("name")
                            arguments_val = item.get("arguments")
                            # If a precise function-span mapping exists, use it
                            if call_id:
                                mapped = pop_tool_call_span(
                                    str(call_id) if call_id is not None else ""
                                )
                                if mapped:
                                    _trace_id, _span_id = mapped
                            conn.execute(
                                f"""
                                INSERT INTO {self.tool_calls_table}
                                (
                                    session_id, raw_event_id, call_id, tool_name,
                                    arguments, status, trace_id, span_id
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                                (
                                    self.session_id,
                                    raw_event_id,
                                    call_id,
                                    tool_name,
                                    arguments_val,
                                    item.get("status"),
                                    _trace_id,
                                    _span_id,
                                ),
                            )
                        elif event_type == "function_call_output":
                            call_id = item.get("call_id")
                            output_val = item.get("output")
                            conn.execute(
                                f"""
                                UPDATE {self.tool_calls_table}
                                SET output = ?, status = 'completed'
                                WHERE session_id = ? AND call_id = ?
                            """,
                                (
                                    json.dumps(output_val) if output_val is not None else None,
                                    self.session_id,
                                    call_id,
                                ),
                            )

                # Update session timestamp
                conn.execute(
                    f"""
                    UPDATE {self.sessions_table}
                    SET updated_at = CURRENT_TIMESTAMP
                    WHERE session_id = ?
                """,
                    (self.session_id,),
                )

                conn.commit()

        await asyncio.to_thread(_add_items_sync)

    async def add_usage_records(self, responses: list[ModelResponse]) -> None:
        """Optionally store usage rows for a set of model responses.

        Best-effort and only active when structured_metadata=True. It is safe to call even if
        structured_metadata=False.
        """
        if not self.structured_metadata or not responses:
            return

        def _add_usage_sync():
            conn = self._get_connection()
            with self._lock if self._is_memory_db else threading.Lock():
                current_span = get_current_span()
                _trace_id = current_span.trace_id if current_span else None
                _span_id = current_span.span_id if current_span else None

                def _to_json_text(obj: object | None) -> str | None:
                    if obj is None:
                        return None
                    try:
                        return json.dumps(obj)
                    except TypeError:
                        # Try common object-to-dict conversions (e.g., Pydantic models)
                        try:
                            if hasattr(obj, "model_dump"):
                                return json.dumps(obj.model_dump())
                            if hasattr(obj, "dict"):
                                return json.dumps(obj.dict())
                            if hasattr(obj, "__dict__"):
                                return json.dumps(obj.__dict__)
                        except Exception:
                            pass
                        # Fallback to string representation
                        return json.dumps(str(obj))

                for resp in responses:
                    usage = getattr(resp, "usage", None)
                    response_id = getattr(resp, "response_id", None)
                    if usage is None:
                        continue

                    # Details may not be JSON-serializable; store as JSON-encoded strings
                    input_details = _to_json_text(getattr(usage, "input_tokens_details", None))
                    output_details = _to_json_text(getattr(usage, "output_tokens_details", None))

                    # Prefer the precise response span if available
                    _usage_trace_id = _trace_id
                    _usage_span_id = _span_id
                    try:
                        if response_id is not None:
                            mapped = get_response_span(response_id)
                            if mapped:
                                _usage_trace_id, _usage_span_id = mapped
                        else:
                            maybe = get_last_response_span_for_trace(_trace_id)
                            if maybe:
                                _usage_trace_id, _usage_span_id = maybe
                    except Exception:
                        pass

                    # Prefer model in response_id; fall back to last seen model for this trace.
                    _model_name: str | None = None
                    try:
                        if response_id is not None:
                            _model_name = get_response_model(response_id)
                        if _model_name is None:
                            _model_name = get_last_model_for_trace(_usage_trace_id)
                    except Exception:
                        pass

                    conn.execute(
                        f"""
                        INSERT INTO {self.usage_table} (
                            session_id, response_id, model, requests, input_tokens,
                            output_tokens, total_tokens, input_tokens_details,
                            output_tokens_details, trace_id, span_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            self.session_id,
                            response_id,
                            _model_name,
                            getattr(usage, "requests", None),
                            getattr(usage, "input_tokens", None),
                            getattr(usage, "output_tokens", None),
                            getattr(usage, "total_tokens", None),
                            input_details,
                            output_details,
                            _usage_trace_id,
                            _usage_span_id,
                        ),
                    )

                conn.commit()

        await asyncio.to_thread(_add_usage_sync)

    async def pop_item(self) -> TResponseInputItem | None:
        """Remove and return the most recent item from the session.

        Returns:
            The most recent item if it exists, None if the session is empty
        """

        def _pop_item_sync():
            conn = self._get_connection()
            with self._lock if self._is_memory_db else threading.Lock():
                # Use DELETE with RETURNING to atomically delete and return the most recent item
                cursor = conn.execute(
                    f"""
                    DELETE FROM {self.messages_table}
                    WHERE id = (
                        SELECT id FROM {self.messages_table}
                        WHERE session_id = ?
                        ORDER BY created_at DESC
                        LIMIT 1
                    )
                    RETURNING message_data
                    """,
                    (self.session_id,),
                )

                result = cursor.fetchone()

                conn.commit()

                if result:
                    message_data = result[0]
                    try:
                        item = json.loads(message_data)
                        return item
                    except json.JSONDecodeError:
                        return None

                return None

        return await asyncio.to_thread(_pop_item_sync)

    async def clear_session(self) -> None:
        """Clear all items for this session."""

        def _clear_session_sync():
            conn = self._get_connection()
            with self._lock if self._is_memory_db else threading.Lock():
                conn.execute(
                    f"DELETE FROM {self.messages_table} WHERE session_id = ?",
                    (self.session_id,),
                )
                conn.execute(
                    f"DELETE FROM {self.sessions_table} WHERE session_id = ?",
                    (self.session_id,),
                )
                conn.commit()

        await asyncio.to_thread(_clear_session_sync)

    def close(self) -> None:
        """Close the database connection."""
        if self._is_memory_db:
            if hasattr(self, "_shared_connection"):
                self._shared_connection.close()
        else:
            if hasattr(self._local, "connection"):
                self._local.connection.close()
