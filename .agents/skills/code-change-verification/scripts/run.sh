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
STATUS_PIPE="${LOG_DIR}/status.fifo"
HEARTBEAT_INTERVAL_SECONDS="${CODE_CHANGE_VERIFICATION_HEARTBEAT_SECONDS:-10}"
declare -a STEP_LAUNCHER=()
declare -a STEP_PIDS=()
declare -a STEP_NAMES=()
declare -a STEP_LOGS=()
declare -a STEP_STARTS=()
RUNNING_STEPS=0
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
    if [ -n "${pid}" ]; then
      # A process group can remain alive after its leader exits, so escalate by group id unconditionally.
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

clear_step() {
  local idx="$1"

  STEP_PIDS[$idx]=""
  STEP_NAMES[$idx]=""
  STEP_LOGS[$idx]=""
  STEP_STARTS[$idx]=""
  RUNNING_STEPS=$((RUNNING_STEPS - 1))
}

step_pid_is_alive() {
  local pid="$1"
  local state=""

  if ! kill -0 "${pid}" 2>/dev/null; then
    return 1
  fi

  state="$(ps -o stat= -p "${pid}" 2>/dev/null | tr -d '[:space:]')"
  case "${state}" in
    Z*|z*|"")
      return 1
      ;;
  esac

  return 0
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
  : > "${log_file}"
  # Start each step in its own process group so fail-fast cleanup can stop pytest worker trees too.
  "${STEP_LAUNCHER[@]}" \
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
  wait "${pid}" 2>/dev/null || true

  if [ "${status}" -eq 0 ]; then
    clear_step "${idx}"
    echo "make ${name} passed in $((now - start_time))s."
    return 0
  fi

  echo "code-change-verification: make ${name} failed with exit code ${status} after $((now - start_time))s." >&2
  echo "--- ${name} log (last 80 lines) ---" >&2
  tail -n 80 "${log_file}" >&2 || true
  stop_running_steps
  return "${status}"
}

check_for_missing_reporters() {
  local idx=""
  local pid=""
  local name=""
  local log_file=""
  local start_time=""
  local now
  local step_status=0

  for idx in "${!STEP_PIDS[@]}"; do
    pid="${STEP_PIDS[$idx]}"
    if [ -z "${pid}" ] || step_pid_is_alive "${pid}"; then
      continue
    fi

    if try_finish_step_from_status_pipe 1; then
      if [ "${STATUS_PIPE_DRAINED}" -eq 1 ]; then
        return 0
      fi
    else
      step_status=$?
      return "${step_status}"
    fi

    name="${STEP_NAMES[$idx]}"
    log_file="${STEP_LOGS[$idx]}"
    start_time="${STEP_STARTS[$idx]}"
    now=$(date +%s)
    wait "${pid}" 2>/dev/null || true

    echo "code-change-verification: make ${name} exited before reporting completion status after $((now - start_time))s." >&2
    echo "--- ${name} log (last 80 lines) ---" >&2
    tail -n 80 "${log_file}" >&2 || true
    stop_running_steps
    return 1
  done

  return 0
}

STATUS_PIPE_DRAINED=0

try_finish_step_from_status_pipe() {
  local timeout="$1"
  local name=""
  local status=""
  local step_status=0

  STATUS_PIPE_DRAINED=0
  if ! IFS=$'\t' read -r -t "${timeout}" name status <&3; then
    return 0
  fi

  STATUS_PIPE_DRAINED=1
  finish_step "${name}" "${status}"
  step_status=$?
  if [ "${step_status}" -ne 0 ]; then
    return "${step_status}"
  fi

  return 0
}

wait_for_parallel_steps() {
  local name=""
  local status=""
  local step_status=""
  local next_heartbeat_at
  local now

  next_heartbeat_at=$(( $(date +%s) + HEARTBEAT_INTERVAL_SECONDS ))

  while [ "${RUNNING_STEPS}" -gt 0 ]; do
    if try_finish_step_from_status_pipe 1; then
      if [ "${STATUS_PIPE_DRAINED}" -eq 1 ]; then
        continue
      fi
    else
      step_status=$?
      if [ "${step_status}" -ne 0 ]; then
        return "${step_status}"
      fi
      continue
    fi

    check_for_missing_reporters
    step_status=$?
    if [ "${step_status}" -ne 0 ]; then
      return "${step_status}"
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
