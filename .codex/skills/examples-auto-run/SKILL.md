---
name: examples-auto-run
description: Run python examples in auto mode with logging, rerun helpers, and background control.
---

# examples-auto-run

## What it does

- Runs `uv run examples/run_examples.py` with:
  - `EXAMPLES_INTERACTIVE_MODE=auto` (auto-input/auto-approve).
  - Per-example logs under `.tmp/examples-start-logs/`.
  - Main summary log path passed via `--main-log` (also under `.tmp/examples-start-logs/`).
  - Generates a rerun list of failures at `.tmp/examples-rerun.txt` when `--write-rerun` is set.
- Provides start/stop/status/logs/tail/collect/rerun helpers via `run.sh`.
- Background option keeps the process running with a pidfile; `stop` cleans it up.

## Usage

```bash
# Start (auto mode; interactive included by default)
.codex/skills/examples-auto-run/scripts/run.sh start [extra args to run_examples.py]
# Examples:
.codex/skills/examples-auto-run/scripts/run.sh start --filter basic
.codex/skills/examples-auto-run/scripts/run.sh start --include-server --include-audio

# Check status
.codex/skills/examples-auto-run/scripts/run.sh status

# Stop running job
.codex/skills/examples-auto-run/scripts/run.sh stop

# List logs
.codex/skills/examples-auto-run/scripts/run.sh logs

# Tail latest log (or specify one)
.codex/skills/examples-auto-run/scripts/run.sh tail
.codex/skills/examples-auto-run/scripts/run.sh tail main_20260113-123000.log

# Collect rerun list from a main log (defaults to latest main_*.log)
.codex/skills/examples-auto-run/scripts/run.sh collect

# Rerun only failed entries from rerun file (auto mode)
.codex/skills/examples-auto-run/scripts/run.sh rerun
```

## Defaults (overridable via env)

- `EXAMPLES_INTERACTIVE_MODE=auto`
- `EXAMPLES_INCLUDE_INTERACTIVE=1`
- `EXAMPLES_INCLUDE_SERVER=0`
- `EXAMPLES_INCLUDE_AUDIO=0`
- `EXAMPLES_INCLUDE_EXTERNAL=0`
- Auto-approvals in auto mode: `APPLY_PATCH_AUTO_APPROVE=1`, `SHELL_AUTO_APPROVE=1`, `AUTO_APPROVE_MCP=1`

## Log locations

- Main logs: `.tmp/examples-start-logs/main_*.log`
- Per-example logs (from `run_examples.py`): `.tmp/examples-start-logs/<module_path>.log`
- Rerun list: `.tmp/examples-rerun.txt`
- Stdout logs: `.tmp/examples-start-logs/stdout_*.log`

## Notes

- The runner delegates to `uv run examples/run_examples.py`, which already writes per-example logs and supports `--collect`, `--rerun-file`, and `--print-auto-skip`.
- `start` uses `--write-rerun` so failures are captured automatically.
- If `.tmp/examples-rerun.txt` exists and is non-empty, invoking the skill with no args runs `rerun` by default.

## Behavioral validation

- After every foreground `start` or `rerun`, the script automatically runs `uv run examples/behavioral_validation.py` against the generated main log.
- The validator:
  1. Reads the example source to derive expected messages (print strings and prompt/message assignments).
  2. Reads each passed example’s log and checks those messages appeared.
  3. Reports per-example status with the full matching log lines; missing expectations are flagged.
- Background runs do not validate automatically; after they finish, run:
  ```bash
  .codex/skills/examples-auto-run/scripts/run.sh validate <main_log_path>
  ```
