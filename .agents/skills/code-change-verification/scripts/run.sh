#!/usr/bin/env bash
# Fail fast on any error or undefined variable.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if command -v git >/dev/null 2>&1; then
  REPO_ROOT="$(git -C "${SCRIPT_DIR}" rev-parse --show-toplevel 2>/dev/null || true)"
fi
REPO_ROOT="${REPO_ROOT:-$(cd "${SCRIPT_DIR}/../../../.." && pwd)}"

cd "${REPO_ROOT}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "code-change-verification: python3 is required to manage parallel step process groups." >&2
  exit 1
fi

LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/code-change-verification.XXXXXX")"
STATUS_PIPE="${LOG_DIR}/status.fifo"
HEARTBEAT_INTERVAL_SECONDS="${CODE_CHANGE_VERIFICATION_HEARTBEAT_SECONDS:-10}"
declare -a STEP_PIDS=()
declare -a STEP_NAMES=()
declare -a STEP_LOGS=()
declare -a STEP_STARTS=()
RUNNING_STEPS=0
EXIT_STATUS=0

mkfifo "${STATUS_PIPE}"
exec 3<> "${STATUS_PIPE}"

cleanup() {
  local trap_status="$?"
  local status="${EXIT_STATUS}"

  if [ "${status}" -eq 0 ]; then
    status="${trap_status}"
  fi

  if [ "${#STEP_PIDS[@]}" -gt 0 ]; then
    stop_running_steps
  fi

  exec 3>&- 3<&- || true
  rm -rf "${LOG_DIR}"
  exit "${status}"
}

on_interrupt() {
  EXIT_STATUS=130
  exit 130
}

on_terminate() {
  EXIT_STATUS=143
  exit 143
}

stop_running_steps() {
  local pid=""

  if [ "${#STEP_PIDS[@]}" -eq 0 ]; then
    return
  fi

  for pid in "${STEP_PIDS[@]}"; do
    if [ -n "${pid}" ]; then
      kill -TERM -- "-${pid}" 2>/dev/null || true
    fi
  done

  sleep 1

  for pid in "${STEP_PIDS[@]}"; do
    if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
      kill -KILL -- "-${pid}" 2>/dev/null || true
    fi
  done

  for pid in "${STEP_PIDS[@]}"; do
    if [ -n "${pid}" ]; then
      wait "${pid}" 2>/dev/null || true
    fi
  done

  STEP_PIDS=()
  STEP_NAMES=()
  STEP_LOGS=()
  STEP_STARTS=()
  RUNNING_STEPS=0
}

find_step_index() {
  local target_name="$1"
  local idx=""

  for idx in "${!STEP_NAMES[@]}"; do
    if [ "${STEP_NAMES[$idx]}" = "${target_name}" ]; then
      echo "${idx}"
      return 0
    fi
  done

  return 1
}

print_heartbeat() {
  local now
  local idx=""
  local name=""
  local start_time=""
  local elapsed=""
  local running=""

  now=$(date +%s)

  for idx in "${!STEP_NAMES[@]}"; do
    name="${STEP_NAMES[$idx]}"
    start_time="${STEP_STARTS[$idx]}"

    if [ -z "${name}" ]; then
      continue
    fi

    elapsed=$((now - start_time))
    if [ -n "${running}" ]; then
      running="${running}, "
    fi
    running="${running}${name} (${elapsed}s)"
  done

  if [ -n "${running}" ]; then
    echo "code-change-verification: still running: ${running}."
  fi
}

start_step() {
  local name="$1"
  shift
  local log_file="${LOG_DIR}/${name}.log"

  echo "Running make ${name}..."
  # Start each step in its own process group so fail-fast cleanup can stop pytest worker trees too.
  python3 -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
    bash -c '
      step_name="$1"
      log_file="$2"
      status_pipe="$3"
      shift 3

      if "$@" >"$log_file" 2>&1; then
        status=0
      else
        status=$?
      fi

      printf "%s\t%s\n" "$step_name" "$status" >"$status_pipe"
      exit "$status"
    ' \
    bash "${name}" "${log_file}" "${STATUS_PIPE}" "$@" &

  STEP_PIDS+=("$!")
  STEP_NAMES+=("${name}")
  STEP_LOGS+=("${log_file}")
  STEP_STARTS+=("$(date +%s)")
  RUNNING_STEPS=$((RUNNING_STEPS + 1))
}

finish_step() {
  local name="$1"
  local status="$2"
  local idx=""
  local pid=""
  local log_file=""
  local start_time=""
  local now

  idx="$(find_step_index "${name}")"
  pid="${STEP_PIDS[$idx]}"
  log_file="${STEP_LOGS[$idx]}"
  start_time="${STEP_STARTS[$idx]}"

  now=$(date +%s)
  STEP_PIDS[$idx]=""
  STEP_NAMES[$idx]=""
  STEP_LOGS[$idx]=""
  STEP_STARTS[$idx]=""
  RUNNING_STEPS=$((RUNNING_STEPS - 1))
  wait "${pid}" 2>/dev/null || true

  if [ "${status}" -eq 0 ]; then
    echo "make ${name} passed in $((now - start_time))s."
    return 0
  fi

  echo "code-change-verification: make ${name} failed with exit code ${status} after $((now - start_time))s." >&2
  echo "--- ${name} log (last 80 lines) ---" >&2
  tail -n 80 "${log_file}" >&2 || true
  stop_running_steps
  return "${status}"
}

wait_for_parallel_steps() {
  local name=""
  local status=""
  local step_status=""
  local next_heartbeat_at
  local now

  next_heartbeat_at=$(( $(date +%s) + HEARTBEAT_INTERVAL_SECONDS ))

  while [ "${RUNNING_STEPS}" -gt 0 ]; do
    if IFS=$'\t' read -r -t 1 name status <&3; then
      finish_step "${name}" "${status}"
      step_status=$?
      if [ "${step_status}" -ne 0 ]; then
        return "${step_status}"
      fi
      continue
    fi

    now=$(date +%s)
    if [ "${now}" -ge "${next_heartbeat_at}" ]; then
      print_heartbeat
      next_heartbeat_at=$((now + HEARTBEAT_INTERVAL_SECONDS))
    fi
  done
}

trap cleanup EXIT
trap on_interrupt INT
trap on_terminate TERM

echo "Running make format..."
set +e
make format
EXIT_STATUS=$?
set -e

if [ "${EXIT_STATUS}" -ne 0 ]; then
  exit "${EXIT_STATUS}"
fi

echo "Running make lint, make typecheck, and make tests in parallel..."
start_step "lint" make lint
start_step "typecheck" make typecheck
start_step "tests" make tests
set +e
wait_for_parallel_steps
EXIT_STATUS=$?
set -e

if [ "${EXIT_STATUS}" -ne 0 ]; then
  exit "${EXIT_STATUS}"
fi

trap - EXIT INT TERM
exec 3>&- 3<&-
rm -rf "${LOG_DIR}"
echo "code-change-verification: all commands passed."
