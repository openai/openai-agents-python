#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
PID_FILE="$ROOT/.tmp/examples-auto-run.pid"
LOG_DIR="$ROOT/.tmp/examples-start-logs"
RERUN_FILE="$ROOT/.tmp/examples-rerun.txt"

ensure_dirs() {
  mkdir -p "$LOG_DIR" "$ROOT/.tmp"
}

is_running() {
  local pid="$1"
  [[ -n "$pid" ]] && ps -p "$pid" >/dev/null 2>&1
}

cmd_start() {
  ensure_dirs
  local background=0
  if [[ "${1:-}" == "--background" ]]; then
    background=1
    shift
  fi

  local ts main_log stdout_log
  ts="$(date +%Y%m%d-%H%M%S)"
  main_log="$LOG_DIR/main_${ts}.log"
  stdout_log="$LOG_DIR/stdout_${ts}.log"

  local run_cmd=(
    uv run examples/run_examples.py
    --auto-mode
    --write-rerun
    --main-log "$main_log"
    --logs-dir "$LOG_DIR"
  )

  if [[ "$background" -eq 1 ]]; then
    if [[ -f "$PID_FILE" ]]; then
      local pid
      pid="$(cat "$PID_FILE" 2>/dev/null || true)"
      if is_running "$pid"; then
        echo "examples/run_examples.py already running (pid=$pid)."
        exit 1
      fi
    fi
    (
      trap '' HUP
      export EXAMPLES_INTERACTIVE_MODE="${EXAMPLES_INTERACTIVE_MODE:-auto}"
      export APPLY_PATCH_AUTO_APPROVE="${APPLY_PATCH_AUTO_APPROVE:-1}"
      export SHELL_AUTO_APPROVE="${SHELL_AUTO_APPROVE:-1}"
      export AUTO_APPROVE_MCP="${AUTO_APPROVE_MCP:-1}"
      export EXAMPLES_INCLUDE_INTERACTIVE="${EXAMPLES_INCLUDE_INTERACTIVE:-1}"
      export EXAMPLES_INCLUDE_SERVER="${EXAMPLES_INCLUDE_SERVER:-0}"
      export EXAMPLES_INCLUDE_AUDIO="${EXAMPLES_INCLUDE_AUDIO:-0}"
      export EXAMPLES_INCLUDE_EXTERNAL="${EXAMPLES_INCLUDE_EXTERNAL:-0}"
      cd "$ROOT"
      exec "${run_cmd[@]}" "$@" > >(tee "$stdout_log") 2>&1
    ) &
    local pid=$!
    echo "$pid" >"$PID_FILE"
    echo "Started run_examples.py (pid=$pid)"
    echo "Main log: $main_log"
    echo "Stdout log: $stdout_log"
    echo "Run '.agents/skills/examples-auto-run/scripts/run.sh validate \"$main_log\"' after it finishes."
    return 0
  fi

  export EXAMPLES_INTERACTIVE_MODE="${EXAMPLES_INTERACTIVE_MODE:-auto}"
  export APPLY_PATCH_AUTO_APPROVE="${APPLY_PATCH_AUTO_APPROVE:-1}"
  export SHELL_AUTO_APPROVE="${SHELL_AUTO_APPROVE:-1}"
  export AUTO_APPROVE_MCP="${AUTO_APPROVE_MCP:-1}"
  export EXAMPLES_INCLUDE_INTERACTIVE="${EXAMPLES_INCLUDE_INTERACTIVE:-1}"
  export EXAMPLES_INCLUDE_SERVER="${EXAMPLES_INCLUDE_SERVER:-0}"
  export EXAMPLES_INCLUDE_AUDIO="${EXAMPLES_INCLUDE_AUDIO:-0}"
  export EXAMPLES_INCLUDE_EXTERNAL="${EXAMPLES_INCLUDE_EXTERNAL:-0}"
  cd "$ROOT"
  set +e
  "${run_cmd[@]}" "$@" 2>&1 | tee "$stdout_log"
  local run_status=${PIPESTATUS[0]}
  set -e
  return "$run_status"
}

