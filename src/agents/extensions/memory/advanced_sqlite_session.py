from __future__ import annotations

import asyncio
import json
import logging
import threading
from contextlib import closing
from pathlib import Path
from typing import Any, cast

from agents.result import RunResult
from agents.usage import Usage

from ...items import TResponseInputItem
from ...memory import SQLiteSession


class AdvancedSQLiteSession(SQLiteSession):
    """Enhanced SQLite session with turn tracking, soft deletion, and usage analytics.

    Features:
    - Turn-based conversation management with soft delete/reactivate
    - Detailed usage tracking per turn with token breakdowns
    - Message structure metadata and tool usage statistics
    """

    ACTIVE = 1  # Message is active and visible in conversation
    INACTIVE = 0  # Message is soft-deleted (hidden but preserved)

    def __init__(
        self,
        *,
        session_id: str,
        db_path: str | Path = ":memory:",
        create_tables: bool = False,
        logger: logging.Logger | None = None,
        **kwargs,
    ):
        super().__init__(session_id, db_path, **kwargs)
        if create_tables:
            self._init_structure_tables()
        self._current_user_turn = 0
        self._initialize_turn_counter()
        self._logger = logger or logging.getLogger(__name__)

    def _init_structure_tables(self):
        """Add structure and usage tracking tables."""
        conn = self._get_connection()

        # Simple message structure with soft deletion and precise ordering
        conn.execute("""
            CREATE TABLE IF NOT EXISTS message_structure (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                message_type TEXT NOT NULL,
                sequence_number INTEGER NOT NULL,
                user_turn_number INTEGER,
                tool_name TEXT,
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                deactivated_at TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id) ON DELETE CASCADE,
                FOREIGN KEY (message_id) REFERENCES agent_messages(id) ON DELETE CASCADE
            )
        """)

        # Turn-level usage tracking with full JSON details
        conn.execute("""
            CREATE TABLE IF NOT EXISTS turn_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                user_turn_number INTEGER NOT NULL,
                requests INTEGER DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                input_tokens_details JSON,
                output_tokens_details JSON,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id) ON DELETE CASCADE,
                UNIQUE(session_id, user_turn_number)
            )
        """)

        # Indexes
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_structure_session_seq
            ON message_structure(session_id, sequence_number)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_structure_active
            ON message_structure(session_id, is_active)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_structure_turn
            ON message_structure(session_id, user_turn_number, is_active)
        """)
        # Compound index for optimal performance on get_items queries
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_structure_active_seq
            ON message_structure(session_id, is_active, sequence_number)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_turn_usage_session_turn
            ON turn_usage(session_id, user_turn_number)
        """)

        conn.commit()

    def _initialize_turn_counter(self):
        """Initialize the turn counter based on existing database state."""
        conn = self._get_connection()

        # Get the highest user_turn_number for this session
        with closing(conn.cursor()) as cursor:
            cursor.execute(
                """
                SELECT COALESCE(MAX(user_turn_number), 0)
                FROM message_structure
                WHERE session_id = ? AND is_active = 1
            """,
                (self.session_id,),
            )
            max_turn = cursor.fetchone()[0]
            self._current_user_turn = max_turn

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        # Add to base table first
        await super().add_items(items)

        # Extract structure metadata with precise sequencing
        if items:
            await self._add_structure_metadata(items)

    async def store_run_usage(self, result: RunResult) -> None:
        """Store usage data for the current conversation turn.

        This is designed to be called after Runner.run() completes.
        Session-level usage can be aggregated from turn data when needed.
        """
        try:
            if result.context_wrapper.usage is not None:
                # Only update turn-level usage - session usage is aggregated on demand
                await self._update_turn_usage_internal(
                    self._current_user_turn, result.context_wrapper.usage
                )
        except Exception as e:
            self._logger.error(f"Failed to store usage for session {self.session_id}: {e}")

    async def _add_structure_metadata(self, items: list[TResponseInputItem]) -> None:
        """Extract structure metadata with microsecond precision ordering.

        This method:
        - Increments user turn counter for each user message encountered
        - Assigns explicit sequence numbers for precise ordering
        - Links messages to their database IDs for structure tracking
        - Handles multiple user messages in a single batch correctly
        """

        def _add_structure_sync():
            conn = self._get_connection()
            with self._lock if self._is_memory_db else threading.Lock():
                # Get the IDs of messages we just inserted, in order
                with closing(conn.cursor()) as cursor:
                    cursor.execute(
                        f"SELECT id FROM {self.messages_table} "
                        f"WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                        (self.session_id, len(items)),
                    )
                    message_ids = [row[0] for row in cursor.fetchall()]
                    message_ids.reverse()  # Match order of items

                # Get current max sequence number
                with closing(conn.cursor()) as cursor:
                    cursor.execute(
                        """
                        SELECT COALESCE(MAX(sequence_number), 0)
                        FROM message_structure
                        WHERE session_id = ? AND is_active = 1
                    """,
                        (self.session_id,),
                    )
                    seq_start = cursor.fetchone()[0]

                # Process items and assign turn numbers correctly
                # Each user message starts a new turn, subsequent items belong to that turn
                structure_data = []
                current_turn = self._current_user_turn

                for i, (item, msg_id) in enumerate(zip(items, message_ids)):
                    # If this is a user message, increment the turn counter
                    if self._is_user_message(item):
                        current_turn += 1
                        self._current_user_turn = current_turn

                    msg_type = self._classify_simple(item)
                    tool_name = self._extract_tool_name(item)

                    structure_data.append(
                        (
                            self.session_id,
                            msg_id,
                            msg_type,
                            seq_start + i + 1,  # Explicit sequence
                            current_turn,
                            tool_name,
                            self.ACTIVE,
                        )
                    )

                with closing(conn.cursor()) as cursor:
                    cursor.executemany(
                        """
                        INSERT INTO message_structure
                        (session_id, message_id, message_type, sequence_number, user_turn_number, tool_name, is_active)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,  # noqa: E501
                        structure_data,
                    )
                    conn.commit()

        try:
            await asyncio.to_thread(_add_structure_sync)
        except Exception as e:
            self._logger.error(
                f"Failed to add structure metadata for session {self.session_id}: {e}"
            )
            # Don't re-raise - structure metadata is supplementary

    def _classify_simple(self, item: TResponseInputItem) -> str:
        """Simple classification."""
        if isinstance(item, dict):
            if item.get("role") == "user":
                return "user"
            elif item.get("role") == "assistant":
                return "assistant"
            elif item.get("type"):
                return str(item.get("type"))
        return "other"

    def _extract_tool_name(self, item: TResponseInputItem) -> str | None:
        """Extract tool name if this is a tool call/output."""
        if isinstance(item, dict):
            item_type = item.get("type")

            # For MCP tools, try to extract from server_label if available
            if item_type in {"mcp_call", "mcp_approval_request"} and "server_label" in item:
                server_label = item.get("server_label")
                tool_name = item.get("name")
                if tool_name and server_label:
                    return f"{server_label}.{tool_name}"
                elif server_label:
                    return str(server_label)
                elif tool_name:
                    return str(tool_name)

            # For tool types without a 'name' field, derive from the type
            elif item_type in {
                "computer_call",
                "file_search_call",
                "web_search_call",
                "code_interpreter_call",
            }:
                return item_type

            # Most other tool calls have a 'name' field
            elif "name" in item:
                name = item.get("name")
                return str(name) if name is not None else None

        return None

    def _is_user_message(self, item: TResponseInputItem) -> bool:
        """Check if this is a user message."""
        return isinstance(item, dict) and item.get("role") == "user"

    async def get_items(
        self, limit: int | None = None, include_inactive: bool = False
    ) -> list[TResponseInputItem]:
        """Get items, optionally including soft-deleted ones."""
        if include_inactive:
            # Use parent implementation for all items
            return await super().get_items(limit)

        # Filter to only active items
        def _get_active_items_sync():
            conn = self._get_connection()
            with self._lock if self._is_memory_db else threading.Lock():
                with closing(conn.cursor()) as cursor:
                    # Get active message IDs in correct order
                    if limit is None:
                        cursor = conn.execute(
                            """
                            SELECT m.message_data
                            FROM agent_messages m
                            JOIN message_structure s ON m.id = s.message_id
                            WHERE m.session_id = ? AND s.is_active = 1
                            ORDER BY s.sequence_number ASC
                        """,
                            (self.session_id,),
                        )
                    else:
                        cursor = conn.execute(
                            """
                            SELECT m.message_data
                            FROM agent_messages m
                            JOIN message_structure s ON m.id = s.message_id
                            WHERE m.session_id = ? AND s.is_active = 1
                            ORDER BY s.sequence_number DESC
                            LIMIT ?
                        """,
                            (self.session_id, limit),
                        )

                    rows = cursor.fetchall()
                    if limit is not None:
                        rows = list(reversed(rows))

                items = []
                for (message_data,) in rows:
                    try:
                        item = json.loads(message_data)
                        items.append(item)
                    except json.JSONDecodeError:
                        continue
                return items

        return await asyncio.to_thread(_get_active_items_sync)

    async def soft_delete_from_turn(self, user_turn_number: int) -> bool:
        """Soft delete conversation from a specific user turn onwards."""

        def _soft_delete_sync():
            conn = self._get_connection()
            with self._lock if self._is_memory_db else threading.Lock():
                with closing(conn.cursor()) as cursor:
                    cursor.execute(
                        """
                        UPDATE message_structure
                        SET is_active = 0, deactivated_at = CURRENT_TIMESTAMP
                        WHERE session_id = ? AND user_turn_number >= ? AND is_active = 1
                    """,
                        (self.session_id, user_turn_number),
                    )
                    affected = cursor.rowcount
                    conn.commit()
                    return affected > 0

        return await asyncio.to_thread(_soft_delete_sync)

    async def reactivate_from_turn(self, user_turn_number: int) -> bool:
        """Reactivate soft-deleted conversation from a specific turn."""

        def _reactivate_sync():
            conn = self._get_connection()
            with self._lock if self._is_memory_db else threading.Lock():
                with closing(conn.cursor()) as cursor:
                    cursor.execute(
                        """
                        UPDATE message_structure
                        SET is_active = 1, deactivated_at = NULL
                        WHERE session_id = ? AND user_turn_number >= ? AND is_active = 0
                    """,
                        (self.session_id, user_turn_number),
                    )
                    affected = cursor.rowcount
                    conn.commit()
                    return affected > 0

        return await asyncio.to_thread(_reactivate_sync)

    async def _update_turn_usage_internal(self, user_turn_number: int, usage_data: Usage) -> None:
        """Internal method to update usage for a specific turn with full JSON details."""

        def _update_sync():
            conn = self._get_connection()
            with self._lock if self._is_memory_db else threading.Lock():
                # Serialize token details as JSON
                input_details_json = None
                output_details_json = None

                if hasattr(usage_data, "input_tokens_details") and usage_data.input_tokens_details:
                    try:
                        input_details_json = json.dumps(usage_data.input_tokens_details.__dict__)
                    except (TypeError, ValueError) as e:
                        self._logger.warning(f"Failed to serialize input tokens details: {e}")
                        input_details_json = None

                    if (
                        hasattr(usage_data, "output_tokens_details")
                        and usage_data.output_tokens_details
                    ):
                        try:
                            output_details_json = json.dumps(
                                usage_data.output_tokens_details.__dict__
                            )
                        except (TypeError, ValueError) as e:
                            self._logger.warning(f"Failed to serialize output tokens details: {e}")
                            output_details_json = None

                with closing(conn.cursor()) as cursor:
                    cursor.execute(
                        """
                        INSERT OR REPLACE INTO turn_usage
                        (session_id, user_turn_number, requests, input_tokens, output_tokens,
                         total_tokens, input_tokens_details, output_tokens_details)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            self.session_id,
                            user_turn_number,
                            usage_data.requests or 0,
                            usage_data.input_tokens or 0,
                            usage_data.output_tokens or 0,
                            usage_data.total_tokens or 0,
                            input_details_json,
                            output_details_json,
                        ),
                    )
                    conn.commit()

        await asyncio.to_thread(_update_sync)

    async def get_session_usage(self, active_only: bool = False) -> dict[str, int] | None:
        """Get current cumulative usage for this session by aggregating turn data.

        Args:
            active_only: If True, only include usage from turns with active messages.
                        If False, include all usage (preserves full cost accounting).
        """

        def _get_usage_sync():
            conn = self._get_connection()
            with self._lock if self._is_memory_db else threading.Lock():
                if active_only:
                    # Join with message_structure to filter by active turns only
                    query = """
                        SELECT
                            SUM(tu.requests) as total_requests,
                            SUM(tu.input_tokens) as total_input_tokens,
                            SUM(tu.output_tokens) as total_output_tokens,
                            SUM(tu.total_tokens) as total_total_tokens,
                            COUNT(DISTINCT tu.user_turn_number) as total_turns
                        FROM turn_usage tu
                        WHERE tu.session_id = ?
                        AND EXISTS (
                            SELECT 1 FROM message_structure ms
                            WHERE ms.session_id = tu.session_id
                            AND ms.user_turn_number = tu.user_turn_number
                            AND ms.is_active = 1
                        )
                    """
                else:
                    # Original query - all usage for full cost accounting
                    query = """
                        SELECT
                            SUM(requests) as total_requests,
                            SUM(input_tokens) as total_input_tokens,
                            SUM(output_tokens) as total_output_tokens,
                            SUM(total_tokens) as total_total_tokens,
                            COUNT(*) as total_turns
                        FROM turn_usage
                        WHERE session_id = ?
                    """

                with closing(conn.cursor()) as cursor:
                    cursor.execute(query, (self.session_id,))
                    row = cursor.fetchone()

                    if row and row[0] is not None:
                        return {
                            "requests": row[0] or 0,
                            "input_tokens": row[1] or 0,
                            "output_tokens": row[2] or 0,
                            "total_tokens": row[3] or 0,
                            "total_turns": row[4] or 0,
                        }
                    return None

        result = await asyncio.to_thread(_get_usage_sync)

        return cast(dict[str, int] | None, result)

    async def get_conversation_by_turns(
        self, include_inactive: bool = False
    ) -> dict[int, list[dict[str, str | bool | None]]]:
        """Get conversation grouped by user turns."""

        def _get_conversation_sync():
            conn = self._get_connection()

            active_filter = "" if include_inactive else "AND is_active = 1"
            with closing(conn.cursor()) as cursor:
                cursor.execute(
                    f"""
                    SELECT user_turn_number, message_type, tool_name, is_active
                    FROM message_structure
                    WHERE session_id = ? {active_filter}
                    ORDER BY sequence_number
                """,  # noqa: E501
                    (self.session_id,),
                )

                turns: dict[int, list[dict[str, str | bool | None]]] = {}
                for row in cursor.fetchall():
                    turn_num, msg_type, tool_name, is_active = row
                    if turn_num not in turns:
                        turns[turn_num] = []
                    turns[turn_num].append(
                        {"type": msg_type, "tool_name": tool_name, "active": bool(is_active)}
                    )
                return turns

        return await asyncio.to_thread(_get_conversation_sync)

    async def get_tool_usage(self, include_inactive: bool = False) -> list[tuple[str, int, int]]:
        """Get all tool usage by turn."""

        def _get_tool_usage_sync():
            conn = self._get_connection()

            active_filter = "" if include_inactive else "AND is_active = 1"
            with closing(conn.cursor()) as cursor:
                cursor.execute(
                    f"""
                    SELECT tool_name, COUNT(*), user_turn_number
                    FROM message_structure
                    WHERE session_id = ? AND message_type IN (
                        'tool_call', 'function_call', 'computer_call', 'file_search_call',
                        'web_search_call', 'code_interpreter_call', 'custom_tool_call',
                        'mcp_call', 'mcp_approval_request'
                    ) {active_filter}
                    GROUP BY tool_name, user_turn_number
                    ORDER BY user_turn_number
                """,  # noqa: E501
                    (self.session_id,),
                )
                return cursor.fetchall()

        return await asyncio.to_thread(_get_tool_usage_sync)

    async def get_turn_usage(
        self, user_turn_number: int | None = None, active_only: bool = False
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Get usage statistics by turn with full JSON token details.

        Args:
            user_turn_number: Specific turn to get usage for. If None, returns all turns.
            active_only: If True, only include usage from turns with active messages.
                        If False, include all usage (preserves full cost accounting).
        """

        def _get_turn_usage_sync():
            conn = self._get_connection()

            if user_turn_number is not None:
                if active_only:
                    query = """
                        SELECT tu.requests, tu.input_tokens, tu.output_tokens, tu.total_tokens,
                               tu.input_tokens_details, tu.output_tokens_details
                        FROM turn_usage tu
                        WHERE tu.session_id = ? AND tu.user_turn_number = ?
                        AND EXISTS (
                            SELECT 1 FROM message_structure ms
                            WHERE ms.session_id = tu.session_id
                            AND ms.user_turn_number = tu.user_turn_number
                            AND ms.is_active = 1
                        )
                    """
                else:
                    query = """
                        SELECT requests, input_tokens, output_tokens, total_tokens,
                               input_tokens_details, output_tokens_details
                        FROM turn_usage
                        WHERE session_id = ? AND user_turn_number = ?
                    """

                with closing(conn.cursor()) as cursor:
                    cursor.execute(query, (self.session_id, user_turn_number))
                    row = cursor.fetchone()

                    if row:
                        # Parse JSON details if present
                        input_details = None
                        output_details = None

                        if row[4]:  # input_tokens_details
                            try:
                                input_details = json.loads(row[4])
                            except json.JSONDecodeError:
                                pass

                        if row[5]:  # output_tokens_details
                            try:
                                output_details = json.loads(row[5])
                            except json.JSONDecodeError:
                                pass

                        return {
                            "requests": row[0],
                            "input_tokens": row[1],
                            "output_tokens": row[2],
                            "total_tokens": row[3],
                            "input_tokens_details": input_details,
                            "output_tokens_details": output_details,
                        }
                    return {}
            else:
                if active_only:
                    query = """
                        SELECT tu.user_turn_number, tu.requests, tu.input_tokens, tu.output_tokens,
                               tu.total_tokens, tu.input_tokens_details, tu.output_tokens_details
                        FROM turn_usage tu
                        WHERE tu.session_id = ?
                        AND EXISTS (
                            SELECT 1 FROM message_structure ms
                            WHERE ms.session_id = tu.session_id
                            AND ms.user_turn_number = tu.user_turn_number
                            AND ms.is_active = 1
                        )
                        ORDER BY tu.user_turn_number
                    """
                else:
                    query = """
                        SELECT user_turn_number, requests, input_tokens, output_tokens,
                               total_tokens, input_tokens_details, output_tokens_details
                        FROM turn_usage
                        WHERE session_id = ?
                        ORDER BY user_turn_number
                    """

                with closing(conn.cursor()) as cursor:
                    cursor.execute(query, (self.session_id,))
                    results = []
                    for row in cursor.fetchall():
                        # Parse JSON details if present
                        input_details = None
                        output_details = None

                        if row[5]:  # input_tokens_details
                            try:
                                input_details = json.loads(row[5])
                            except json.JSONDecodeError:
                                pass

                        if row[6]:  # output_tokens_details
                            try:
                                output_details = json.loads(row[6])
                            except json.JSONDecodeError:
                                pass

                        results.append(
                            {
                                "user_turn_number": row[0],
                                "requests": row[1],
                                "input_tokens": row[2],
                                "output_tokens": row[3],
                                "total_tokens": row[4],
                                "input_tokens_details": input_details,
                                "output_tokens_details": output_details,
                            }
                        )
                    return results

        result = await asyncio.to_thread(_get_turn_usage_sync)

        return cast(list[dict[str, Any]] | dict[str, Any], result)
