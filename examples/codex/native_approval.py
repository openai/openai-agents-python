"""Approve or reject one native Codex command after recreating its worker.

Development only; unavailable in alpha.1. Build and install a native wheel from
the same source revision, then run with saved Codex login:
    python examples/codex/native_approval.py

This live example keeps its SQLite checkpoint and command fixture in a temporary
directory. Only an explicit approval runs the saved command, which writes one
fixture file. Both workers use the same workspace, configuration, and store.
The checkpoint is private application state, never client-supplied RunState.
"""

import argparse
import asyncio
import json
import os
import shlex
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from agents import Agent, ResumableSession, RunConfig, Runner
from agents.models.codex import CodexProvider


class SQLiteStore:
    """Save this example's private checkpoints with atomic compare-and-swap."""

    def __init__(self, path: Path) -> None:
        self.path = path
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS checkpoints (id TEXT PRIMARY KEY, value TEXT)")

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
                        "INSERT INTO checkpoints (id, value) VALUES (?, ?) ON CONFLICT(id) DO NOTHING",
                        (session_id, value),
                    )
                else:
                    result = db.execute(
                        "UPDATE checkpoints SET value = ? WHERE id = ? AND value = ?",
                        (value, session_id, expected),
                    )
            return result.rowcount == 1

        return await asyncio.to_thread(write)


def provider_for(workspace: Path) -> CodexProvider:
    return CodexProvider(
        cwd=workspace,
        config_overrides=(
            'sandbox_mode="workspace-write"',
            'approval_policy="on-request"',
            'approvals_reviewer="user"',
        ),
    )


def worker() -> Agent:
    return Agent(
        name="Approval worker",
        instructions=(
            "Make only the one native exec_command call requested by the user. "
            "Do not change the command or modify any other files. "
            "After the command result or a rejection, give a brief final answer. "
            "Never retry a rejected command or use another tool."
        ),
    )


async def pause(workspace: Path, database: Path, command: str, model: str | None) -> str:
    session = await ResumableSession.create(SQLiteStore(database))
    provider = provider_for(workspace)
    try:
        result = await Runner.run(
            worker(),
            "Call exec_command exactly once with these arguments, explicitly requesting approval:\n"
            + json.dumps(
                {"cmd": command, "login": False, "sandbox_permissions": "require_escalated"}
            ),
            session=session,
            run_config=RunConfig(model=model, model_provider=provider, tracing_disabled=True),
            max_turns=3,
        )
        if len(result.interruptions) != 1:
            raise RuntimeError("Expected one pending native command; no approval will be given.")
        return session.session_id
    finally:
        await provider.aclose()


async def resume(
    workspace: Path, database: Path, session_id: str, command: str, model: str | None
) -> bool:
    session = await ResumableSession.resume(SQLiteStore(database), session_id)
    agent = worker()
    state = await session.load_state(agent)
    (pending,) = state.get_interruptions()
    arguments = json.loads(pending.arguments or "{}")
    if (
        pending.tool_name != "exec_command"
        or arguments.get("cmd") != command
        or arguments.get("login") is not False
        or arguments.get("sandbox_permissions") != "require_escalated"
    ):
        raise RuntimeError("The saved invocation differs from the requested fixture command.")
    print("Pending native command, restored by a new worker:")
    print(json.dumps({"tool": pending.tool_name, "arguments": arguments}, indent=2))
    try:
        decision = input("Type approve to run this command, or reject to decline: ").strip().lower()
    except EOFError:
        decision = "reject"
    approved = decision == "approve"
    if approved:
        state.approve(pending)
    else:
        state.reject(pending)
    provider = provider_for(workspace)
    try:
        result = await Runner.run(
            agent,
            state,
            run_config=RunConfig(model=model, model_provider=provider, tracing_disabled=True),
            max_turns=3,
        )
        if result.interruptions:
            raise RuntimeError("The worker requested another approval; the example is stopping.")
        print(result.final_output)
    finally:
        await provider.aclose()
    return approved


async def main(model: str | None = None) -> None:
    with TemporaryDirectory(prefix="codex-approval-") as directory:
        root = Path(directory).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        target = workspace / "approved.txt"
        script = workspace / "write_fixture.py"
        source = (
            "import time\nfrom pathlib import Path\n"
            # Give the native command's exit watcher time to observe this short process.
            "time.sleep(1)\n"
            f"Path({str(target)!r}).write_text('approved\\n', encoding='utf-8')\n"
        )
        script.write_text(source, encoding="utf-8")
        argv = [sys.executable, str(script)]
        command = "& " + subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
        database = root / "checkpoints.db"
        session_id = await pause(workspace, database, command, model)
        if target.exists() or script.read_text(encoding="utf-8") != source:
            raise RuntimeError("The fixture changed before approval; the example is stopping.")
        approved = await resume(workspace, database, session_id, command, model)
        if approved:
            if not target.is_file() or target.read_text(encoding="utf-8") != "approved\n":
                raise RuntimeError("The approved command did not write the expected fixture.")
            print("Approved command wrote the fixture after recovery.")
        else:
            if target.exists():
                raise RuntimeError("A rejected command changed the fixture.")
            print("Rejected command left the fixture absent.")
    print("Temporary workspace and checkpoint removed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", help="Override the Codex model from your configuration")
    args = parser.parse_args()
    asyncio.run(main(args.model))
