#!/usr/bin/env python3
"""Run repository skill unittest modules without collecting the SDK suite."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def test_environment() -> dict[str, str]:
    """Keep process essentials, excluding credentials and user Git configuration."""
    env = {
        name: os.environ[name]
        for name in ("PATH", "SYSTEMROOT", "WINDIR", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL")
        if name in os.environ
    }
    # Release fixtures invoke python through make, so use this interpreter's directory first.
    env["PATH"] = os.pathsep.join((str(Path(sys.executable).parent), env.get("PATH", os.defpath)))
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_ALLOW_PROTOCOL="file",
        GIT_TERMINAL_PROMPT="0",
        UV_DEFAULT_INDEX="https://pypi.org/simple",
    )
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list", action="store_true", help="List discovered modules without running."
    )
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    skill_tests = sorted(repo.glob(".agents/skills/*/scripts/test_*.py"))
    if not skill_tests:
        parser.error("No repository skill test modules found.")
    tests = sorted([*skill_tests, *repo.glob(".github/scripts/test_run_repo_skill_tests.py")])
    print(f"Discovered {len(tests)} repository skill test modules:", flush=True)
    for path in tests:
        print(f"  {path.relative_to(repo).as_posix()}", flush=True)
    if args.list:
        return 0

    env = test_environment()
    failed = []
    for path in tests:
        print(f"\nRunning {path.relative_to(repo).as_posix()}", flush=True)
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", path.name, "-v"],
            cwd=path.parent,
            env=env,
            check=False,
        )
        if result.returncode:
            failed.append(path.relative_to(repo).as_posix())

    print(f"\nCompleted {len(tests)} modules; {len(failed)} failed.", flush=True)
    for path in failed:
        print(f"  FAILED: {path}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
