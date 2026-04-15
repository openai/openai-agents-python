# NASA Spending Text-to-SQL Agent

Multi-turn conversational agent that translates natural-language questions about NASA federal
spending into SQL queries, executes them against a local SQLite database, and returns structured
tabular results.

## How it works

1. **Schema knowledge**: The agent receives a compact schema summary in its system prompt and can
   read detailed per-table documentation from workspace files on demand.
2. **SQL execution**: A custom `SqlCapability` provides a `run_sql` tool with guardrails — read-only
   mode, statement validation, row limits, and query timeouts. The agent is instructed to use
   `run_sql` for all queries; the tool enforces read-only access at the SQLite level.
3. **Multi-turn conversation**: The agent retains context across turns, so you can ask follow-up
   questions like "break that down by year" or "just the top 5".
4. **Compaction**: Uses the `Compaction` capability to automatically summarize older conversation
   context, keeping long sessions within the model's context window.
5. **Pause/resume**: Type `exit` to pause the sandbox and quit. Run the script again to reconnect
   to the same paused sandbox — no re-download needed. If the sandbox can't be reconnected (e.g.
   it was deleted or expired), a fresh one is created and the database is rebuilt automatically.
6. **Memory**: Uses the `Memory` capability to extract learnings from each conversation and
   consolidate them into structured files. On subsequent sessions, the agent starts with context
   from previous conversations (useful query patterns, data caveats, etc.).

## Data

The database contains NASA federal spending data from [USAspending.gov](https://usaspending.gov),
defaulting to FY2021-FY2025 (configurable via `--start-fy`/`--end-fy` flags on `setup_db.py`).

It uses a single `spending` table where each row is one transaction (obligation, modification,
or de-obligation) on a federal award. The agent aggregates as needed via SQL.

The database is built automatically on first run (requires internet access in the sandbox).
Subsequent runs reuse the existing database.

## Prerequisites

- Python 3.12+
- `openai-agents` installed with Daytona support (`uv sync --extra daytona` from repo root)
- `OPENAI_API_KEY` environment variable set (for the LLM)
- `DAYTONA_API_KEY` environment variable set (for the sandbox — get one at [daytona.io](https://daytona.io))
- Internet access (for first-run database setup inside the sandbox)

## Run

From the repository root:

```bash
export OPENAI_API_KEY="sk-..."
export DAYTONA_API_KEY="..."
uv run python -m examples.sandbox.extensions.daytona.usaspending_text2sql.agent
```

## Example questions

```
> What are NASA's top 10 contractors by total spending?
> Break that down by fiscal year
> Which NASA centers award the most contracts?
> Show me grants to universities in California
> How has NASA spending changed over time?
> What are the largest individual awards in the last 3 years?
> Compare contract vs grant spending by year
```

## Architecture

```
daytona/usaspending_text2sql/
├── agent.py            — SandboxAgent definition + interactive REPL
├── sql_capability.py   — SqlCapability (Capability) with run_sql tool and guardrails
├── setup_db.py         — Runs inside sandbox; fetches data from USAspending API, builds SQLite DB
├── schema/
│   ├── overview.md     — Compact schema summary (injected into instructions)
│   └── tables/         — Per-table column documentation (read on demand via Shell capability)
└── README.md
```

### SQL guardrails (defense in depth)

1. **Connection-level**: SQLite opened with `?mode=ro` URI (read-only)
2. **PRAGMA**: `query_only = ON` prevents writes even if validation is bypassed
3. **Statement validation**: Only `SELECT`, `WITH`, `EXPLAIN`, `PRAGMA` are allowed
4. **Row limit**: Hard cap (default 100 rows) with truncation detection
5. **Timeout**: Queries killed after 30 seconds

### Audit log

All sandbox operations (exec calls, start/stop, SQL queries and their results) are logged to
`.audit_log.jsonl` as structured JSONL events via the SDK's `Instrumentation` and `JsonlOutboxSink`.
This is useful for debugging, replaying sessions, or inspecting exactly what SQL the agent ran.

### Sandbox

This example uses Daytona as its sandbox backend. The agent and capability definitions are
backend-agnostic, but the entrypoint (`agent.py`) hardcodes `DaytonaSandboxClient` and
Daytona-specific features like pause/resume.
