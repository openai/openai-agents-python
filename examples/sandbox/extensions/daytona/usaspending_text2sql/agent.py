"""NASA spending text-to-SQL agent.

Multi-turn conversational agent that translates natural-language questions
about NASA federal spending into SQL queries, executes them against a
USAspending SQLite database, and returns structured results.

Usage:
    uv run python -m examples.sandbox.extensions.daytona.usaspending_text2sql.agent

The database is built automatically inside the sandbox on first run by
executing setup_db.py (requires internet access). Subsequent runs reuse the
existing database.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import textwrap
from pathlib import Path
from typing import Any

from openai.types.responses import ResponseTextDeltaEvent

from agents import Runner
from agents.run import RunConfig
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities.compaction import Compaction
from agents.sandbox.capabilities.memory import Memory
from agents.sandbox.capabilities.shell import Shell
from agents.sandbox.config import MemoryGenerateConfig, MemoryReadConfig
from agents.sandbox.entries import Dir, File, LocalDir, LocalFile
from agents.sandbox.session import (
    EventPayloadPolicy,
    Instrumentation,
    JsonlOutboxSink,
)
from examples.sandbox.extensions.daytona.usaspending_text2sql.sql_capability import (
    SqlCapability,
)

try:
    from agents.extensions.sandbox import (
        DEFAULT_DAYTONA_WORKSPACE_ROOT,
        DaytonaSandboxClient,
        DaytonaSandboxClientOptions,
        DaytonaSandboxSessionState,
    )
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "Daytona sandbox examples require the optional repo extra.\n"
        "Install it with: uv sync --extra daytona"
    ) from exc

EXAMPLE_DIR = Path(__file__).parent
SCHEMA_DIR = EXAMPLE_DIR / "schema"
SETUP_DB_PATH = EXAMPLE_DIR / "setup_db.py"
SESSION_STATE_PATH = EXAMPLE_DIR / ".session_state.json"
AUDIT_LOG_PATH = EXAMPLE_DIR / ".audit_log.jsonl"

# Set at runtime once the exposed port is resolved.
_downloads_base_url: str = ""

DEVELOPER_INSTRUCTIONS = (
    (SCHEMA_DIR / "overview.md").read_text()
    + """

## Instructions

- Always use the `run_sql` tool to query the database. Never attempt to run sqlite3 directly.
- Read schema documentation from schema/tables/ if you need detailed column information.
- Read schema/glossary.md for official USAspending term definitions (e.g., what "obligation" vs "outlay" means).
- Prefer aggregations (GROUP BY, SUM, COUNT, AVG) over returning many raw rows.
- Format monetary values with dollar signs and commas in your final answers (e.g., $1,234,567).
- When the user asks a follow-up question, use conversation context to understand references
  like "break that down by year" or "just the top 5".
- If a query fails, read the error message and try to fix the SQL.
- Explain your query logic briefly so the user can verify correctness.

## Data caveats

- The database contains **obligations** (money legally committed), not outlays (money actually paid).
  When the user asks about "spending", clarify that these are obligation amounts.
- Amounts are tied to the **action_date** (when the obligation was signed), not when the work happens.
  A multi-year contract may appear entirely in the fiscal year it was obligated.
- Some recipients are masked as "MULTIPLE RECIPIENTS" or "REDACTED DUE TO PII" for privacy reasons.
  Mention this if recipient-level analysis looks incomplete.
