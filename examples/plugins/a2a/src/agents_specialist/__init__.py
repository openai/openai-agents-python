"""Delegate one application-selected question to one trusted A2A specialist."""

import asyncio
import hashlib
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from a2a.types import Message, Task, TaskState

from agents.decorators import tool
from agents.plugins import Plugin, PluginHooks, RequireApproval

__all__ = ["Client", "DurableCall", "Receipt", "specialist"]


class Client(Protocol):
    """The subset of the SDK's A2AClient needed for one owner and one endpoint."""

    @property
    def card_json(self) -> bytes | None: ...

    @property
    def rpc_token_provider(self) -> Callable[[], str] | None: ...

    async def send_message(self, text: str, *, message_id: str) -> Message | Task: ...

    async def get_task(self, *, task_id: str, context_id: str) -> Task: ...


@dataclass(frozen=True)
class Receipt:
    """The receiver's immutable mapping from one accepted message to its task."""

    owner: str
    endpoint: str
    message_id: str
    digest: str
    task_id: str
    context_id: str


@dataclass(frozen=True, kw_only=True)
class DurableCall:
    """One operation in a private SQLite file. The lookup must check current access."""

    owner: str
    operation_id: str
    db_path: Path
    lookup_receipt: Callable[[str, str, str], Awaitable[Receipt | None]]

    def __post_init__(self) -> None:
        if not self.owner or not self.operation_id or not self.db_path.is_absolute():
            raise ValueError("Supply an owner, operation ID and absolute database path.")

    def _message_id(self, endpoint: str, digest: str, send: bool) -> str | None:
        key = (self.operation_id, self.owner, endpoint, digest)
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS specialist_calls (
                    operation_id TEXT PRIMARY KEY, owner TEXT, endpoint TEXT,
                    digest TEXT, message_id TEXT UNIQUE
                )"""
            )
            if send:
                # Only the worker that commits a new reservation may send.
                message_id = uuid4().hex
                created = db.execute(
                    "INSERT INTO specialist_calls VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(operation_id) DO NOTHING",
                    (*key, message_id),
                ).rowcount
                return message_id if created else None
            row = db.execute(
                "SELECT message_id FROM specialist_calls "
                "WHERE operation_id=? AND owner=? AND endpoint=? AND digest=?",
                key,
            ).fetchone()
            return row[0] if row else None

    async def _task(
        self, client: Client, endpoint: str, question: str, *, send: bool
    ) -> Task | None:
        digest = hashlib.sha256(question.encode()).hexdigest()
        message_id = await asyncio.to_thread(self._message_id, endpoint, digest, send)
        if not message_id:
            return None
        task = await client.send_message(question, message_id=message_id) if send else None
        receipt = await self.lookup_receipt(self.owner, endpoint, message_id)
        if (
            receipt is None
            or (receipt.owner, receipt.endpoint, receipt.message_id, receipt.digest)
            != (self.owner, endpoint, message_id, digest)
            or not receipt.task_id
            or not receipt.context_id
        ):
            return None
        if not send:
            task = await client.get_task(task_id=receipt.task_id, context_id=receipt.context_id)
        if isinstance(task, Task) and (task.id, task.context_id) == (
            receipt.task_id,
            receipt.context_id,
        ):
            return task
        return None


def specialist(
    client: Client,
    *,
    question: str,
    artifact_id: str | None,
    verdicts: tuple[str, ...],
    durable: DurableCall | None = None,
) -> Plugin[None]:
    """Create once per owner and question. The token provider must recheck the grant.

    Pass artifact_id=None to accept any nonempty ID on the task's single artifact.
    """
    if client.rpc_token_provider is None or client.card_json is None:
        raise ValueError("Supply a grant-checking token provider and a local Agent Card.")
    endpoint: str = getattr(client, "rpc_url", "") if durable is not None else ""
    if durable is not None and not endpoint:
        raise ValueError("Durable calls require a client with a fixed rpc_url.")
    allowed = frozenset(verdicts)
    if artifact_id is not None and not artifact_id:
        raise ValueError("Supply a nonempty artifact ID or None.")
    if not question or not allowed or allowed & {"", "pending", "unknown"}:
        raise ValueError("Supply a question and nonempty, non-reserved verdicts.")

    attempted = False
    receipt: tuple[str, str] | None = None

    def read(task: Task | None) -> str:
        if task is None:
            return "unknown"
        if task.status.state in (TaskState.TASK_STATE_SUBMITTED, TaskState.TASK_STATE_WORKING):
            return "pending"
        if task.status.state == TaskState.TASK_STATE_COMPLETED and len(task.artifacts) == 1:
            artifact = task.artifacts[0]
            if (
                artifact.artifact_id
                and (artifact_id is None or artifact.artifact_id == artifact_id)
                and len(artifact.parts) == 1
            ):
                verdict = artifact.parts[0].text
                if verdict in allowed:
                    return verdict
        return "unknown"

    @tool
    async def ask_specialist() -> str:
        """Send the application's selected question to the designated specialist once."""
        nonlocal attempted, receipt
        if attempted:
            return "unknown"
        # Reserve before awaiting. A failed or cancelled send might still reach the peer.
        attempted = True
        try:
            if durable is not None:
                return read(await durable._task(client, endpoint, question, send=True))
            reply = await client.send_message(question, message_id=uuid4().hex)
        except Exception:
            # Never put dependency errors or private request details into the model input.
            return "unknown"
        if not isinstance(reply, Task):
            return "unknown"
        result = read(reply)
        if result == "pending":
            receipt = (reply.id, reply.context_id)
        return result

    @tool
    async def refresh_specialist() -> str:
        """Check a previous attempt without sending the question again."""
        try:
            if durable is not None:
                return read(await durable._task(client, endpoint, question, send=False))
            if receipt is None:
                return "unknown"
            task = await client.get_task(task_id=receipt[0], context_id=receipt[1])
        except Exception:
            return "unknown"
        return read(task)

    return Plugin(
        name="specialist",
        tools=[ask_specialist, refresh_specialist],
        hooks=PluginHooks(check_own_tool=lambda _: RequireApproval()),
        instructions=(
            "Ask the specialist once. Report unknown results as unknown. "
            "Refresh a previous attempt only at the user's request."
        ),
    )
