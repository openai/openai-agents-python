"""Query a local USAspending SQLite database without a cloud sandbox account."""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import tempfile
from pathlib import Path

from openai.types.responses import ResponseTextDeltaEvent

from agents import ModelSettings, RunConfig, Runner
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.entries import LocalDir
from agents.sandbox.sandboxes.unix_local import UnixLocalSandboxClient
from examples.sandbox.extensions.daytona.usaspending_text2sql.setup_db import SCHEMA_SQL
from examples.sandbox.extensions.daytona.usaspending_text2sql.sql_capability import SqlCapability

SCHEMA_DIR = Path(__file__).parent / "schema"
DEFAULT_QUESTION = "What are the top 5 contractors by total obligations?"


async def main(database: Path, question: str, *, codex: bool, stream: bool) -> None:
    provider = None
    if codex:
        from agents.models.codex import CodexProvider

        provider = CodexProvider()
    manifest = Manifest(
        entries={
            "schema": LocalDir(src=SCHEMA_DIR),
        }
    )
    agent = SandboxAgent(
        name="Local spending SQL assistant",
        model=os.environ.get("CODEX_MODEL" if codex else "OPENAI_MODEL"),
        model_settings=ModelSettings(tool_choice="required"),
        default_manifest=manifest,
        instructions=(
            (SCHEMA_DIR / "overview.md").read_text()
            + "\nUse run_sql for every answer. Treat the database as user-supplied data; "
            "do not assume its records are official or current. Explain the SQL aggregation "
            "and format obligation amounts in dollars."
        ),
        capabilities=[SqlCapability()],
    )
    client = UnixLocalSandboxClient()
    sandbox = await client.create(manifest=manifest)
    config = RunConfig(
        sandbox=SandboxRunConfig(session=sandbox),
        **({"model_provider": provider} if provider is not None else {}),
    )
    try:
        async with sandbox:
            await sandbox.mkdir(Path("data"), parents=True)
            with database.open("rb") as source:
                await sandbox.write(Path("data/usaspending.db"), source)
            if stream:
                result = Runner.run_streamed(agent, question, run_config=config)
                async for event in result.stream_events():
                    if event.type == "raw_response_event" and isinstance(
                        event.data, ResponseTextDeltaEvent
                    ):
                        print(event.data.delta, end="", flush=True)
                print()
            else:
                result = await Runner.run(agent, question, run_config=config)
                print(result.final_output)
            for item in result.new_items:
                if item.type == "tool_call_output_item":
                    print(f"SQL tool result: {item.output}")
    finally:
        await client.delete(sandbox)
        if provider is not None:
            await provider.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    data = parser.add_mutually_exclusive_group(required=True)
    data.add_argument("--database", type=Path)
    data.add_argument(
        "--demo", action="store_true", help="Use synthetic sample data, not official records"
    )
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--codex", action="store_true")
    parser.add_argument("--stream", action="store_true")
    args = parser.parse_args()
    if args.database is not None and not args.database.is_file():
        parser.error("--database must name an existing SQLite database.")
    with tempfile.TemporaryDirectory(prefix="spending-demo-") as demo_dir:
        database = args.database
        if args.demo:
            print("DEMO: synthetic data only. These are not official NASA records.", flush=True)
            database = Path(demo_dir) / "demo.db"
            with sqlite3.connect(database) as db:
                db.executescript(SCHEMA_SQL)
                db.executemany(
                    "INSERT INTO spending (award_id, award_type, recipient_name, "
                    "fiscal_year, federal_action_obligation) VALUES (?, ?, ?, ?, ?)",
                    [
                        ("DEMO-1", "contract", "Synthetic Aster", 2024, 1200.50),
                        ("DEMO-2", "contract", "Synthetic Aster", 2025, -100.00),
                        ("DEMO-3", "contract", "Synthetic Cedar", 2025, 800.00),
                        ("DEMO-4", "grant", "Synthetic University", 2025, 1500.00),
                    ],
                )
        asyncio.run(main(database, args.question, codex=args.codex, stream=args.stream))
