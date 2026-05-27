from __future__ import annotations

import textwrap
from typing import Any, Literal

from agents.sandbox import Capability, ExecTimeoutError, Manifest
from agents.sandbox.session.base_sandbox_session import BaseSandboxSession
from agents.tool import FunctionTool

# Python script executed inside the sandbox to run SQL queries safely.
# Receives the query on stdin, enforces read-only mode and row limits.
_QUERY_RUNNER_SCRIPT = r"""
import csv, json, os, sqlite3, sys, time

db_path = sys.argv[1]
display_limit = int(sys.argv[2])
csv_limit = int(sys.argv[3])
results_dir = sys.argv[4] if len(sys.argv) > 4 else ""

query = sys.stdin.read().strip()
if not query:
    print("Error: empty query")
    sys.exit(0)

# Statement-level validation: only allow read-only operations
first_token = query.lstrip().split()[0].upper() if query.strip() else ""
if first_token not in ("SELECT", "WITH", "EXPLAIN", "PRAGMA"):
    print(f"Error: only SELECT, WITH, EXPLAIN, and PRAGMA statements are allowed (got {first_token})")
    sys.exit(0)

try:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    cursor = conn.execute(query)
    columns = [desc[0] for desc in cursor.description] if cursor.description else []
    rows = cursor.fetchmany(csv_limit + 1)
    conn.close()
except sqlite3.Error as e:
    print(f"SQL error: {e}")
    sys.exit(0)

if not columns:
    print(json.dumps({"columns": [], "rows": [], "row_count": 0, "truncated": False}))
    sys.exit(0)

csv_truncated = len(rows) > csv_limit
if csv_truncated:
    rows = rows[:csv_limit]

# Save full result as CSV for download
csv_file = ""
if results_dir:
    os.makedirs(results_dir, exist_ok=True)
    csv_file = f"query_{int(time.time())}_{os.getpid()}.csv"
    with open(os.path.join(results_dir, csv_file), "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        writer.writerows(rows)

# Return only display_limit rows to the model, but report total counts
total_rows = len(rows)
display_rows = rows[:display_limit]

result = {
    "columns": columns,
    "rows": display_rows,
    "row_count": total_rows,
    "display_count": len(display_rows),
    "truncated": csv_truncated,
}
if csv_file:
    result["csv_file"] = csv_file
    if total_rows > len(display_rows):
        result["note"] = f"Showing {len(display_rows)} of {total_rows} rows. Full result saved to CSV."

print(json.dumps(result))
"""


def _shell_quote(s: str) -> str:
    """Single-quote a string for safe shell interpolation."""
    return "'" + s.replace("'", "'\\''") + "'"


_SQL_CAPABILITY_INSTRUCTIONS = textwrap.dedent(
    """\
    When querying the database:
    - Always use `run_sql` to execute SQL. Never run sqlite3 directly via a shell.
    - Write standard SQLite-compatible SQL.
    - Prefer aggregations (GROUP BY, SUM, COUNT, AVG) over returning many raw rows.
    - The display shows up to 100 rows, but up to 10,000 rows are saved to a downloadable CSV.
      If the user needs a large export, let them know the full result is available via the download link.
    - Use the schema documentation files in schema/tables/ if you need column details.
    - Read schema/glossary.md for official definitions of USAspending terms.
    - For monetary values, the database stores amounts in dollars as REAL values.
    """
).strip()


def _make_run_sql_tool(
    session: BaseSandboxSession,
    db_path: str,
    max_display_rows: int,
    max_csv_rows: int,
    timeout_seconds: float,
    results_dir: str,
) -> FunctionTool:
    """Build a FunctionTool that executes read-only SQL inside the sandbox."""

    async def run_sql(query: str, limit: int | None = None) -> str:
        """Execute a read-only SQL query against the NASA USAspending SQLite database.

        Returns results as JSON with columns, rows, row_count, and truncated fields.
        Results are also saved as a downloadable CSV. The display is limited to a
        small number of rows, but the CSV may contain many more.

        Args:
            query: SQL SELECT query to execute against the USAspending database.
                Only read-only queries are allowed.
            limit: Optional display row limit override.
        """
        display_limit = max(1, min(limit or max_display_rows, max_display_rows))

        command = (
            f"printf '%s' {_shell_quote(query)} "
            f"| python3 -c {_shell_quote(_QUERY_RUNNER_SCRIPT)} "
            f"{_shell_quote(db_path)} {display_limit} {max_csv_rows}"
            f" {_shell_quote(results_dir)}"
        )

        try:
            result = await session.exec(command, timeout=timeout_seconds)
        except (ExecTimeoutError, TimeoutError):
            return f"Query timed out after {timeout_seconds}s. Try a simpler query or add a LIMIT."

        output = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")

        if not result.ok():
            return f"Execution error (exit {result.exit_code}):\n{stderr or output}"

        return output.strip() if output.strip() else "Query returned no results."

    from agents.tool import function_tool as _function_tool

    return _function_tool(run_sql, name_override="run_sql")


class SqlCapability(Capability):
    type: Literal["sql"] = "sql"
    db_path: str = "data/usaspending.db"
    max_display_rows: int = 100
    max_csv_rows: int = 10_000
    timeout_seconds: float = 30.0
    results_dir: str = "results"

    def bind(self, session: BaseSandboxSession) -> None:
        self.session = session

    def tools(self) -> list[Any]:
        if self.session is None:
            raise ValueError("SqlCapability is not bound to a SandboxSession")
        return [
            _make_run_sql_tool(
                session=self.session,
                db_path=self.db_path,
                max_display_rows=self.max_display_rows,
                max_csv_rows=self.max_csv_rows,
                timeout_seconds=self.timeout_seconds,
                results_dir=self.results_dir,
            )
        ]

    async def instructions(self, manifest: Manifest) -> str | None:
        return _SQL_CAPABILITY_INSTRUCTIONS
