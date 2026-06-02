#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

MAX_RUFF_FIX_FILES = 20
PYTHON_SUFFIXES = {".py", ".pyi"}


@dataclass
class HookState:
    last_tidy_fingerprint: str | None = None


def write_stop_block(reason: str, system_message: str) -> None:
    sys.stdout.write(
        json.dumps(
            {
                "decision": "block",
                "reason": reason,
                "systemMessage": system_message,
            }
        )
    )


def run_command(cwd: str, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            check=False,
            text=True,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(args, returncode=127, stdout="", stderr=str(exc))


def run_git(cwd: str, *args: str) -> subprocess.CompletedProcess[str]:
    return run_command(cwd, "git", *args)


def git_root(cwd: str) -> str:
    result = run_git(cwd, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git root lookup failed")
    return result.stdout.strip()


def parse_status_paths(repo_root: str) -> list[str]:
    unstaged = run_git(repo_root, "diff", "--name-only", "--diff-filter=ACMR")
    untracked = run_git(repo_root, "ls-files", "--others", "--exclude-standard")
    if unstaged.returncode != 0 or untracked.returncode != 0:
        return []

    paths = {
        line.strip()
        for result in (unstaged, untracked)
        for line in result.stdout.splitlines()
        if line.strip()
    }
    return sorted(paths)


def untracked_paths(repo_root: str, paths: list[str]) -> set[str]:
    if not paths:
        return set()

    result = run_git(repo_root, "ls-files", "--others", "--exclude-standard", "--", *paths)
    if result.returncode != 0:
        return set()

    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def fingerprint_for_paths(repo_root: str, paths: list[str]) -> str | None:
    if not paths:
        return None

    repo_root_path = Path(repo_root)
    untracked = untracked_paths(repo_root, paths)
    tracked_paths = [file_path for file_path in paths if file_path not in untracked]
    diff_parts: list[str] = []

    if tracked_paths:
        diff = run_git(repo_root, "diff", "--no-ext-diff", "--binary", "--", *tracked_paths)
        if diff.returncode == 0:
            diff_parts.append(diff.stdout)

    for file_path in sorted(untracked):
        try:
            digest = hashlib.sha256((repo_root_path / file_path).read_bytes()).hexdigest()
        except OSError:
            continue
        diff_parts.append(f"untracked:{file_path}:{digest}")

    if not diff_parts:
        return None

    return hashlib.sha256("\n".join(diff_parts).encode("utf-8")).hexdigest()


def state_dir() -> Path:
    return Path(tempfile.gettempdir()) / "openai-agents-python-codex-hooks"


def state_path(session_id: str, repo_root: str) -> Path:
    root_hash = hashlib.sha256(repo_root.encode("utf-8")).hexdigest()[:12]
    safe_session_id = "".join(
        ch if ch.isascii() and (ch.isalnum() or ch in "._-") else "_" for ch in session_id
    )
    return state_dir() / f"{safe_session_id}-{root_hash}.json"


def load_state(session_id: str, repo_root: str) -> HookState:
    file_path = state_path(session_id, repo_root)
    if not file_path.exists():
        return HookState()

    try:
        payload = json.loads(file_path.read_text())
    except (OSError, json.JSONDecodeError):
        return HookState()

    return HookState(last_tidy_fingerprint=payload.get("last_tidy_fingerprint"))


def save_state(session_id: str, repo_root: str, state: HookState) -> None:
    file_path = state_path(session_id, repo_root)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(asdict(state), indent=2))


def lint_fix_paths(repo_root: str) -> list[str]:
    return [
        file_path
        for file_path in parse_status_paths(repo_root)
        if Path(file_path).suffix in PYTHON_SUFFIXES
    ]


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "null")
    except json.JSONDecodeError:
        return

    if not isinstance(payload, dict):
        return

    session_id = payload.get("session_id")
    cwd = payload.get("cwd")
    if not isinstance(session_id, str) or not isinstance(cwd, str):
        return

    if payload.get("stop_hook_active"):
        return

    repo_root = git_root(cwd)
    current_paths = lint_fix_paths(repo_root)
    if not current_paths or len(current_paths) > MAX_RUFF_FIX_FILES:
        return

    state = load_state(session_id, repo_root)
    current_fingerprint = fingerprint_for_paths(repo_root, current_paths)
    if current_fingerprint is None or state.last_tidy_fingerprint == current_fingerprint:
        return

    format_result = run_command(repo_root, "uv", "run", "ruff", "format", "--", *current_paths)
    check_result: subprocess.CompletedProcess[str] | None = None
    if format_result.returncode == 0:
        check_result = run_command(
            repo_root,
            "uv",
            "run",
            "ruff",
            "check",
            "--fix",
            "--",
            *current_paths,
        )

    if format_result.returncode != 0:
        write_stop_block(
            "`uv run ruff format -- ...` failed for the touched Python files. "
            "Review the formatting step before wrapping up.",
            "Repo hook: targeted Ruff format failed.",
        )
        return

    if check_result and check_result.returncode != 0:
        write_stop_block(
            "`uv run ruff check --fix -- ...` failed for the touched Python files. "
            "Review the lint output before wrapping up.",
            "Repo hook: targeted Ruff lint fix failed.",
        )
        return

    updated_paths = lint_fix_paths(repo_root)
    updated_fingerprint = fingerprint_for_paths(repo_root, updated_paths)
    state.last_tidy_fingerprint = updated_fingerprint
    save_state(session_id, repo_root, state)

    if updated_fingerprint != current_fingerprint:
        write_stop_block(
            "I ran targeted tidy steps on the touched Python files "
            "(`ruff format` and `ruff check --fix`). Review the updated diff, "
            "then continue or wrap up.",
            "Repo hook: ran targeted Ruff tidy on touched files.",
        )


if __name__ == "__main__":
    main()
