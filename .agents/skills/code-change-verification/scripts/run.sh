#!/usr/bin/env bash
set -euo pipefail
# Job control gives each background make invocation its own process group.
set -m

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if command -v git >/dev/null 2>&1; then
  REPO_ROOT="$(git -C "${SCRIPT_DIR}" rev-parse --show-toplevel 2>/dev/null || true)"
fi
REPO_ROOT="${REPO_ROOT:-$(cd "${SCRIPT_DIR}/../../../.." && pwd)}"
cd "${REPO_ROOT}"

STEP_PID=""
EXIT_STATUS=0

stop_step() {
  if [ -z "${STEP_PID}" ]; then
    return
  fi
  # Descendants can outlive make, so always address its process group.
  if kill -TERM -- "-${STEP_PID}" 2>/dev/null; then
    sleep 1
    kill -KILL -- "-${STEP_PID}" 2>/dev/null || true
  fi
  wait "${STEP_PID}" 2>/dev/null || true
  STEP_PID=""
}

cleanup() {
  local status=$?
  if [ "${EXIT_STATUS}" -ne 0 ]; then
    status=${EXIT_STATUS}
  fi
  trap - EXIT
  trap '' INT TERM
  stop_step
  exit "${status}"
}

cancel() {
  if [ "${EXIT_STATUS}" -eq 0 ]; then
    EXIT_STATUS=$1
  fi
  # Defer only until a just-launched PID has been registered.
  if [ -n "${STEP_PID}" ]; then
    exit "${EXIT_STATUS}"
  fi
}

trap cleanup EXIT
trap 'cancel 130' INT
trap 'cancel 143' TERM

for step in format lint typecheck tests; do
  if [ "${EXIT_STATUS}" -ne 0 ]; then
    exit "${EXIT_STATUS}"
  fi
  echo "Running make ${step}..."
  started=${SECONDS}
  make "${step}" &
  STEP_PID=$!
  if [ "${EXIT_STATUS}" -eq 0 ]; then
    wait "${STEP_PID}" || {
      status=$?
      if [ "${EXIT_STATUS}" -eq 0 ]; then
        EXIT_STATUS=${status}
      fi
    }
  fi
  if [ "${EXIT_STATUS}" -ne 0 ]; then
    echo "code-change-verification: make ${step} failed with exit code ${EXIT_STATUS}." >&2
    exit "${EXIT_STATUS}"
  fi
  stop_step
  if [ "${EXIT_STATUS}" -ne 0 ]; then
    exit "${EXIT_STATUS}"
  fi
  echo "make ${step} passed in $((SECONDS - started))s."
done

echo "code-change-verification: all commands passed."
