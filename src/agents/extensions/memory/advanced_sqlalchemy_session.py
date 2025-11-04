from __future__ import annotations

import json
import logging
import time
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    case,
    delete,
    func,
    insert,
    select,
    text as sql_text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from agents.result import RunResult
from agents.usage import Usage

from ...items import TResponseInputItem
from .sqlalchemy_session import SQLAlchemySession


class AdvancedSQLAlchemySession(SQLAlchemySession):
    """SQLAlchemy implementation of the advanced session with branching and usage tracking."""

    _message_structure: Table
    _turn_usage: Table

    def __init__(
        self,
        session_id: str,
        *,
        engine: AsyncEngine,
        create_tables: bool = False,
        sessions_table: str = "agent_sessions",
        messages_table: str = "agent_messages",
        structure_table: str = "message_structure",
        turn_usage_table: str = "turn_usage",
        logger: logging.Logger | None = None,
    ):
        """Initialize the AdvancedSQLAlchemySession."""
        super().__init__(
            session_id,
            engine=engine,
            create_tables=create_tables,
            sessions_table=sessions_table,
            messages_table=messages_table,
        )

        self._message_structure = Table(
            structure_table,
            self._metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column(
                "session_id",
                String,
                ForeignKey(f"{self._sessions.name}.session_id", ondelete="CASCADE"),
                nullable=False,
            ),
            Column(
                "message_id",
                Integer,
                ForeignKey(f"{self._messages.name}.id", ondelete="CASCADE"),
                nullable=False,
            ),
            Column("branch_id", String, nullable=False, server_default="main"),
            Column("message_type", String, nullable=False),
            Column("sequence_number", Integer, nullable=False),
            Column("user_turn_number", Integer),
            Column("branch_turn_number", Integer),
            Column("tool_name", String),
            Column(
                "created_at",
                TIMESTAMP(timezone=False),
                server_default=sql_text("CURRENT_TIMESTAMP"),
            ),
            sqlite_autoincrement=True,
        )

        Index(
            f"idx_{structure_table}_session_seq",
            self._message_structure.c.session_id,
            self._message_structure.c.sequence_number,
        )
        Index(
            f"idx_{structure_table}_branch",
            self._message_structure.c.session_id,
            self._message_structure.c.branch_id,
        )
        Index(
            f"idx_{structure_table}_branch_turn",
            self._message_structure.c.session_id,
            self._message_structure.c.branch_id,
            self._message_structure.c.user_turn_number,
        )
        Index(
            f"idx_{structure_table}_branch_seq",
            self._message_structure.c.session_id,
            self._message_structure.c.branch_id,
            self._message_structure.c.sequence_number,
        )

        self._turn_usage = Table(
            turn_usage_table,
            self._metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column(
                "session_id",
                String,
                ForeignKey(f"{self._sessions.name}.session_id", ondelete="CASCADE"),
                nullable=False,
            ),
            Column("branch_id", String, nullable=False, server_default="main"),
            Column("user_turn_number", Integer, nullable=False),
            Column("requests", Integer, nullable=False, server_default="0"),
            Column("input_tokens", Integer, nullable=False, server_default="0"),
            Column("output_tokens", Integer, nullable=False, server_default="0"),
            Column("total_tokens", Integer, nullable=False, server_default="0"),
            Column("input_tokens_details", Text),
            Column("output_tokens_details", Text),
            Column(
                "created_at",
                TIMESTAMP(timezone=False),
                server_default=sql_text("CURRENT_TIMESTAMP"),
            ),
            UniqueConstraint(
                "session_id",
                "branch_id",
                "user_turn_number",
                name=f"uq_{turn_usage_table}_turn",
            ),
            sqlite_autoincrement=True,
        )

        Index(
            f"idx_{turn_usage_table}_session_turn",
            self._turn_usage.c.session_id,
            self._turn_usage.c.branch_id,
            self._turn_usage.c.user_turn_number,
        )

        self._current_branch_id = "main"
        self._logger = logger or logging.getLogger(__name__)

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        """Add items to the session.

        Args:
            items: The items to add to the session
        """
        if not items:
            return

        await self._ensure_tables()
        async with self._lock:
            await super().add_items(items)
            try:
                await self._add_structure_metadata(items)
            except Exception as exc:  # pragma: no cover - defensive
                self._logger.error(
                    "Failed to add structure metadata for session %s: %s",
                    self.session_id,
                    exc,
                )
                try:
                    await self._cleanup_orphaned_messages()
                except Exception as cleanup_error:  # pragma: no cover - defensive
                    self._logger.error(
                        "Failed to cleanup orphaned messages for session %s: %s",
                        self.session_id,
                        cleanup_error,
                    )

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
        branch = branch_id or self._current_branch_id
        await self._ensure_tables()

        async with self._session_factory() as sess:
            if limit is None:
                stmt = (
                    select(self._messages.c.message_data)
                    .join(
                        self._message_structure,
                        and_(
                            self._messages.c.id == self._message_structure.c.message_id,
                            self._message_structure.c.branch_id == branch,
                        ),
                    )
                    .where(self._messages.c.session_id == self.session_id)
                    .order_by(self._message_structure.c.sequence_number.asc())
                )
            else:
                stmt = (
                    select(self._messages.c.message_data)
                    .join(
                        self._message_structure,
                        and_(
                            self._messages.c.id == self._message_structure.c.message_id,
                            self._message_structure.c.branch_id == branch,
                        ),
                    )
                    .where(self._messages.c.session_id == self.session_id)
                    .order_by(self._message_structure.c.sequence_number.desc())
                    .limit(limit)
                )

            result = await sess.execute(stmt)
            rows: list[str] = [row[0] for row in result.all()]

        if limit is not None:
            rows.reverse()

        items: list[TResponseInputItem] = []
        for raw in rows:
            try:
                items.append(await self._deserialize_item(raw))
            except json.JSONDecodeError:
                # Skip corrupted rows
                continue
        return items

    async def store_run_usage(self, result: RunResult) -> None:
        """Store usage data for the current conversation turn.

        This is designed to be called after `Runner.run()` completes.
        Session-level usage can be aggregated from turn data when needed.

        Args:
            result: The result from the run
        """
        usage = result.context_wrapper.usage
        if usage is None:
            return

        try:
            current_turn = await self._get_current_turn_number()
            if current_turn > 0:
                await self._update_turn_usage_internal(current_turn, usage)
        except Exception as exc:  # pragma: no cover - defensive logging
            self._logger.error("Failed to store usage for session %s: %s", self.session_id, exc)

    async def _get_next_turn_number(self, branch_id: str) -> int:
        """Get the next turn number for a specific branch.

        Args:
            branch_id: The branch ID to get the next turn number for.

        Returns:
            The next available turn number for the specified branch.
        """
        max_turn = await self._get_current_turn_number(branch_id)
        return max_turn + 1

    async def _get_next_branch_turn_number(self, branch_id: str) -> int:
        """Get the next branch turn number for a specific branch.

        Args:
            branch_id: The branch ID to get the next branch turn number for.

        Returns:
            The next available branch turn number for the specified branch.
        """
        await self._ensure_tables()
        async with self._session_factory() as sess:
            stmt = select(
                func.coalesce(func.max(self._message_structure.c.branch_turn_number), 0)
            ).where(
                and_(
                    self._message_structure.c.session_id == self.session_id,
                    self._message_structure.c.branch_id == branch_id,
                )
            )
            value = await sess.scalar(stmt)
        return int(value or 0) + 1

    async def _get_current_turn_number(self, branch_id: str | None = None) -> int:
        """Get the current turn number for the current branch.

        Returns:
            The current turn number for the active branch.
        """
        branch = branch_id or self._current_branch_id
        await self._ensure_tables()
        async with self._session_factory() as sess:
            stmt = select(
                func.coalesce(func.max(self._message_structure.c.user_turn_number), 0)
            ).where(
                and_(
                    self._message_structure.c.session_id == self.session_id,
                    self._message_structure.c.branch_id == branch,
                )
            )
            value = await sess.scalar(stmt)
        return int(value or 0)

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
        if not items:
            return

        await self._ensure_tables()

        async with self._session_factory() as sess:
            async with sess.begin():
                ids_stmt = (
                    select(self._messages.c.id)
                    .where(self._messages.c.session_id == self.session_id)
                    .order_by(self._messages.c.id.desc())
                    .limit(len(items))
                )
                id_rows = await sess.execute(ids_stmt)
                message_ids = [row[0] for row in id_rows.all()]
                message_ids.reverse()

                if len(message_ids) != len(items):
                    self._logger.warning(
                        "Mismatch retrieving message IDs for session %s. Expected %s got %s",
                        self.session_id,
                        len(items),
                        len(message_ids),
                    )
                    return

                seq_stmt = select(
                    func.coalesce(func.max(self._message_structure.c.sequence_number), 0)
                ).where(self._message_structure.c.session_id == self.session_id)
                seq_start = await sess.scalar(seq_stmt)
                seq_start = int(seq_start or 0)

                turn_stmt = select(
                    func.coalesce(func.max(self._message_structure.c.user_turn_number), 0),
                    func.coalesce(func.max(self._message_structure.c.branch_turn_number), 0),
                ).where(
                    and_(
                        self._message_structure.c.session_id == self.session_id,
                        self._message_structure.c.branch_id == self._current_branch_id,
                    )
                )
                turn_row = await sess.execute(turn_stmt)
                turn_values = turn_row.one_or_none()
                current_turn = int(turn_values[0]) if turn_values and turn_values[0] else 0
                current_branch_turn = int(turn_values[1]) if turn_values and turn_values[1] else 0

                structure_payload: list[dict[str, Any]] = []
                user_message_count = 0

                for offset, (item, message_id) in enumerate(zip(items, message_ids)):
                    msg_type = self._classify_message_type(item)
                    tool_name = self._extract_tool_name(item)

                    if self._is_user_message(item):
                        user_message_count += 1

                    turn_value = current_turn + user_message_count
                    branch_turn_value = current_branch_turn + user_message_count

                    structure_payload.append(
                        {
                            "session_id": self.session_id,
                            "message_id": message_id,
                            "branch_id": self._current_branch_id,
                            "message_type": msg_type,
                            "sequence_number": seq_start + offset + 1,
                            "user_turn_number": turn_value,
                            "branch_turn_number": branch_turn_value,
                            "tool_name": tool_name,
                        }
                    )

                if structure_payload:
                    await sess.execute(insert(self._message_structure), structure_payload)

    async def _cleanup_orphaned_messages(self) -> None:
        """Remove messages that exist in agent_messages but not in message_structure.

        This can happen if _add_structure_metadata fails after super().add_items() succeeds.
        Used for maintaining data consistency.
        """
        await self._ensure_tables()
        async with self._session_factory() as sess:
            async with sess.begin():
                join_stmt = (
                    select(self._messages.c.id)
                    .select_from(
                        self._messages.outerjoin(
                            self._message_structure,
                            self._messages.c.id == self._message_structure.c.message_id,
                        )
                    )
                    .where(
                        and_(
                            self._messages.c.session_id == self.session_id,
                            self._message_structure.c.message_id.is_(None),
                        )
                    )
                )
                result = await sess.execute(join_stmt)
                orphan_ids = [row[0] for row in result.all()]

                if orphan_ids:
                    await sess.execute(
                        delete(self._messages).where(self._messages.c.id.in_(orphan_ids))
                    )
                    self._logger.info(
                        "Cleaned up %s orphaned messages for session %s",
                        len(orphan_ids),
                        self.session_id,
                    )

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

            if item_type in {"mcp_call", "mcp_approval_request"} and "server_label" in item:
                server_label = item.get("server_label")
                tool_name = item.get("name")
                if tool_name and server_label:
                    return f"{server_label}.{tool_name}"
                if server_label:
                    return str(server_label)
                if tool_name:
                    return str(tool_name)

            if item_type in {
                "computer_call",
                "file_search_call",
                "web_search_call",
                "code_interpreter_call",
            }:
                return item_type

            if "name" in item and item.get("name") is not None:
                return str(item.get("name"))

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
        self,
        turn_number: int,
        branch_name: str | None = None,
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
        await self._ensure_tables()
        async with self._session_factory() as sess:
            stmt = (
                select(self._messages.c.message_data)
                .join(
                    self._message_structure,
                    self._messages.c.id == self._message_structure.c.message_id,
                )
                .where(
                    and_(
                        self._message_structure.c.session_id == self.session_id,
                        self._message_structure.c.branch_id == self._current_branch_id,
                        self._message_structure.c.branch_turn_number == turn_number,
                        self._message_structure.c.message_type == "user",
                    )
                )
            )
            row = await sess.execute(stmt)
            message_row = row.first()

        if not message_row:
            raise ValueError(
                f"Turn {turn_number} does not contain a user message "
                f"in branch '{self._current_branch_id}'"
            )

        try:
            message_content = json.loads(message_row[0]).get("content", "")
        except Exception:  # pragma: no cover - defensive
            message_content = "Unable to parse content"

        if branch_name is None:
            branch_name = f"branch_from_turn_{turn_number}_{int(time.time())}"

        await self._copy_messages_to_new_branch(branch_name, turn_number)

        old_branch = self._current_branch_id
        self._current_branch_id = branch_name
        self._logger.debug(
            "Created branch '%s' from turn %s ('%s') in '%s'",
            branch_name,
            turn_number,
            message_content[:50] + ("..." if len(message_content) > 50 else ""),
            old_branch,
        )
        return branch_name

    async def create_branch_from_content(
        self,
        search_term: str,
        branch_name: str | None = None,
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
        matches = await self.find_turns_by_content(search_term)
        if not matches:
            raise ValueError(f"No user turns found containing '{search_term}'")

        return await self.create_branch_from_turn(matches[0]["turn"], branch_name)

    async def switch_to_branch(self, branch_id: str) -> None:
        """Switch to a different branch.

        Args:
            branch_id: The branch to switch to.

        Raises:
            ValueError: If the branch doesn't exist.
        """
        await self._ensure_tables()
        async with self._session_factory() as sess:
            stmt = (
                select(func.count())
                .select_from(self._message_structure)
                .where(
                    and_(
                        self._message_structure.c.session_id == self.session_id,
                        self._message_structure.c.branch_id == branch_id,
                    )
                )
            )
            exists = await sess.scalar(stmt)

        if not exists:
            raise ValueError(f"Branch '{branch_id}' does not exist")

        old_branch = self._current_branch_id
        self._current_branch_id = branch_id
        self._logger.info("Switched from branch '%s' to '%s'", old_branch, branch_id)

    async def delete_branch(self, branch_id: str, *, force: bool = False) -> None:
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

        if branch_id == "main":
            raise ValueError("Cannot delete the 'main' branch")

        if branch_id == self._current_branch_id:
            if not force:
                raise ValueError(
                    f"Cannot delete current branch '{branch_id}'. "
                    "Use force=True or switch branches first"
                )
            await self.switch_to_branch("main")

        await self._ensure_tables()
        async with self._lock:
            async with self._session_factory() as sess:
                async with sess.begin():
                    exists_stmt = (
                        select(func.count())
                        .select_from(self._message_structure)
                        .where(
                            and_(
                                self._message_structure.c.session_id == self.session_id,
                                self._message_structure.c.branch_id == branch_id,
                            )
                        )
                    )
                    exists = await sess.scalar(exists_stmt)
                    if not exists:
                        raise ValueError(f"Branch '{branch_id}' does not exist")

                    usage_result = await sess.execute(
                        delete(self._turn_usage).where(
                            and_(
                                self._turn_usage.c.session_id == self.session_id,
                                self._turn_usage.c.branch_id == branch_id,
                            )
                        )
                    )

                    structure_result = await sess.execute(
                        delete(self._message_structure).where(
                            and_(
                                self._message_structure.c.session_id == self.session_id,
                                self._message_structure.c.branch_id == branch_id,
                            )
                        )
                    )

        self._logger.info(
            "Deleted branch '%s': %s message entries, %s usage entries",
            branch_id,
            structure_result.rowcount if "structure_result" in locals() else 0,
            usage_result.rowcount if "usage_result" in locals() else 0,
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
        await self._ensure_tables()
        async with self._session_factory() as sess:
            stmt = (
                select(
                    self._message_structure.c.branch_id,
                    func.count().label("message_count"),
                    func.sum(
                        case(
                            (self._message_structure.c.message_type == "user", 1),
                            else_=0,
                        )
                    ).label("user_turns"),
                    func.min(self._message_structure.c.created_at).label("created_at"),
                )
                .where(self._message_structure.c.session_id == self.session_id)
                .group_by(self._message_structure.c.branch_id)
                .order_by(func.min(self._message_structure.c.created_at))
            )
            result = await sess.execute(stmt)
            rows = result.all()

        branches: list[dict[str, Any]] = []
        for branch_id, message_count, user_turns, created_at in rows:
            branches.append(
                {
                    "branch_id": branch_id,
                    "message_count": int(message_count or 0),
                    "user_turns": int(user_turns or 0),
                    "is_current": branch_id == self._current_branch_id,
                    "created_at": created_at,
                }
            )
        return branches

    async def _copy_messages_to_new_branch(
        self,
        new_branch_id: str,
        from_turn_number: int,
    ) -> None:
        """Copy messages before the branch point to the new branch.

        Args:
            new_branch_id: The ID of the new branch to copy messages to.
            from_turn_number: The turn number to copy messages up to (exclusive).
        """
        await self._ensure_tables()
        async with self._lock:
            async with self._session_factory() as sess:
                async with sess.begin():
                    select_stmt = (
                        select(
                            self._message_structure.c.message_id,
                            self._message_structure.c.message_type,
                            self._message_structure.c.sequence_number,
                            self._message_structure.c.user_turn_number,
                            self._message_structure.c.branch_turn_number,
                            self._message_structure.c.tool_name,
                        )
                        .where(
                            and_(
                                self._message_structure.c.session_id == self.session_id,
                                self._message_structure.c.branch_id == self._current_branch_id,
                                self._message_structure.c.branch_turn_number < from_turn_number,
                            )
                        )
                        .order_by(self._message_structure.c.sequence_number)
                    )
                    rows = await sess.execute(select_stmt)
                    messages_to_copy = rows.all()

                    if not messages_to_copy:
                        return

                    seq_stmt = select(
                        func.coalesce(func.max(self._message_structure.c.sequence_number), 0)
                    ).where(self._message_structure.c.session_id == self.session_id)
                    seq_start = await sess.scalar(seq_stmt)
                    seq_start = int(seq_start or 0)

                    payload: list[dict[str, Any]] = []
                    for idx, (
                        message_id,
                        message_type,
                        _,
                        user_turn_number,
                        branch_turn_number,
                        tool_name,
                    ) in enumerate(messages_to_copy):
                        payload.append(
                            {
                                "session_id": self.session_id,
                                "message_id": message_id,
                                "branch_id": new_branch_id,
                                "message_type": message_type,
                                "sequence_number": seq_start + idx + 1,
                                "user_turn_number": user_turn_number,
                                "branch_turn_number": branch_turn_number,
                                "tool_name": tool_name,
                            }
                        )

                    await sess.execute(insert(self._message_structure), payload)

    async def get_conversation_turns(
        self,
        branch_id: str | None = None,
    ) -> list[dict[str, Any]]:
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
        branch = branch_id or self._current_branch_id
        await self._ensure_tables()
        async with self._session_factory() as sess:
            stmt = (
                select(
                    self._message_structure.c.branch_turn_number,
                    self._messages.c.message_data,
                    self._message_structure.c.created_at,
                )
                .join(
                    self._messages,
                    self._messages.c.id == self._message_structure.c.message_id,
                )
                .where(
                    and_(
                        self._message_structure.c.session_id == self.session_id,
                        self._message_structure.c.branch_id == branch,
                        self._message_structure.c.message_type == "user",
                    )
                )
                .order_by(self._message_structure.c.branch_turn_number)
            )
            result = await sess.execute(stmt)
            rows = result.all()

        turns: list[dict[str, Any]] = []
        for turn_number, message_data, created_at in rows:
            try:
                content = json.loads(message_data).get("content", "")
            except (json.JSONDecodeError, AttributeError):
                continue
            turns.append(
                {
                    "turn": int(turn_number),
                    "content": content[:100] + ("..." if len(content) > 100 else ""),
                    "full_content": content,
                    "timestamp": created_at,
                    "can_branch": True,
                }
            )
        return turns

    async def find_turns_by_content(
        self,
        search_term: str,
        branch_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Find user turns containing specific content.

        Args:
            search_term: Text to search for in user messages.
            branch_id: Branch to search in (current branch if None).

        Returns:
            List of matching turns with same format as get_conversation_turns().
        """
        branch = branch_id or self._current_branch_id
        pattern = f"%{search_term}%"
        await self._ensure_tables()

        async with self._session_factory() as sess:
            stmt = (
                select(
                    self._message_structure.c.branch_turn_number,
                    self._messages.c.message_data,
                    self._message_structure.c.created_at,
                )
                .join(
                    self._messages,
                    self._messages.c.id == self._message_structure.c.message_id,
                )
                .where(
                    and_(
                        self._message_structure.c.session_id == self.session_id,
                        self._message_structure.c.branch_id == branch,
                        self._message_structure.c.message_type == "user",
                        self._messages.c.message_data.like(pattern),
                    )
                )
                .order_by(self._message_structure.c.branch_turn_number)
            )
            result = await sess.execute(stmt)
            rows = result.all()

        matches: list[dict[str, Any]] = []
        for turn_number, message_data, created_at in rows:
            try:
                content = json.loads(message_data).get("content", "")
            except (json.JSONDecodeError, AttributeError):
                continue
            matches.append(
                {
                    "turn": int(turn_number),
                    "content": content,
                    "full_content": content,
                    "timestamp": created_at,
                    "can_branch": True,
                }
            )
        return matches

    async def get_conversation_by_turns(
        self,
        branch_id: str | None = None,
    ) -> dict[int, list[dict[str, str | None]]]:
        """Get conversation grouped by user turns for specified branch.

        Args:
            branch_id: Branch to get conversation from (current branch if None).

        Returns:
            Dictionary mapping turn numbers to lists of message metadata.
        """
        branch = branch_id or self._current_branch_id
        await self._ensure_tables()
        async with self._session_factory() as sess:
            stmt = (
                select(
                    self._message_structure.c.user_turn_number,
                    self._message_structure.c.message_type,
                    self._message_structure.c.tool_name,
                )
                .where(
                    and_(
                        self._message_structure.c.session_id == self.session_id,
                        self._message_structure.c.branch_id == branch,
                    )
                )
                .order_by(self._message_structure.c.sequence_number)
            )
            result = await sess.execute(stmt)
            rows = result.all()

        turns: dict[int, list[dict[str, str | None]]] = {}
        for turn_number, message_type, tool_name in rows:
            key = int(turn_number or 0)
            turns.setdefault(key, []).append({"type": message_type, "tool_name": tool_name})
        return turns

    async def get_tool_usage(
        self,
        branch_id: str | None = None,
    ) -> list[tuple[str | None, int, int]]:
        """Get all tool usage by turn for specified branch.

        Args:
            branch_id: Branch to get tool usage from (current branch if None).

        Returns:
            List of tuples containing (tool_name, usage_count, turn_number).
        """
        branch = branch_id or self._current_branch_id
        tool_types = {
            "tool_call",
            "function_call",
            "computer_call",
            "file_search_call",
            "web_search_call",
            "code_interpreter_call",
            "custom_tool_call",
            "mcp_call",
            "mcp_approval_request",
        }

        await self._ensure_tables()
        async with self._session_factory() as sess:
            stmt = (
                select(
                    self._message_structure.c.tool_name,
                    func.count(),
                    self._message_structure.c.user_turn_number,
                )
                .where(
                    and_(
                        self._message_structure.c.session_id == self.session_id,
                        self._message_structure.c.branch_id == branch,
                        self._message_structure.c.message_type.in_(tool_types),
                    )
                )
                .group_by(
                    self._message_structure.c.tool_name,
                    self._message_structure.c.user_turn_number,
                )
                .order_by(self._message_structure.c.user_turn_number)
            )
            result = await sess.execute(stmt)
            rows = result.all()

        return [
            (
                tool_name,
                int(count or 0),
                int(user_turn_number or 0),
            )
            for tool_name, count, user_turn_number in rows
        ]

    async def get_session_usage(
        self,
        branch_id: str | None = None,
    ) -> dict[str, int] | None:
        """Get cumulative usage for session or specific branch.

        Args:
            branch_id: If provided, only get usage for that branch. If None, get all branches.

        Returns:
            Dictionary with usage statistics or None if no usage data found.
        """
        await self._ensure_tables()
        async with self._session_factory() as sess:
            if branch_id:
                stmt = select(
                    func.sum(self._turn_usage.c.requests),
                    func.sum(self._turn_usage.c.input_tokens),
                    func.sum(self._turn_usage.c.output_tokens),
                    func.sum(self._turn_usage.c.total_tokens),
                    func.count(),
                ).where(
                    and_(
                        self._turn_usage.c.session_id == self.session_id,
                        self._turn_usage.c.branch_id == branch_id,
                    )
                )
            else:
                stmt = select(
                    func.sum(self._turn_usage.c.requests),
                    func.sum(self._turn_usage.c.input_tokens),
                    func.sum(self._turn_usage.c.output_tokens),
                    func.sum(self._turn_usage.c.total_tokens),
                    func.count(),
                ).where(self._turn_usage.c.session_id == self.session_id)

            result = await sess.execute(stmt)
            row = result.first()

        if not row or row[0] is None:
            return None

        requests, input_tokens, output_tokens, total_tokens, turns = row
        return {
            "requests": int(requests or 0),
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "total_tokens": int(total_tokens or 0),
            "total_turns": int(turns or 0),
        }

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
        branch = branch_id or self._current_branch_id
        await self._ensure_tables()
        async with self._session_factory() as sess:
            if user_turn_number is not None:
                stmt = select(
                    self._turn_usage.c.requests,
                    self._turn_usage.c.input_tokens,
                    self._turn_usage.c.output_tokens,
                    self._turn_usage.c.total_tokens,
                    self._turn_usage.c.input_tokens_details,
                    self._turn_usage.c.output_tokens_details,
                ).where(
                    and_(
                        self._turn_usage.c.session_id == self.session_id,
                        self._turn_usage.c.branch_id == branch,
                        self._turn_usage.c.user_turn_number == user_turn_number,
                    )
                )
                result = await sess.execute(stmt)
                row = result.first()
                if not row:
                    return {}
                return {
                    "requests": int(row[0] or 0),
                    "input_tokens": int(row[1] or 0),
                    "output_tokens": int(row[2] or 0),
                    "total_tokens": int(row[3] or 0),
                    "input_tokens_details": self._loads_optional(row[4]),
                    "output_tokens_details": self._loads_optional(row[5]),
                }

            stmt = (
                select(
                    self._turn_usage.c.user_turn_number,
                    self._turn_usage.c.requests,
                    self._turn_usage.c.input_tokens,
                    self._turn_usage.c.output_tokens,
                    self._turn_usage.c.total_tokens,
                    self._turn_usage.c.input_tokens_details,
                    self._turn_usage.c.output_tokens_details,
                )
                .where(
                    and_(
                        self._turn_usage.c.session_id == self.session_id,
                        self._turn_usage.c.branch_id == branch,
                    )
                )
                .order_by(self._turn_usage.c.user_turn_number)
            )
            result = await sess.execute(stmt)
            rows = result.all()

        usage_rows: list[dict[str, Any]] = []
        for (
            turn_number,
            requests,
            input_tokens,
            output_tokens,
            total_tokens,
            input_details,
            output_details,
        ) in rows:
            usage_rows.append(
                {
                    "user_turn_number": int(turn_number or 0),
                    "requests": int(requests or 0),
                    "input_tokens": int(input_tokens or 0),
                    "output_tokens": int(output_tokens or 0),
                    "total_tokens": int(total_tokens or 0),
                    "input_tokens_details": self._loads_optional(input_details),
                    "output_tokens_details": self._loads_optional(output_details),
                }
            )
        return usage_rows

    async def _update_turn_usage_internal(
        self,
        user_turn_number: int,
        usage_data: Usage,
    ) -> None:
        """Internal method to update usage for a specific turn with full JSON details.

        Args:
            user_turn_number: The turn number to update usage for.
            usage_data: The usage data to store.
        """
        await self._ensure_tables()
        input_details = self._dumps_token_details(getattr(usage_data, "input_tokens_details", None))
        output_details = self._dumps_token_details(
            getattr(usage_data, "output_tokens_details", None)
        )

        payload = {
            "requests": usage_data.requests or 0,
            "input_tokens": usage_data.input_tokens or 0,
            "output_tokens": usage_data.output_tokens or 0,
            "total_tokens": usage_data.total_tokens or 0,
            "input_tokens_details": input_details,
            "output_tokens_details": output_details,
        }

        async with self._session_factory() as sess:
            async with sess.begin():
                update_stmt = (
                    update(self._turn_usage)
                    .where(
                        and_(
                            self._turn_usage.c.session_id == self.session_id,
                            self._turn_usage.c.branch_id == self._current_branch_id,
                            self._turn_usage.c.user_turn_number == user_turn_number,
                        )
                    )
                    .values(**payload)
                )
                result = await sess.execute(update_stmt)

                if result.rowcount == 0:
                    insert_stmt = insert(self._turn_usage).values(
                        session_id=self.session_id,
                        branch_id=self._current_branch_id,
                        user_turn_number=user_turn_number,
                        **payload,
                    )
                    await sess.execute(insert_stmt)

    def _dumps_token_details(self, details: Any) -> str | None:
        """Serialize token detail objects to JSON."""
        if not details:
            return None

        for attr in ("model_dump", "dict"):
            if hasattr(details, attr):
                try:
                    return json.dumps(getattr(details, attr)())
                except (TypeError, ValueError):
                    continue

        try:
            return json.dumps(details.__dict__)
        except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
            self._logger.warning("Failed to serialize token details: %s", exc)
            return None

    def _loads_optional(self, payload: str | None) -> Any:
        """Deserialize optional JSON payloads."""
        if not payload:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None
