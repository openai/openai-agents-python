"""A manager tool for specialists that return a single yes/no verdict."""

import hashlib
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from itertools import islice
from uuid import uuid4

from a2a.types import a2a_pb2 as a2a

from agents.decorators import tool
from agents.extensions.a2a import A2ASpecialist
from agents.tool_context import ToolContext


class UnresolvedCall(Exception):
    """The manager has no verified result. Do not submit the question again."""


@dataclass(frozen=True)
class Receipt:
    """A receiver-owned record read over a trusted channel, never supplied by the model."""

    owner: str
    endpoint: str
    message_id: str
    digest: str
    task_id: str
    context_id: str
    verdict: str


@dataclass(frozen=True)
class Registry:
    """Current grants and a trusted receipt lookup for one authenticated owner and run."""

    owner: str
    run_id: str
    specialists: Mapping[str, A2ASpecialist]
    db: sqlite3.Connection
    lookup_receipt: Callable[[str, str, str], Receipt | None]

    def __post_init__(self) -> None:
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS calls (
                owner TEXT, run_id TEXT, call_id TEXT, name TEXT, endpoint TEXT,
                digest TEXT, message_id TEXT UNIQUE, PRIMARY KEY (owner, run_id, call_id)
            )"""
        )

    async def ask(self, call_id: str, name: str, question: str) -> str:
        specialist = self.specialists.get(name)
        if specialist is None or not self.owner or not self.run_id or not call_id:
            raise PermissionError("No grant for this invocation")

        key = (self.owner, self.run_id, call_id)
        binding = (name, specialist.rpc_url, hashlib.sha256(question.encode()).hexdigest())
        message_id = uuid4().hex
        # Commit before network I/O. Only the worker that inserted this row may send.
        with self.db:
            created = self.db.execute(
                "INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(owner, run_id, call_id) DO NOTHING",
                (*key, *binding, message_id),
            ).rowcount
        task = await specialist.send(question, message_id=message_id) if created else None
        return await self._result(call_id, expected=binding, task=task)

    async def recover(self, call_id: str) -> str:
        """Read a reserved call for this owner and run without sending a new message."""
        return await self._result(call_id)

    async def _result(
        self,
        call_id: str,
        *,
        expected: tuple[str, str, str] | None = None,
        task: a2a.Task | None = None,
    ) -> str:
        row = self.db.execute(
            "SELECT name, endpoint, digest, message_id FROM calls "
            "WHERE owner=? AND run_id=? AND call_id=?",
            (self.owner, self.run_id, call_id),
        ).fetchone()
        if row is None:
            raise UnresolvedCall("No reservation for this invocation")
        name, endpoint, digest, message_id = row
        specialist = self.specialists.get(name)
        if specialist is None or specialist.rpc_url != endpoint:
            raise PermissionError("No grant for this invocation")
        if expected is not None and (name, endpoint, digest) != expected:
            raise PermissionError("Invocation binding changed")

        receipt = self.lookup_receipt(self.owner, specialist.rpc_url, message_id)
        if receipt is None or (
            receipt.owner,
            receipt.endpoint,
            receipt.message_id,
            receipt.digest,
        ) != (self.owner, specialist.rpc_url, message_id, digest):
            raise UnresolvedCall("No matching receipt")
        if task is None:
            task = await specialist.get(task_id=receipt.task_id, context_id=receipt.context_id)
        if task.id != receipt.task_id or task.context_id != receipt.context_id:
            raise UnresolvedCall("Task does not match the receipt")
        if task.status.state != a2a.TASK_STATE_COMPLETED:
            raise UnresolvedCall("Task has no completed result")
        if len(task.artifacts) == 1 and len(task.artifacts[0].parts) == 1:
            verdict = task.artifacts[0].parts[0].text
            if verdict == receipt.verdict and verdict in ("yes", "no"):
                return receipt.verdict
        raise UnresolvedCall("Unexpected verdict")


@tool
async def find_specialists(ctx: ToolContext[Registry], prefix: str) -> list[str]:
    """List up to ten registered names starting with the given prefix."""
    names = (name for name in ctx.context.specialists if name.startswith(prefix))
    return list(islice(names, 10))


@tool(failure_error_function=None)
async def ask_specialist(ctx: ToolContext[Registry], name: str, question: str) -> str:
    """Ask a registered specialist a yes/no question and return its verdict."""
    return await ctx.context.ask(ctx.tool_call_id, name, question)
