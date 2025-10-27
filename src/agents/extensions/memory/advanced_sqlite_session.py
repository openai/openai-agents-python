from __future__ import annotations

import asyncio
import json
import logging
import threading
from contextlib import closing
from pathlib import Path
from typing import Any, Union, cast

from agents.result import RunResult
from agents.usage import Usage

from ...items import TResponseInputItem
from ...memory import SQLiteSession


class AdvancedSQLiteSession(SQLiteSession):
    """Enhanced SQLite session with conversation branching and usage analytics."""

    def __init__(
        self,
        *,
        session_id: str,
        db_path: str | Path = ":memory:",
        create_tables: bool = False,
        logger: logging.Logger | None = None,
        **kwargs,
    ):
        """Initialize the AdvancedSQLiteSession.

        Args:
            session_id: The ID of the session
            db_path: The path to the SQLite database file. Defaults to `:memory:` for in-memory storage
            create_tables: Whether to create the structure tables
            logger: The logger to use. Defaults to the module logger
            **kwargs: Additional keyword arguments to pass to the superclass
        """  # noqa: E501
        super().__init__(session_id, db_path, **kwargs)
        if create_tables:
            self._init_structure_tables()
        self._current_branch_id = "main"
        self._logger = logger or logging.getLogger(__name__)

    def _init_structure_tables(self):
        """Add structure and usage tracking tables.

        Creates the message_structure and turn_usage tables with appropriate
        indexes for conversation branching and usage analytics.
        """
        conn = self._get_connection()

        # Message structure with branch support
        conn.execute("""
            CREATE TABLE IF NOT EXISTS message_structure (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                branch_id TEXT NOT NULL DEFAULT 'main',
                message_type TEXT NOT NULL,
                sequence_number INTEGER NOT NULL,
                user_turn_number INTEGER,
                branch_turn_number INTEGER,
                tool_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id) ON DELETE CASCADE,
                FOREIGN KEY (message_id) REFERENCES agent_messages(id) ON DELETE CASCADE
            )
        """)

        # Turn-level usage tracking with branch support and full JSON details
        conn.execute("""
            CREATE TABLE IF NOT EXISTS turn_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                branch_id TEXT NOT NULL DEFAULT 'main',
                user_turn_number INTEGER NOT NULL,
                requests INTEGER DEFAULT 0,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                input_tokens_details JSON,
                output_tokens_details JSON,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES agent_sessions(session_id) ON DELETE CASCADE,
                UNIQUE(session_id, branch_id, user_turn_number)
            )
        """)

        # Indexes
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_structure_session_seq
            ON message_structure(session_id, sequence_number)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_structure_branch
            ON message_structure(session_id, branch_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_structure_turn
            ON message_structure(session_id, branch_id, user_turn_number)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_structure_branch_seq
            ON message_structure(session_id, branch_id, sequence_number)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_turn_usage_session_turn
            ON turn_usage(session_id, branch_id, user_turn_number)
        """)

        conn.commit()

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Add items to the session.

        Args:
            items: The items to add to the session
        """
        # Add to base table first
        await super().add_items(items)

        # Extract structure metadata with precise sequencing
        if items:
            await self._add_structure_metadata(items)

    async def get_items(
        self,
        limit: int | None = None,
        branch_id: str | None = None,
    ) -> list[TResponseInputItem]:
        """Get items from current or specified branch.

        Args:
            limit: Maximum number of items to return. If None, returns all items.
            branch_id: Branch to get items from. If None, uses current branch.

        Returns:
            List of conversation items from the specified branch.
        """
        if branch_id is None:
            branch_id = self._current_branch_id

            # Get all items for this branch
            def _get_all_items_sync():
                """Synchronous helper to get all items for a branch."""
                conn = self._get_connection()
                # TODO: Refactor SQLiteSession to use asyncio.Lock instead of threading.Lock and update this code  # noqa: E501
                with self._lock if self._is_memory_db else threading.Lock():
                    with closing(conn.cursor()) as cursor:
                        if limit is None:
                            cursor.execute(
                                """
                                SELECT m.message_data
                                FROM agent_messages m
                                JOIN message_structure s ON m.id = s.message_id
                                WHERE m.session_id = ? AND s.branch_id = ?
                                ORDER BY s.sequence_number ASC
                            """,
                                (self.session_id, branch_id),
                            )
                        else:
                            cursor.execute(
                                """
                                SELECT m.message_data
                                FROM agent_messages m
                                JOIN message_structure s ON m.id = s.message_id
                                WHERE m.session_id = ? AND s.branch_id = ?
                                ORDER BY s.sequence_number DESC
                                LIMIT ?
                            """,
                                (self.session_id, branch_id, limit),
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

            return await asyncio.to_thread(_get_all_items_sync)

        def _get_items_sync():
            """Synchronous helper to get items for a specific branch."""
            conn = self._get_connection()
            # TODO: Refactor SQLiteSession to use asyncio.Lock instead of threading.Lock and update this code  # noqa: E501
            with self._lock if self._is_memory_db else threading.Lock():
                with closing(conn.cursor()) as cursor:
                    # Get message IDs in correct order for this branch
                    if limit is None:
                        cursor.execute(
                            """
                            SELECT m.message_data
                            FROM agent_messages m
                            JOIN message_structure s ON m.id = s.message_id
                            WHERE m.session_id = ? AND s.branch_id = ?
                            ORDER BY s.sequence_number ASC
                        """,
                            (self.session_id, branch_id),
                        )
                    else:
                        cursor.execute(
                            """
                            SELECT m.message_data
                            FROM agent_messages m
                            JOIN message_structure s ON m.id = s.message_id
                            WHERE m.session_id = ? AND s.branch_id = ?
                            ORDER BY s.sequence_number DESC
                            LIMIT ?
                        """,
                            (self.session_id, branch_id, limit),
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

        return await asyncio.to_thread(_get_items_sync)

    async def store_run_usage(self, result: RunResult) -> None:
        """Store usage data for the current conversation turn.

        This is designed to be called after `Runner.run()` completes.
        Session-level usage can be aggregated from turn data when needed.

        Args:
            result: The result from the run
        """
        try:
            if result.context_wrapper.usage is not None:
                # Get the current turn number for this branch
                current_turn = self._get_current_turn_number()
                # Only update turn-level usage - session usage is aggregated on demand
                await self._update_turn_usage_internal(current_turn, result.context_wrapper.usage)
        except Exception as e:
            self._logger.error(f"Failed to store usage for session {self.session_id}: {e}")

    def _get_next_turn_number(self, branch_id: str) -> int:
        """Get the next turn number for a specific branch.

        Args:
            branch_id: The branch ID to get the next turn number for.

        Returns:
            The next available turn number for the specified branch.
        """
        conn = self._get_connection()
        with closing(conn.cursor()) as cursor:
            cursor.execute(
                """
                SELECT COALESCE(MAX(user_turn_number), 0)
                FROM message_structure
                WHERE session_id = ? AND branch_id = ?
            """,
                (self.session_id, branch_id),
            )
            result = cursor.fetchone()
            max_turn = result[0] if result else 0
            return max_turn + 1

    def _get_next_branch_turn_number(self, branch_id: str) -> int:
        """Get the next branch turn number for a specific branch.

        Args:
            branch_id: The branch ID to get the next branch turn number for.

        Returns:
            The next available branch turn number for the specified branch.
        """
        conn = self._get_connection()
        with closing(conn.cursor()) as cursor:
            cursor.execute(
                """
                SELECT COALESCE(MAX(branch_turn_number), 0)
                FROM message_structure
                WHERE session_id = ? AND branch_id = ?
            """,
                (self.session_id, branch_id),
            )
            result = cursor.fetchone()
            max_turn = result[0] if result else 0
            return max_turn + 1

    def _get_current_turn_number(self) -> int:
        """Get the current turn number for the current branch.

        Returns:
            The current turn number for the active branch.
        """
        conn = self._get_connection()
        with closing(conn.cursor()) as cursor:
            cursor.execute(
                """
                SELECT COALESCE(MAX(user_turn_number), 0)
                FROM message_structure
                WHERE session_id = ? AND branch_id = ?
                """,
                (self.session_id, self._current_branch_id),
            )
            result = cursor.fetchone()
            return result[0] if result else 0

    async def _add_structure_metadata(self, items: list[TResponseInputItem]) -> None:
        """Extract structure metadata with branch-aware turn tracking.

        This method:
        - Assigns turn numbers per branch (not globally)
        - Assigns explicit sequence numbers for precise ordering
        - Links messages to their database IDs for structure tracking
        - Handles multiple user messages in a single batch correctly

        Args:
            items: The items to add to the session
        """

        def _add_structure_sync():
            """Synchronous helper to add structure metadata to database."""
            conn = self._get_connection()
            # TODO: Refactor SQLiteSession to use asyncio.Lock instead of threading.Lock and update this code  # noqa: E501
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

                # Get current max sequence number (global)
                with closing(conn.cursor()) as cursor:
                    cursor.execute(
                        """
                        SELECT COALESCE(MAX(sequence_number), 0)
                        FROM message_structure
                        WHERE session_id = ?
                    """,
                        (self.session_id,),
                    )
                    seq_start = cursor.fetchone()[0]

                # Get current turn numbers atomically with a single query
                with closing(conn.cursor()) as cursor:
                    cursor.execute(
                        """
                        SELECT
                            COALESCE(MAX(user_turn_number), 0) as max_global_turn,
                            COALESCE(MAX(branch_turn_number), 0) as max_branch_turn
                        FROM message_structure
                        WHERE session_id = ? AND branch_id = ?
                    """,
                        (self.session_id, self._current_branch_id),
                    )
                    result = cursor.fetchone()
                    current_turn = result[0] if result else 0
                    current_branch_turn = result[1] if result else 0

                # Process items and assign turn numbers correctly
                structure_data = []
                user_message_count = 0

                for i, (item, msg_id) in enumerate(zip(items, message_ids)):
                    msg_type = self._classify_message_type(item)
                    tool_name = self._extract_tool_name(item)

                    # If this is a user message, increment turn counters
                    if self._is_user_message(item):
                        user_message_count += 1
                        item_turn = current_turn + user_message_count
                        item_branch_turn = current_branch_turn + user_message_count
                    else:
                        # Non-user messages inherit the turn number of the most recent user message
                        item_turn = current_turn + user_message_count
                        item_branch_turn = current_branch_turn + user_message_count

                    structure_data.append(
                        (
                            self.session_id,
                            msg_id,
                            self._current_branch_id,
                            msg_type,
                            seq_start + i + 1,  # Global sequence
                            item_turn,  # Global turn number
                            item_branch_turn,  # Branch-specific turn number
                            tool_name,
                        )
                    )

                with closing(conn.cursor()) as cursor:
                    cursor.executemany(
                        """
                        INSERT INTO message_structure
                        (session_id, message_id, branch_id, message_type, sequence_number,
                         user_turn_number, branch_turn_number, tool_name)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        structure_data,
                    )
                    conn.commit()

        try:
            await asyncio.to_thread(_add_structure_sync)
        except Exception as e:
            self._logger.error(
                f"Failed to add structure metadata for session {self.session_id}: {e}"
            )
            # Try to clean up any orphaned messages to maintain consistency
            try:
                await self._cleanup_orphaned_messages()
            except Exception as cleanup_error:
                self._logger.error(f"Failed to cleanup orphaned messages: {cleanup_error}")
            # Don't re-raise - structure metadata is supplementary

    async def _cleanup_orphaned_messages(self) -> None:
        """Remove messages that exist in agent_messages but not in message_structure.

        This can happen if _add_structure_metadata fails after super().add_items() succeeds.
        Used for maintaining data consistency.
        """

        def _cleanup_sync():
            """Synchronous helper to cleanup orphaned messages."""
            conn = self._get_connection()
            # TODO: Refactor SQLiteSession to use asyncio.Lock instead of threading.Lock and update this code  # noqa: E501
            with self._lock if self._is_memory_db else threading.Lock():
                with closing(conn.cursor()) as cursor:
                    # Find messages without structure metadata
                    cursor.execute(
                        """
                        SELECT am.id
                        FROM agent_messages am
                        LEFT JOIN message_structure ms ON am.id = ms.message_id
                        WHERE am.session_id = ? AND ms.message_id IS NULL
                    """,
                        (self.session_id,),
                    )

                    orphaned_ids = [row[0] for row in cursor.fetchall()]

                    if orphaned_ids:
                        # Delete orphaned messages
                        placeholders = ",".join("?" * len(orphaned_ids))
                        cursor.execute(
                            f"DELETE FROM agent_messages WHERE id IN ({placeholders})", orphaned_ids
                        )

                        deleted_count = cursor.rowcount
                        conn.commit()

                        self._logger.info(f"Cleaned up {deleted_count} orphaned messages")
                        return deleted_count

                    return 0

        return await asyncio.to_thread(_cleanup_sync)

    def _classify_message_type(self, item: TResponseInputItem) -> str:
        """Classify the type of a message item.

        Args:
            item: The message item to classify.

        Returns:
            String representing the message type (user, assistant, etc.).
        """
        if isinstance(item, dict):
            if item.get("role") == "user":
                return "user"
            elif item.get("role") == "assistant":
                return "assistant"
            elif item.get("type"):
                return str(item.get("type"))
        return "other"

    def _extract_tool_name(self, item: TResponseInputItem) -> str | None:
        """Extract tool name if this is a tool call/output.

        Args:
            item: The message item to extract tool name from.

        Returns:
            Tool name if item is a tool call, None otherwise.
        """
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
        """Check if this is a user message.

        Args:
            item: The message item to check.

        Returns:
            True if the item is a user message, False otherwise.
        """
        return isinstance(item, dict) and item.get("role") == "user"

    async def create_branch_from_turn(
        self, turn_number: int, branch_name: str | None = None
    ) -> str:
        """Create a new branch starting from a specific user message turn.

        Args:
            turn_number: The branch turn number of the user message to branch from
            branch_name: Optional name for the branch (auto-generated if None)

        Returns:
            The branch_id of the newly created branch

        Raises:
            ValueError: If turn doesn't exist or doesn't contain a user message
        """
        import time

        # Validate the turn exists and contains a user message
        def _validate_turn():
            """Synchronous helper to validate turn exists and contains user message."""
            conn = self._get_connection()
            with closing(conn.cursor()) as cursor:
                cursor.execute(
                    """
                    SELECT am.message_data
                    FROM message_structure ms
                    JOIN agent_messages am ON ms.message_id = am.id
                    WHERE ms.session_id = ? AND ms.branch_id = ?
                    AND ms.branch_turn_number = ? AND ms.message_type = 'user'
                    """,
                    (self.session_id, self._current_branch_id, turn_number),
                )

                result = cursor.fetchone()
                if not result:
                    raise ValueError(
                        f"Turn {turn_number} does not contain a user message "
                        f"in branch '{self._current_branch_id}'"
                    )

                message_data = result[0]
                try:
                    content = json.loads(message_data).get("content", "")
                    return content[:50] + "..." if len(content) > 50 else content
                except Exception:
                    return "Unable to parse content"

        turn_content = await asyncio.to_thread(_validate_turn)

        # Generate branch name if not provided
        if branch_name is None:
            timestamp = int(time.time())
            branch_name = f"branch_from_turn_{turn_number}_{timestamp}"

        # Copy messages before the branch point to the new branch
        await self._copy_messages_to_new_branch(branch_name, turn_number)

        # Switch to new branch
        old_branch = self._current_branch_id
        self._current_branch_id = branch_name

        self._logger.debug(
            f"Created branch '{branch_name}' from turn {turn_number} ('{turn_content}') in '{old_branch}'"  # noqa: E501
        )
        return branch_name

    async def create_branch_from_content(
        self, search_term: str, branch_name: str | None = None
    ) -> str:
        """Create branch from the first user turn matching the search term.

        Args:
            search_term: Text to search for in user messages.
            branch_name: Optional name for the branch (auto-generated if None).

        Returns:
            The branch_id of the newly created branch.

        Raises:
            ValueError: If no matching turns are found.
        """
        matching_turns = await self.find_turns_by_content(search_term)
        if not matching_turns:
            raise ValueError(f"No user turns found containing '{search_term}'")

        # Use the first (earliest) match
        turn_number = matching_turns[0]["turn"]
        return await self.create_branch_from_turn(turn_number, branch_name)

    async def switch_to_branch(self, branch_id: str) -> None:
        """Switch to a different branch.

        Args:
            branch_id: The branch to switch to.

        Raises:
            ValueError: If the branch doesn't exist.
        """

        # Validate branch exists
        def _validate_branch():
            """Synchronous helper to validate branch exists."""
            conn = self._get_connection()
            with closing(conn.cursor()) as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM message_structure
                    WHERE session_id = ? AND branch_id = ?
                """,
                    (self.session_id, branch_id),
                )

                count = cursor.fetchone()[0]
                if count == 0:
                    raise ValueError(f"Branch '{branch_id}' does not exist")

        await asyncio.to_thread(_validate_branch)

        old_branch = self._current_branch_id
        self._current_branch_id = branch_id
        self._logger.info(f"Switched from branch '{old_branch}' to '{branch_id}'")

    async def delete_branch(self, branch_id: str, force: bool = False) -> None:
        """Delete a branch and all its associated data.

        Args:
            branch_id: The branch to delete.
            force: If True, allows deleting the current branch (will switch to 'main').

        Raises:
            ValueError: If branch doesn't exist, is 'main', or is current branch without force.
        """
        if not branch_id or not branch_id.strip():
            raise ValueError("Branch ID cannot be empty")

        branch_id = branch_id.strip()

        # Protect main branch
        if branch_id == "main":
            raise ValueError("Cannot delete the 'main' branch")

        # Check if trying to delete current branch
        if branch_id == self._current_branch_id:
            if not force:
                raise ValueError(
                    f"Cannot delete current branch '{branch_id}'. Use force=True or switch branches first"  # noqa: E501
                )
            else:
                # Switch to main before deleting
                await self.switch_to_branch("main")

        def _delete_sync():
            """Synchronous helper to delete branch and associated data."""
            conn = self._get_connection()
            # TODO: Refactor SQLiteSession to use asyncio.Lock instead of threading.Lock and update this code  # noqa: E501
            with self._lock if self._is_memory_db else threading.Lock():
                with closing(conn.cursor()) as cursor:
                    # First verify the branch exists
                    cursor.execute(
                        """
                        SELECT COUNT(*) FROM message_structure
                        WHERE session_id = ? AND branch_id = ?
                    """,
                        (self.session_id, branch_id),
                    )

                    count = cursor.fetchone()[0]
                    if count == 0:
                        raise ValueError(f"Branch '{branch_id}' does not exist")

                    # Delete from turn_usage first (foreign key constraint)
                    cursor.execute(
                        """
                        DELETE FROM turn_usage
                        WHERE session_id = ? AND branch_id = ?
                    """,
                        (self.session_id, branch_id),
                    )

                    usage_deleted = cursor.rowcount

                    # Delete from message_structure
                    cursor.execute(
                        """
                        DELETE FROM message_structure
                        WHERE session_id = ? AND branch_id = ?
                    """,
                        (self.session_id, branch_id),
                    )

                    structure_deleted = cursor.rowcount

                    conn.commit()

                    return usage_deleted, structure_deleted

        usage_deleted, structure_deleted = await asyncio.to_thread(_delete_sync)

        self._logger.info(
            f"Deleted branch '{branch_id}': {structure_deleted} message entries, {usage_deleted} usage entries"  # noqa: E501
        )

    async def list_branches(self) -> list[dict[str, Any]]:
        """List all branches in this session.

        Returns:
            List of dicts with branch info containing:
                - 'branch_id': Branch identifier
                - 'message_count': Number of messages in branch
                - 'user_turns': Number of user turns in branch
                - 'is_current': Whether this is the current branch
                - 'created_at': When the branch was first created
        """

        def _list_branches_sync():
            """Synchronous helper to list all branches."""
            conn = self._get_connection()
            with closing(conn.cursor()) as cursor:
                cursor.execute(
                    """
                    SELECT
                        ms.branch_id,
                        COUNT(*) as message_count,
                        COUNT(CASE WHEN ms.message_type = 'user' THEN 1 END) as user_turns,
                        MIN(ms.created_at) as created_at
                    FROM message_structure ms
                    WHERE ms.session_id = ?
                    GROUP BY ms.branch_id
                    ORDER BY created_at
                """,
                    (self.session_id,),
                )

                branches = []
                for row in cursor.fetchall():
                    branch_id, msg_count, user_turns, created_at = row
                    branches.append(
                        {
                            "branch_id": branch_id,
                            "message_count": msg_count,
                            "user_turns": user_turns,
                            "is_current": branch_id == self._current_branch_id,
                            "created_at": created_at,
                        }
                    )

                return branches

        return await asyncio.to_thread(_list_branches_sync)

    async def _copy_messages_to_new_branch(self, new_branch_id: str, from_turn_number: int) -> None:
        """Copy messages before the branch point to the new branch.

        Args:
            new_branch_id: The ID of the new branch to copy messages to.
            from_turn_number: The turn number to copy messages up to (exclusive).
        """

        def _copy_sync():
            """Synchronous helper to copy messages to new branch."""
            conn = self._get_connection()
            # TODO: Refactor SQLiteSession to use asyncio.Lock instead of threading.Lock and update this code  # noqa: E501
            with self._lock if self._is_memory_db else threading.Lock():
                with closing(conn.cursor()) as cursor:
                    # Get all messages before the branch point
                    cursor.execute(
                        """
                        SELECT
                            ms.message_id,
                            ms.message_type,
                            ms.sequence_number,
                            ms.user_turn_number,
                            ms.branch_turn_number,
                            ms.tool_name
                        FROM message_structure ms
                        WHERE ms.session_id = ? AND ms.branch_id = ?
                        AND ms.branch_turn_number < ?
                        ORDER BY ms.sequence_number
                    """,
                        (self.session_id, self._current_branch_id, from_turn_number),
                    )

                    messages_to_copy = cursor.fetchall()

                    if messages_to_copy:
                        # Get the max sequence number for the new inserts
                        cursor.execute(
                            """
                            SELECT COALESCE(MAX(sequence_number), 0)
                            FROM message_structure
                            WHERE session_id = ?
                        """,
                            (self.session_id,),
                        )

                        seq_start = cursor.fetchone()[0]

                        # Insert copied messages with new branch_id
                        new_structure_data = []
                        for i, (
                            msg_id,
                            msg_type,
                            _,
                            user_turn,
                            branch_turn,
                            tool_name,
                        ) in enumerate(messages_to_copy):
                            new_structure_data.append(
                                (
                                    self.session_id,
                                    msg_id,  # Same message_id (sharing the actual message data)
                                    new_branch_id,
                                    msg_type,
                                    seq_start + i + 1,  # New sequence number
                                    user_turn,  # Keep same global turn number
                                    branch_turn,  # Keep same branch turn number
                                    tool_name,
                                )
                            )

                        cursor.executemany(
                            """
                            INSERT INTO message_structure
                            (session_id, message_id, branch_id, message_type, sequence_number,
                             user_turn_number, branch_turn_number, tool_name)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                            new_structure_data,
                        )

                    conn.commit()

        await asyncio.to_thread(_copy_sync)

    async def get_conversation_turns(self, branch_id: str | None = None) -> list[dict[str, Any]]:
        """Get user turns with content for easy browsing and branching decisions.

        Args:
            branch_id: Branch to get turns from (current branch if None).

        Returns:
            List of dicts with turn info containing:
                - 'turn': Branch turn number
                - 'content': User message content (truncated)
                - 'full_content': Full user message content
                - 'timestamp': When the turn was created
                - 'can_branch': Always True (all user messages can branch)
        """
        if branch_id is None:
            branch_id = self._current_branch_id

        def _get_turns_sync():
            """Synchronous helper to get conversation turns."""
            conn = self._get_connection()
            with closing(conn.cursor()) as cursor:
                cursor.execute(
                    """
                    SELECT
                        ms.branch_turn_number,
                        am.message_data,
                        ms.created_at
                    FROM message_structure ms
                    JOIN agent_messages am ON ms.message_id = am.id
                    WHERE ms.session_id = ? AND ms.branch_id = ?
                    AND ms.message_type = 'user'
                    ORDER BY ms.branch_turn_number
                """,
                    (self.session_id, branch_id),
                )

                turns = []
                for row in cursor.fetchall():
                    turn_num, message_data, created_at = row
                    try:
                        content = json.loads(message_data).get("content", "")
                        turns.append(
                            {
                                "turn": turn_num,
                                "content": content[:100] + "..." if len(content) > 100 else content,
                                "full_content": content,
                                "timestamp": created_at,
                                "can_branch": True,
                            }
                        )
                    except (json.JSONDecodeError, AttributeError):
                        continue

                return turns

        return await asyncio.to_thread(_get_turns_sync)

    async def find_turns_by_content(
        self, search_term: str, branch_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Find user turns containing specific content.

        Args:
            search_term: Text to search for in user messages.
            branch_id: Branch to search in (current branch if None).

        Returns:
            List of matching turns with same format as get_conversation_turns().
        """
        if branch_id is None:
            branch_id = self._current_branch_id

        def _search_sync():
            """Synchronous helper to search turns by content."""
            conn = self._get_connection()
            with closing(conn.cursor()) as cursor:
                cursor.execute(
                    """
                    SELECT
                        ms.branch_turn_number,
                        am.message_data,
                        ms.created_at
                    FROM message_structure ms
                    JOIN agent_messages am ON ms.message_id = am.id
                    WHERE ms.session_id = ? AND ms.branch_id = ?
                    AND ms.message_type = 'user'
                    AND am.message_data LIKE ?
                    ORDER BY ms.branch_turn_number
                """,
                    (self.session_id, branch_id, f"%{search_term}%"),
                )

                matches = []
                for row in cursor.fetchall():
                    turn_num, message_data, created_at = row
                    try:
                        content = json.loads(message_data).get("content", "")
                        matches.append(
                            {
                                "turn": turn_num,
                                "content": content,
                                "full_content": content,
                                "timestamp": created_at,
                                "can_branch": True,
                            }
                        )
                    except (json.JSONDecodeError, AttributeError):
                        continue

                return matches

        return await asyncio.to_thread(_search_sync)

    async def get_conversation_by_turns(
        self, branch_id: str | None = None
    ) -> dict[int, list[dict[str, str | None]]]:
        """Get conversation grouped by user turns for specified branch.

        Args:
            branch_id: Branch to get conversation from (current branch if None).

        Returns:
            Dictionary mapping turn numbers to lists of message metadata.
        """
        if branch_id is None:
            branch_id = self._current_branch_id

        def _get_conversation_sync():
            """Synchronous helper to get conversation by turns."""
            conn = self._get_connection()
            with closing(conn.cursor()) as cursor:
                cursor.execute(
                    """
                    SELECT user_turn_number, message_type, tool_name
                    FROM message_structure
                    WHERE session_id = ? AND branch_id = ?
                    ORDER BY sequence_number
                """,
                    (self.session_id, branch_id),
                )

                turns: dict[int, list[dict[str, str | None]]] = {}
                for row in cursor.fetchall():
                    turn_num, msg_type, tool_name = row
                    if turn_num not in turns:
                        turns[turn_num] = []
                    turns[turn_num].append({"type": msg_type, "tool_name": tool_name})
                return turns

        return await asyncio.to_thread(_get_conversation_sync)

    async def get_tool_usage(self, branch_id: str | None = None) -> list[tuple[str, int, int]]:
        """Get all tool usage by turn for specified branch.

        Args:
            branch_id: Branch to get tool usage from (current branch if None).

        Returns:
            List of tuples containing (tool_name, usage_count, turn_number).
        """
        if branch_id is None:
            branch_id = self._current_branch_id

        def _get_tool_usage_sync():
            """Synchronous helper to get tool usage statistics."""
            conn = self._get_connection()
            with closing(conn.cursor()) as cursor:
                cursor.execute(
                    """
                    SELECT tool_name, COUNT(*), user_turn_number
                    FROM message_structure
                    WHERE session_id = ? AND branch_id = ? AND message_type IN (
                        'tool_call', 'function_call', 'computer_call', 'file_search_call',
                        'web_search_call', 'code_interpreter_call', 'custom_tool_call',
                        'mcp_call', 'mcp_approval_request'
                    )
                    GROUP BY tool_name, user_turn_number
                    ORDER BY user_turn_number
                """,
                    (self.session_id, branch_id),
                )
                return cursor.fetchall()

        return await asyncio.to_thread(_get_tool_usage_sync)

    async def get_session_usage(self, branch_id: str | None = None) -> dict[str, int] | None:
        """Get cumulative usage for session or specific branch.

        Args:
            branch_id: If provided, only get usage for that branch. If None, get all branches.

        Returns:
            Dictionary with usage statistics or None if no usage data found.
        """

        def _get_usage_sync():
            """Synchronous helper to get session usage data."""
            conn = self._get_connection()
            # TODO: Refactor SQLiteSession to use asyncio.Lock instead of threading.Lock and update this code  # noqa: E501
            with self._lock if self._is_memory_db else threading.Lock():
                if branch_id:
                    # Branch-specific usage
                    query = """
                        SELECT
                            SUM(requests) as total_requests,
                            SUM(input_tokens) as total_input_tokens,
                            SUM(output_tokens) as total_output_tokens,
                            SUM(total_tokens) as total_total_tokens,
                            COUNT(*) as total_turns
                        FROM turn_usage
                        WHERE session_id = ? AND branch_id = ?
                    """
                    params: tuple[str, ...] = (self.session_id, branch_id)
                else:
                    # All branches
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
                    params = (self.session_id,)

                with closing(conn.cursor()) as cursor:
                    cursor.execute(query, params)
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

        return cast(Union[dict[str, int], None], result)

    async def get_turn_usage(
        self,
        user_turn_number: int | None = None,
        branch_id: str | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Get usage statistics by turn with full JSON token details.

        Args:
            user_turn_number: Specific turn to get usage for. If None, returns all turns.
            branch_id: Branch to get usage from (current branch if None).

        Returns:
            Dictionary with usage data for specific turn, or list of dictionaries for all turns.
        """

        if branch_id is None:
            branch_id = self._current_branch_id

        def _get_turn_usage_sync():
            """Synchronous helper to get turn usage statistics."""
            conn = self._get_connection()

            if user_turn_number is not None:
                query = """
                    SELECT requests, input_tokens, output_tokens, total_tokens,
                           input_tokens_details, output_tokens_details
                    FROM turn_usage
                    WHERE session_id = ? AND branch_id = ? AND user_turn_number = ?
                """

                with closing(conn.cursor()) as cursor:
                    cursor.execute(query, (self.session_id, branch_id, user_turn_number))
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
                query = """
                    SELECT user_turn_number, requests, input_tokens, output_tokens,
                           total_tokens, input_tokens_details, output_tokens_details
                    FROM turn_usage
                    WHERE session_id = ? AND branch_id = ?
                    ORDER BY user_turn_number
                """

                with closing(conn.cursor()) as cursor:
                    cursor.execute(query, (self.session_id, branch_id))
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

        return cast(Union[list[dict[str, Any]], dict[str, Any]], result)

    async def _update_turn_usage_internal(self, user_turn_number: int, usage_data: Usage) -> None:
        """Internal method to update usage for a specific turn with full JSON details.

        Args:
            user_turn_number: The turn number to update usage for.
            usage_data: The usage data to store.
        """

        def _update_sync():
            """Synchronous helper to update turn usage data."""
            conn = self._get_connection()
            # TODO: Refactor SQLiteSession to use asyncio.Lock instead of threading.Lock and update this code  # noqa: E501
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
                        (session_id, branch_id, user_turn_number, requests, input_tokens, output_tokens,
                         total_tokens, input_tokens_details, output_tokens_details)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,  # noqa: E501
                        (
                            self.session_id,
                            self._current_branch_id,
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
