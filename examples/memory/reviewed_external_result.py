"""Resume an approved SDK call against an idempotent external service.

Run ``python -m examples.memory.reviewed_external_result`` for an offline demo.
SQLite models the external service's own atomic authorization and operation-ID
boundary. In production, that service must supply those guarantees itself.
The checkpoint database and service database belong to one authorized tenant.
No serialized SDK context carries service credentials or executor authority.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from agents import Agent, ResumableSession, RunConfig, Runner
from agents.decorators import tool
from agents.testing import ScriptedModel, function_call
from agents.tool_context import ToolContext
from examples.memory.resumable_sqlite_session import SQLiteStore


@dataclass(frozen=True)
class Review:
    """An operator's decision for exactly one bare SDK tool call."""

    session_id: str
    call_id: str
    arguments: str
    tool_name: str = "publish_note"


def canonical(arguments: str) -> str:
    return json.dumps(json.loads(arguments), sort_keys=True, separators=(",", ":"))


class NoteService:
    """A local stand-in for a service with durable idempotency and revocation."""

    def __init__(self, path: Path) -> None:
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS policy (session TEXT PRIMARY KEY, allowed INTEGER);"
                "CREATE TABLE IF NOT EXISTS notes (session TEXT PRIMARY KEY, text TEXT);"
                "CREATE TABLE IF NOT EXISTS operations "
                "(id TEXT PRIMARY KEY, arguments TEXT NOT NULL, result TEXT NOT NULL);"
            )

    def allow(self, session_id: str, allowed: bool) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT OR REPLACE INTO policy VALUES (?, ?)", (session_id, int(allowed)))

    def publish(self, review: Review, arguments: str) -> str:
        """Check authorization and expected state in the transaction doing the write."""
        if canonical(arguments) != review.arguments or review.tool_name != "publish_note":
            raise PermissionError("The arguments require fresh review.")
        payload = json.loads(arguments)
        text, expected = payload["text"], payload["expected"]
        operation_id = self.operation_id(review)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            permission = db.execute(
                "SELECT allowed FROM policy WHERE session = ?", (review.session_id,)
            ).fetchone()
            if permission != (1,):
                raise PermissionError("Authorization has been revoked.")
            prior = db.execute(
                "SELECT arguments, result FROM operations WHERE id = ?", (operation_id,)
            ).fetchone()
            if prior:
                if prior[0] != review.arguments:
                    raise PermissionError("The operation ID belongs to different arguments.")
                return str(prior[1])
            row = db.execute(
                "SELECT text FROM notes WHERE session = ?", (review.session_id,)
            ).fetchone()
            current = "" if row is None else row[0]
            if current != expected:
                raise PermissionError("The note changed; obtain fresh review.")
            result = f"Published: {text}"
            db.execute("INSERT OR REPLACE INTO notes VALUES (?, ?)", (review.session_id, text))
            db.execute(
                "INSERT INTO operations VALUES (?, ?, ?)",
                (operation_id, review.arguments, result),
            )
            return result

    @staticmethod
    def operation_id(review: Review) -> str:
        # Tuple encoding preserves session, tool identity, and provider call identity.
        return json.dumps([review.session_id, review.tool_name, review.call_id])

    def reconcile(self, review: Review) -> str | None:
        """Read the effect without dispatching the operation again."""
        with closing(sqlite3.connect(self.path)) as db:
            # Read authorization and the result from the same database snapshot.
            db.execute("BEGIN")
            permission = db.execute(
                "SELECT allowed FROM policy WHERE session = ?", (review.session_id,)
            ).fetchone()
            if permission != (1,):
                raise PermissionError("Authorization has been revoked.")
            row = db.execute(
                "SELECT arguments, result FROM operations WHERE id = ?",
                (self.operation_id(review),),
            ).fetchone()
            if row is None:
                return None
            if row[0] != review.arguments:
                raise PermissionError("The recorded operation does not match the review.")
            return str(row[1])


def writer(service: NoteService, review: Review | None, *, lose_ack: bool = False) -> Agent:
    @tool(needs_approval=True, failure_error_function=None)
    async def publish_note(ctx: ToolContext[None], text: str, expected: str) -> str:
        """Publish a reviewed note only if its prior content still matches."""
        if review is None or ctx.tool_call_id != review.call_id:
            raise PermissionError("The invocation has not been reviewed.")
        result = await asyncio.to_thread(service.publish, review, ctx.tool_arguments)
        if lose_ack:
            # Model worker loss after the external service commits, before SDK acknowledgment.
            raise asyncio.CancelledError()
        return result

    return Agent(
        name="Note writer",
        tools=[publish_note],
        tool_use_behavior={"stop_at_tool_names": [publish_note.name]},
        model=ScriptedModel([]),
    )


async def request_review(folder: Path) -> Review:
    store = SQLiteStore(folder / "sessions.db")
    session = await ResumableSession.create(store)
    service = NoteService(folder / "service.db")
    service.allow(session.session_id, True)
    agent = writer(service, None)
    agent.model = ScriptedModel(
        [[function_call("publish_note", {"text": "Hello", "expected": ""}, call_id="note-1")]]
    )
    result = await Runner.run(
        agent, "Publish Hello.", session=session, run_config=RunConfig(tracing_disabled=True)
    )
    (pending,) = result.interruptions
    if not isinstance(pending.call_id, str) or not isinstance(pending.arguments, str):
        raise ValueError("The pending SDK call needs a stable call ID and arguments.")
    return Review(session.session_id, pending.call_id, canonical(pending.arguments))


async def resume_review(
    folder: Path, review: Review, *, lose_ack: bool = False, barrier=None
) -> str:
    session = await ResumableSession.resume(SQLiteStore(folder / "sessions.db"), review.session_id)
    agent = writer(NoteService(folder / "service.db"), review, lose_ack=lose_ack)
    state = await session.load_state(agent)
    (pending,) = state.get_interruptions()
    if (
        not isinstance(pending.arguments, str)
        or pending.call_id != review.call_id
        or pending.tool_name != review.tool_name
        or canonical(pending.arguments) != review.arguments
    ):
        raise PermissionError("The pending call requires fresh review.")
    state.approve(pending)
    if barrier is not None:
        # The demo's process barrier makes both workers race from the same checkpoint.
        await asyncio.to_thread(barrier.wait, 10)
    result = await Runner.run(agent, state, run_config=RunConfig(tracing_disabled=True))
    return str(result.final_output)


async def main() -> None:
    with TemporaryDirectory() as directory:
        folder = Path(directory)
        review = await request_review(folder)
        print(await resume_review(folder, review))
        print(NoteService(folder / "service.db").reconcile(review))


if __name__ == "__main__":
    asyncio.run(main())
