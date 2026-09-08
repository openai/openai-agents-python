#!/usr/bin/env bash
# Fail fast on any error or undefined variable.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if command -v git >/dev/null 2>&1; then
  REPO_ROOT="$(git -C "${SCRIPT_DIR}" rev-parse --show-toplevel 2>/dev/null || true)"
fi
REPO_ROOT="${REPO_ROOT:-$(cd "${SCRIPT_DIR}/../../../.." && pwd)}"

cd "${REPO_ROOT}"

LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/code-change-verification.XXXXXX")"
HEARTBEAT_INTERVAL_SECONDS="${CODE_CHANGE_VERIFICATION_HEARTBEAT_SECONDS:-10}"
declare -a STEP_LAUNCHER=()
declare -a STEP_PIDS=()
declare -a STEP_NAMES=()
declare -a STEP_LOGS=()
declare -a STEP_STARTS=()
EXIT_STATUS=0

resolve_executable_path() {
  local name="$1"
  type -P "${name}" 2>/dev/null || true
}

configure_step_launcher() {
  local perl_path=""
  local python_path=""
  local uv_path=""

  perl_path="$(resolve_executable_path perl)"
  if [ -n "${perl_path}" ]; then
    STEP_LAUNCHER=("${perl_path}" -MPOSIX=setsid -e 'setsid() or die $!; exec @ARGV')
    return 0
  fi

  python_path="$(resolve_executable_path python3)"
  if [ -z "${python_path}" ]; then
    python_path="$(resolve_executable_path python)"
  fi
  if [ -n "${python_path}" ]; then
    STEP_LAUNCHER=("${python_path}" -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])')
    return 0
  fi

  uv_path="$(resolve_executable_path uv)"
  if [ -n "${uv_path}" ]; then
    STEP_LAUNCHER=("${uv_path}" run --no-sync python -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])')
    return 0
  fi

  echo "code-change-verification: perl, python3, python, or uv is required to manage parallel step process groups." >&2
  exit 1
}

configure_step_launcher

cleanup() {
  local trap_status="$?"
  local status="${EXIT_STATUS}"

  # Repeated signals must not interrupt cleanup or replace the primary failure.
  trap - EXIT
  trap '' INT TERM

  if [ "${status}" -eq 0 ]; then
    status="${trap_status}"
  fi

  if [ "${#STEP_PIDS[@]}" -gt 0 ]; then
    stop_running_steps
  fi

  rm -rf "${LOG_DIR}"
  exit "${status}"
}

on_interrupt() {
  if [ "${EXIT_STATUS}" -eq 0 ]; then
    EXIT_STATUS=130
  fi
}

on_terminate() {
  if [ "${EXIT_STATUS}" -eq 0 ]; then
    EXIT_STATUS=143
  fi
}

stop_running_steps() {
  local pid=""

  if [ "${#STEP_PIDS[@]}" -eq 0 ]; then
    return
  fi

  for pid in "${STEP_PIDS[@]}"; do
    if [ -n "${pid}" ]; then
      kill -TERM -- "-${pid}" 2>/dev/null || true
      # The launcher may not have created its process group yet.
      kill -TERM "${pid}" 2>/dev/null || true
    fi
  done

  sleep 1

  for pid in "${STEP_PIDS[@]}"; do
    if [ -n "${pid}" ]; then
      # A process group can remain alive after its leader exits, so escalate by group id unconditionally.
      kill -KILL -- "-${pid}" 2>/dev/null || true
      kill -KILL "${pid}" 2>/dev/null || true
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
}

clear_step() {
  local idx="$1"

  STEP_PIDS[$idx]=""
  STEP_NAMES[$idx]=""
  STEP_LOGS[$idx]=""
  STEP_STARTS[$idx]=""
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

  if [ "${EXIT_STATUS}" -ne 0 ]; then
    return "${EXIT_STATUS}"
  fi

  echo "Running make ${name}..."
  : > "${log_file}"
  # Start each step in its own process group so fail-fast cleanup can stop pytest worker trees too.
  "${STEP_LAUNCHER[@]}" "$@" >"${log_file}" 2>&1 &

  STEP_PIDS+=("$!")
  STEP_NAMES+=("${name}")
  STEP_LOGS+=("${log_file}")
  STEP_STARTS+=("$(date +%s)")
}

finish_step() {
  local idx="$1"
  local name="${STEP_NAMES[$idx]}"
  local status=0
  local pid=""
  local log_file=""
  local start_time=""
  local now

  pid="${STEP_PIDS[$idx]}"
  log_file="${STEP_LOGS[$idx]}"
  start_time="${STEP_STARTS[$idx]}"

  # Only wait supplies an exit status; the job table is just a readiness hint.
  wait "${pid}" || status=$?
  now=$(date +%s)

  if [ "${status}" -eq 0 ] && kill -0 -- "-${pid}" 2>/dev/null; then
    echo "code-change-verification: make ${name} left running processes." >&2
    status=1
  fi

  if [ "${status}" -eq 0 ]; then
    clear_step "${idx}"
    echo "make ${name} passed in $((now - start_time))s."
    return 0
  fi

  echo "code-change-verification: make ${name} failed with exit code ${status} after $((now - start_time))s." >&2
  echo "--- ${name} log (last 80 lines) ---" >&2
  tail -n 80 "${log_file}" >&2 || true
  return "${status}"
}

wait_for_parallel_steps() {
  local idx=""
  local pid=""
  local running_pids=""
  local pending_steps=0
  local next_heartbeat_at
  local now

  next_heartbeat_at=$(( $(date +%s) + HEARTBEAT_INTERVAL_SECONDS ))

  while :; do
    pending_steps=0
    # Defer cancellation until every launched PID has been registered.
    if [ "${EXIT_STATUS}" -ne 0 ]; then
      return "${EXIT_STATUS}"
    fi
    # Bash owns these jobs, so completion detection does not need sandboxed ps.
    running_pids="$(jobs -pr)"
    for idx in "${!STEP_PIDS[@]}"; do
      pid="${STEP_PIDS[$idx]}"
      if [ -z "${pid}" ]; then
        continue
      fi
      case $'\n'"${running_pids}"$'\n' in
        *$'\n'"${pid}"$'\n'*)
          pending_steps=1
          continue
          ;;
      esac
      finish_step "${idx}" || return $?
    done

    if [ "${pending_steps}" -eq 0 ]; then
      break
    fi
    now=$(date +%s)
    if [ "${now}" -ge "${next_heartbeat_at}" ]; then
      print_heartbeat
      next_heartbeat_at=$((now + HEARTBEAT_INTERVAL_SECONDS))
    fi
    sleep 0.1
  done
}

trap cleanup EXIT
trap on_interrupt INT
trap on_terminate TERM

start_step "format" make format
wait_for_parallel_steps || EXIT_STATUS=$?
if [ "${EXIT_STATUS}" -ne 0 ]; then
  exit "${EXIT_STATUS}"
fi

echo "Running make lint, make typecheck, and make tests in parallel..."
start_step "lint" make lint
start_step "typecheck" make typecheck
start_step "tests" make tests
wait_for_parallel_steps || EXIT_STATUS=$?

if [ "${EXIT_STATUS}" -ne 0 ]; then
  exit "${EXIT_STATUS}"
fi

trap - EXIT INT TERM
rm -rf "${LOG_DIR}"
echo "code-change-verification: all commands passed."