"""
)

DB_PATH = "data/usaspending.db"

WORKSPACE_ROOT = DEFAULT_DAYTONA_WORKSPACE_ROOT


def build_agent() -> SandboxAgent:
    """Build the agent blueprint."""
    manifest = Manifest(
        root=WORKSPACE_ROOT,
        entries={
            "setup_db.py": LocalFile(src=SETUP_DB_PATH),
            "schema": LocalDir(src=SCHEMA_DIR),
            "data": Dir(ephemeral=True),
            "memory/memory_summary.md": File(content=b""),
            "memory/phase_two_selection.json": File(content=b""),
        },
    )

    return SandboxAgent(
        name="NASA Spending Q&A",
        default_manifest=manifest,
        model="gpt-5.5",
        instructions=(
            "You are a helpful data analyst that answers questions about NASA federal spending "
            "by writing and executing SQL queries.\n\n" + DEVELOPER_INSTRUCTIONS
        ),
        capabilities=[
            SqlCapability(db_path=DB_PATH),
            Shell(),
            Compaction(),
            Memory(
                read=MemoryReadConfig(live_update=False),
                generate=MemoryGenerateConfig(
                    extra_prompt=(
                        "Pay attention to which SQL patterns work best for the USAspending data, "
                        "column quirks (e.g. recipient_parent_name vs recipient_name for grouping), "
                        "and data caveats the user discovers (e.g. negative obligations, masked "
                        "recipients)."
                    ),
                ),
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Terminal formatting helpers (unchanged from universal_computer version)
# ---------------------------------------------------------------------------

DIM = "\033[2;39m"
DIM_CYAN = "\033[2;36m"
DIM_BLUE = "\033[2;34m"
DIM_YELLOW = "\033[2;33m"
DIM_GREEN = "\033[2;32m"
RESET = "\033[0m"

_SQL_KEYWORDS = (
    r"\b(?:SELECT|FROM|WHERE|JOIN|LEFT|RIGHT|INNER|OUTER|CROSS|FULL|NATURAL|ON|AND|OR"
    r"|NOT|IN|IS|NULL|AS|WITH|GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|OFFSET|UNION"
    r"|ALL|DISTINCT|CASE|WHEN|THEN|ELSE|END|EXISTS|BETWEEN|LIKE|INSERT|UPDATE"
    r"|DELETE|CREATE|DROP|ALTER|SET|VALUES|INTO|TABLE|INDEX|VIEW|ASC|DESC|BY"
    r"|OVER|PARTITION\s+BY)\b"
)

_SQL_FUNCTIONS = (
    r"\b(?:COUNT|SUM|AVG|MIN|MAX|COALESCE|CAST|SUBSTR|LENGTH|ROUND|ABS|IFNULL"
    r"|NULLIF|REPLACE|TRIM|UPPER|LOWER|DATE|DATETIME|STRFTIME|TYPEOF|TOTAL"
    r"|GROUP_CONCAT|PRINTF|ROW_NUMBER|RANK|DENSE_RANK)(?=\s*\()"
)

_SQL_STRING = r"'(?:''|[^'])*'"


def _highlight_sql(sql: str) -> str:
    """Apply ANSI syntax highlighting to a SQL string."""
    placeholders: list[str] = []

    def _stash_string(m: re.Match[str]) -> str:
        placeholders.append(m.group(0))
        return f"\x00STR{len(placeholders) - 1}\x00"

    result = re.sub(_SQL_STRING, _stash_string, sql)

    result = re.sub(
        _SQL_KEYWORDS,
        lambda m: f"{DIM_BLUE}{m.group(0)}{DIM}",
        result,
        flags=re.IGNORECASE,
    )
    result = re.sub(
        _SQL_FUNCTIONS,
        lambda m: f"{DIM_YELLOW}{m.group(0)}{DIM}",
        result,
        flags=re.IGNORECASE,
    )

    def _restore_string(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        return f"{DIM_GREEN}{placeholders[idx]}{DIM}"

    result = re.sub(r"\x00STR(\d+)\x00", _restore_string, result)
    return result


def _format_tool_args(name: str, arguments: str) -> str:
    """Format a tool call for display, pretty-printing SQL queries."""
    if name == "run_sql":
        try:
            args = json.loads(arguments)
            query = args.get("query", "")
            limit = args.get("limit")
            header = f"  {DIM}[SQL]"
            if limit is not None:
                header += f"  (limit {limit})"
            header += RESET
            highlighted = _highlight_sql(query)
            sql = textwrap.indent(highlighted, "    ")
            return f"{header}\n{DIM}{sql}{RESET}"
        except Exception:
            pass
    return f"  {DIM}[tool] {name}({arguments}){RESET}"


def _format_tool_result(output: str) -> str | None:
    """Format a tool result for display. Returns None for non-SQL results."""
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        if output.strip():
            return f"  {DIM}{output.strip()}{RESET}"
        return None

    columns = data.get("columns")
    rows = data.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        return None

    row_count = data.get("row_count", len(rows))
    display_count = data.get("display_count", len(rows))
    truncated = data.get("truncated", False)

    if not columns:
        return f"  {DIM_CYAN}\u2192 Result (0 rows){RESET}"

    # Build the summary line.
    parts = []
    if display_count < row_count:
        parts.append(f"showing {display_count} of {row_count}")
    else:
        parts.append(f"{row_count} rows")
    if truncated:
        parts.append("CSV truncated at limit")

    csv_file = data.get("csv_file")
    download_line = ""
    if csv_file and _downloads_base_url:
        download_line = f"\n  {DIM}\u2193 {_downloads_base_url}{csv_file}{RESET}"

    # Try to fit the table in the terminal. If too wide, skip it —
    # the model's prose summary + download link are enough.
    try:
        term_width = os.get_terminal_size().columns
    except OSError:
        term_width = 120

    widths = [len(str(c)) for c in columns]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(str(val) if val is not None else "NULL"))

    # 4 leading spaces + "| " between each col + trailing " |"
    table_width = 4 + sum(widths) + 3 * len(widths) + 1

    if table_width > term_width:
        header = f"  {DIM_CYAN}\u2192 Result ({row_count} rows) \u2014 too wide to print in terminal, download below{RESET}"
        return f"{header}{download_line}"

    def fmt_row(vals: list[Any]) -> str:
        cells = []
        for v, w in zip(vals, widths, strict=False):
            cells.append(str(v if v is not None else "NULL").ljust(w))
        return "    | " + " | ".join(cells) + " |"

    lines = [fmt_row(columns)]
    lines.append("    |" + "|".join("-" * (w + 2) for w in widths) + "|")
    for row in rows:
        lines.append(fmt_row(row))

    header = f"  {DIM_CYAN}\u2192 Result ({', '.join(parts)})"
    table = "\n".join(lines)
    return f"{header}\n{table}{RESET}{download_line}"


# ---------------------------------------------------------------------------
# Multi-turn REPL using Runner.run_streamed()
# ---------------------------------------------------------------------------


async def run_turn(
    agent: SandboxAgent,
    conversation: list[Any],
    question: str,
    run_config: RunConfig,
) -> list[Any]:
    """Run one conversational turn and return the updated conversation history."""
    input_items = conversation + [{"role": "user", "content": question}]

    result = Runner.run_streamed(agent, input_items, run_config=run_config)

    async for event in result.stream_events():
        if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
            print(event.data.delta, end="", flush=True)
            continue

        if event.type != "run_item_stream_event":
            continue

        if event.name == "tool_called":
            item = event.item
            raw = getattr(item, "raw_item", None)
            if raw is not None:
                name = getattr(raw, "name", "")
                arguments = getattr(raw, "arguments", "")
                print()
                print(_format_tool_args(name, arguments))
            continue

        if event.name == "tool_output":
            item = event.item
            output = getattr(item, "output", "")
            if isinstance(output, str):
                formatted = _format_tool_result(output)
                if formatted is not None:
                    print(formatted)
            print()
            continue

    print()

    # Build the full conversation history for the next turn using the SDK's
    # built-in conversion, which correctly serializes all item types.
    return result.to_input_list()


# ---------------------------------------------------------------------------
# Session state persistence for pause/resume
# ---------------------------------------------------------------------------


def _load_session_state() -> DaytonaSandboxSessionState | None:
    """Load saved session state from disk, or return None."""
    if not SESSION_STATE_PATH.exists():
        return None
    try:
        return DaytonaSandboxSessionState.model_validate_json(SESSION_STATE_PATH.read_text())
    except Exception:
        return None


def _save_session_state(state: DaytonaSandboxSessionState) -> None:
    """Persist session state to disk so the sandbox can be reused next run."""
    SESSION_STATE_PATH.write_text(state.model_dump_json(indent=2))


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------


async def main() -> None:
    agent = build_agent()

    instrumentation = Instrumentation(
        sinks=[JsonlOutboxSink(AUDIT_LOG_PATH)],
        payload_policy=EventPayloadPolicy(include_exec_output=True),
    )
    RESULTS_PORT = 8080

    client = DaytonaSandboxClient(instrumentation=instrumentation)
    client_options = DaytonaSandboxClientOptions(
        pause_on_exit=True,
        exposed_ports=(RESULTS_PORT,),
    )

    # Try to resume a previously paused sandbox.
    saved_state = _load_session_state()
    sandbox = None
    destroy = False

    try:
        if saved_state is not None:
            old_sandbox_id = saved_state.sandbox_id
            try:
                sandbox = await client.resume(saved_state)
                assert isinstance(sandbox.state, DaytonaSandboxSessionState)
                if sandbox.state.sandbox_id == old_sandbox_id:
                    print("Reconnected to existing sandbox.")
                else:
                    print("Previous sandbox no longer exists. Created a new one.")
            except Exception as e:
                print(f"Could not resume previous sandbox: {e}")
                saved_state = None
                sandbox = None

        if sandbox is None:
            sandbox = await client.create(manifest=agent.default_manifest, options=client_options)

        await sandbox.start()

        # Persist state immediately so crashes don't orphan the sandbox.
        assert isinstance(sandbox.state, DaytonaSandboxSessionState)
        _save_session_state(sandbox.state)

        # Build database inside sandbox (idempotent — skips if DB already exists).
        print("Setting up database (may take a few minutes on first run)...")
        result = await sandbox.exec("python3", "setup_db.py", timeout=1800.0)
        stdout = result.stdout.decode("utf-8", errors="replace")
        if stdout.strip():
            print(stdout)
        if not result.ok():
            stderr = result.stderr.decode("utf-8", errors="replace")
            print(f"Database setup failed:\n{stderr}", file=sys.stderr)
            sys.exit(1)

        # Start a file server in the sandbox so query results can be downloaded.
        await sandbox.exec("mkdir -p results", timeout=5.0)
        await sandbox.exec(
            f"nohup python3 -m http.server {RESULTS_PORT} --directory results > /dev/null 2>&1 &",
            timeout=5.0,
        )

        # Resolve the Daytona signed URL for the file server.
        global _downloads_base_url
        try:
            endpoint = await sandbox.resolve_exposed_port(RESULTS_PORT)
            _downloads_base_url = endpoint.url_for("http")
        except Exception as e:
            print(f"  Warning: could not resolve download URL: {e}")

        run_config = RunConfig(
            sandbox=SandboxRunConfig(session=sandbox),
            workflow_name="NASA Spending Q&A",
        )

        downloads_line = ""
        if _downloads_base_url:
            downloads_line = f"\n  Browse results: {DIM_CYAN}{_downloads_base_url}{RESET}"

        print(f"""
{DIM}{"=" * 60}{RESET}
  NASA Spending Q&A (FY2021\u2013FY2025)

  Data from USAspending.gov \u2014 contracts, grants, and IDVs
  awarded by NASA. Each row is a transaction (obligation).

  Includes: amounts, award descriptions, recipients, recipient
  locations, places of performance, industry and product
  categories, sub-agencies, and fiscal years.
{downloads_line}
  Type {DIM_CYAN}'exit'{RESET} to pause sandbox, {DIM_CYAN}'destroy'{RESET} to delete it.
{DIM}{"=" * 60}{RESET}
""")

        conversation: list[Any] = []

        while True:
            try:
                question = input("> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break

            cmd = question.strip().lower()
            if cmd == "exit":
                break
            if cmd == "destroy":
                destroy = True
                break

            if not question.strip():
                continue

            try:
                conversation = await run_turn(agent, conversation, question, run_config)
            except Exception as e:
                print(f"\nError: {e}")
            print()

        if destroy:
            assert isinstance(sandbox.state, DaytonaSandboxSessionState)
            sandbox.state.pause_on_exit = False
            SESSION_STATE_PATH.unlink(missing_ok=True)
            print("Deleting sandbox...")
        else:
            assert isinstance(sandbox.state, DaytonaSandboxSessionState)
            _save_session_state(sandbox.state)
            print("Saving memory and pausing sandbox (can take a couple of minutes)...")

    finally:
        if sandbox is not None:
            if destroy:
                # Skip memory flush — sandbox is being deleted.
                await sandbox.stop()
                await sandbox.shutdown()
            else:
                await sandbox.aclose()
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
