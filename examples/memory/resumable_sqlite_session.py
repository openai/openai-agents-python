"""Recover a pending tool call using a custom SQLite checkpoint store.

Run from the repository root with
    uv run python examples/memory/resumable_sqlite_session.py

This offline example simulates two workers on one host. In a service, use a
private database per tenant and authorize access before loading a session.
"""

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from agents import Agent, ResumableSession, ResumableSessionStore, RunConfig, Runner
from agents.decorators import tool
from agents.items import TResponseOutputItem
from agents.testing import ModelCall, ModelStep, ScriptedModel, assistant_message, function_call


class SQLiteStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        with closing(sqlite3.connect(path)) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS checkpoints "
                "(id TEXT PRIMARY KEY, value TEXT NOT NULL COLLATE BINARY)"
            )

    async def load(self, session_id: str) -> str | None:
        def read() -> str | None:
            with closing(sqlite3.connect(self.path)) as db:
                row = db.execute(
                    "SELECT value FROM checkpoints WHERE id = ?", (session_id,)
                ).fetchone()
                return None if row is None else row[0]

        return await asyncio.to_thread(read)

    async def save(self, session_id: str, value: str, *, expected: str | None) -> bool:
        def write() -> bool:
            with closing(sqlite3.connect(self.path)) as db, db:
                if expected is None:
                    result = db.execute(
                        "INSERT INTO checkpoints (id, value) VALUES (?, ?) "
                        "ON CONFLICT(id) DO NOTHING",
                        (session_id, value),
                    )
                else:
                    # Compare and write in one statement. A stale worker updates zero rows.
                    result = db.execute(
                        "UPDATE checkpoints SET value = ? WHERE id = ? AND value = ?",
                        (value, session_id, expected),
                    )
            return result.rowcount == 1

        return await asyncio.to_thread(write)


@tool(needs_approval=True)
def publish_note(text: str) -> str:
    """Simulate publishing a note."""
    return f"Published {text}"


async def start(store: ResumableSessionStore) -> str:
    session = await ResumableSession.create(store)
    model = ScriptedModel([[function_call("publish_note", {"text": "Hello"}, call_id="c1")]])
    agent = Agent(name="Writer", model=model, tools=[publish_note])
    await Runner.run(
        agent, "Publish my note.", session=session, run_config=RunConfig(tracing_disabled=True)
    )
    return session.session_id


def read_result(call: ModelCall) -> list[TResponseOutputItem]:
    (output,) = [
        item["output"] for item in call.input if item.get("type") == "function_call_output"
    ]
    return [assistant_message(output)]


async def resume(store: ResumableSessionStore, session_id: str) -> None:
    session = await ResumableSession.resume(store, session_id)
    model = ScriptedModel([ModelStep(responder=read_result)])
    agent = Agent(name="Writer", model=model, tools=[publish_note])
    state = await session.load_state(agent)
    (request,) = state.get_interruptions()
    print(request.tool_name, request.arguments)
    state.reject(request)  # Simulate the user declining this specific call.
    result = await Runner.run(agent, state, run_config=RunConfig(tracing_disabled=True))
    print(result.final_output)


if __name__ == "__main__":
    with TemporaryDirectory() as directory:
        path = Path(directory) / "sessions.db"
        session_id = asyncio.run(start(SQLiteStore(path)))
        asyncio.run(resume(SQLiteStore(path), session_id))
