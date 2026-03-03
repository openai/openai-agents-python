#!/usr/bin/env bash
set -euo pipefail

repeat_count="${1:-5}"

pytest_args=(
  tests/test_asyncio_progress.py
  tests/test_run_step_execution.py
  -k
  "cancel or post_invoke"
)

for run in $(seq 1 "$repeat_count"); do
  echo "Async teardown stability run ${run}/${repeat_count}"
  uv run pytest -q "${pytest_args[@]}"
done
