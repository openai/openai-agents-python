#!/usr/bin/env bash
set -euo pipefail

repeat_count="${1:-5}"

asyncio_progress_args=(
  tests/test_asyncio_progress.py
)

run_step_execution_args=(
  tests/test_run_step_execution.py
  -k
  "cancel or post_invoke"
)

for run in $(seq 1 "$repeat_count"); do
  echo "Async teardown stability run ${run}/${repeat_count}"
  uv run pytest -q "${asyncio_progress_args[@]}"
  uv run pytest -q "${run_step_execution_args[@]}"
done