cmd_stop() {
  if [[ ! -f "$PID_FILE" ]]; then
    echo "No pid file; nothing to stop."
    return 0
  fi
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -z "$pid" ]]; then
    rm -f "$PID_FILE"
    echo "Pid file empty; cleaned."
    return 0
  fi
  if ! is_running "$pid"; then
    rm -f "$PID_FILE"
    echo "Process $pid not running; cleaned pid file."
    return 0
  fi
  echo "Stopping pid $pid ..."
  kill "$pid" 2>/dev/null || true
  sleep 1
  if is_running "$pid"; then
    echo "Sending SIGKILL to $pid ..."
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  echo "Stopped."
}

cmd_status() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if is_running "$pid"; then
      echo "Running (pid=$pid)"
      return 0
    fi
  fi
  echo "Not running."
}

cmd_logs() {
  ensure_dirs
  ls -1t "$LOG_DIR"
}

cmd_tail() {
  ensure_dirs
  local file="${1:-}"
  if [[ -z "$file" ]]; then
    file="$(ls -1t "$LOG_DIR" | head -n1)"
  fi
  if [[ -z "$file" ]]; then
    echo "No log files yet."
    exit 1
  fi
  tail -f "$LOG_DIR/$file"
}

collect_rerun() {
  ensure_dirs
  local log_file="${1:-}"
  if [[ -z "$log_file" ]]; then
    log_file="$(ls -1t "$LOG_DIR"/main_*.log 2>/dev/null | head -n1)"
  fi
  if [[ -z "$log_file" ]] || [[ ! -f "$log_file" ]]; then
    echo "No main log file found."
    exit 1
  fi
  cd "$ROOT"
  uv run examples/run_examples.py --collect "$log_file" --output "$RERUN_FILE"
}

cmd_rerun() {
  ensure_dirs
  local file="${1:-$RERUN_FILE}"
  if [[ ! -s "$file" ]]; then
    echo "Rerun list is empty: $file"
    exit 0
  fi
  local ts main_log stdout_log
  ts="$(date +%Y%m%d-%H%M%S)"
  main_log="$LOG_DIR/main_${ts}.log"
  stdout_log="$LOG_DIR/stdout_${ts}.log"
  cd "$ROOT"
  export EXAMPLES_INTERACTIVE_MODE="${EXAMPLES_INTERACTIVE_MODE:-auto}"
  export APPLY_PATCH_AUTO_APPROVE="${APPLY_PATCH_AUTO_APPROVE:-1}"
  export SHELL_AUTO_APPROVE="${SHELL_AUTO_APPROVE:-1}"
  export AUTO_APPROVE_MCP="${AUTO_APPROVE_MCP:-1}"
  set +e
  uv run examples/run_examples.py --auto-mode --rerun-file "$file" --write-rerun --main-log "$main_log" --logs-dir "$LOG_DIR" 2>&1 | tee "$stdout_log"
  local run_status=${PIPESTATUS[0]}
  set -e
  return "$run_status"
}

usage() {
  cat <<'EOF'
Usage: run.sh <start|stop|status|logs|tail|collect|rerun> [args...]

Commands:
  start [--filter ... | other args]   Run examples in auto mode (foreground). Pass --background to run detached.
  stop                                Kill the running auto-run (if any).
  status                              Show whether it is running.
  logs                                List log files (.tmp/examples-start-logs).
  tail [logfile]                      Tail the latest (or specified) log.
  collect [main_log]                  Parse a main log and write failed examples to .tmp/examples-rerun.txt.
  rerun [rerun_file]                  Run only the examples listed in .tmp/examples-rerun.txt.

Environment overrides:
  EXAMPLES_INTERACTIVE_MODE (default auto)
  EXAMPLES_INCLUDE_SERVER/INTERACTIVE/AUDIO/EXTERNAL (defaults: 0/1/0/0)
  APPLY_PATCH_AUTO_APPROVE, SHELL_AUTO_APPROVE, AUTO_APPROVE_MCP (default 1 in auto mode)
EOF
}

default_cmd="start"
if [[ $# -eq 0 && -s "$RERUN_FILE" ]]; then
  default_cmd="rerun"
fi

case "${1:-$default_cmd}" in
  start) shift || true; cmd_start "$@" ;;
  stop) shift || true; cmd_stop ;;
  status) shift || true; cmd_status ;;
  logs) shift || true; cmd_logs ;;
  tail) shift; cmd_tail "${1:-}" ;;
  collect) shift || true; collect_rerun "${1:-}" ;;
  rerun) shift || true; cmd_rerun "${1:-}" ;;
  *) usage; exit 1 ;;
esac
