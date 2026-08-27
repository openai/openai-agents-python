#!/usr/bin/env python3
"""Validate the Git invariants of an implementation-kickoff handoff."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath


class GitCommandError(RuntimeError):
    """Report a failed Git inspection command."""


def run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        errors="surrogateescape",
    )
    if check and result.returncode != 0:
        command = "git " + " ".join(args)
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Git error"
        raise GitCommandError(f"{command} failed: {detail}")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a clean, single-commit implementation-kickoff handoff."
    )
    parser.add_argument("--repo", type=Path, required=True, help="Path to the task worktree.")
    parser.add_argument(
        "--base",
        required=True,
        help="Expected parent commit or ref for the single handoff commit.",
    )
    parser.add_argument(
        "--expected-branch",
        required=True,
        help="Exact local branch name expected at HEAD.",
    )
    parser.add_argument(
        "--required-trailer-email",
        action="append",
        default=[],
        help="Email that must appear in a Co-authored-by trailer. Repeat as needed.",
    )
    parser.add_argument(
        "--shipped-path-manifest",
        type=Path,
        help=(
            "File containing the exact repository-relative paths expected in the handoff commit, "
            "one per line."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit the result as JSON.")
    return parser.parse_args()


def _nonblocking_opener(path: str, flags: int) -> int:
    return os.open(path, flags | getattr(os, "O_NONBLOCK", 0))


def load_shipped_paths(path: Path) -> set[str]:
    with open(path, "rb", opener=_nonblocking_opener) as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise ValueError(f"Shipped-path manifest must be a regular file: {path}")
        lines = file.read().decode().splitlines()
    if not lines:
        raise ValueError(f"Shipped-path manifest is empty: {path}")

    shipped_paths: set[str] = set()
    for line_number, raw_path in enumerate(lines, start=1):
        if not raw_path:
            raise ValueError(f"Shipped-path manifest contains a blank line at {line_number}.")
        path_value = PurePosixPath(raw_path)
        if path_value.is_absolute() or ".." in path_value.parts or str(path_value) != raw_path:
            raise ValueError(
                "Shipped-path manifest entries must be normalized repository-relative paths: "
                f"{raw_path!r}."
            )
        if raw_path in shipped_paths:
            raise ValueError(f"Duplicate shipped-path manifest entry: {raw_path}")
        shipped_paths.add(raw_path)
    return shipped_paths


def is_repository_root(path: Path) -> bool:
    if not path.is_dir():
        return False
    result = run_git(path, "rev-parse", "--show-toplevel", check=False)
    return result.returncode == 0 and Path(result.stdout.strip()).resolve() == path.resolve()


def hidden_index_paths(
    repo: Path,
    prefix: str = "",
    seen_repositories: frozenset[Path] = frozenset(),
) -> list[str]:
    resolved_repo = repo.resolve()
    if resolved_repo in seen_repositories:
        return []
    seen_repositories |= {resolved_repo}

    def display_path(relative_path: str) -> str:
        return f"{prefix}/{relative_path}" if prefix else relative_path

    hidden_paths: list[str] = []
    for entry in run_git(repo, "ls-files", "-v", "-z").stdout.split("\0"):
        if len(entry) < 3 or entry[1] != " ":
            continue
        tag = entry[0]
        relative_path = entry[2:]
        if tag.islower():
            hidden_paths.append(f"assume-unchanged={display_path(relative_path)}")
        elif tag == "S":
            candidate = repo / relative_path
            if candidate.exists() or candidate.is_symlink():
                hidden_paths.append(f"materialized skip-worktree={display_path(relative_path)}")
    for entry in run_git(repo, "ls-files", "--stage", "-z").stdout.split("\0"):
        metadata, separator, relative_path = entry.partition("\t")
        fields = metadata.split()
        if not separator or len(fields) != 3 or fields[0] != "160000" or fields[2] != "0":
            continue
        submodule_path = repo / relative_path
        if is_repository_root(submodule_path):
            hidden_paths.extend(
                hidden_index_paths(
                    submodule_path,
                    display_path(relative_path),
                    seen_repositories,
                )
            )
    return sorted(hidden_paths)


def validate(args: argparse.Namespace) -> tuple[dict[str, object], list[str]]:
    repo = args.repo.expanduser().resolve()
    failures: list[str] = []

    if not repo.is_dir():
        return {"repo": str(repo), "valid": False}, [f"Repository path does not exist: {repo}"]

    top_level = Path(run_git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if top_level != repo:
        failures.append(f"--repo must be the worktree root: expected {top_level}, got {repo}")

    status = run_git(
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignore-submodules=none",
    ).stdout
    if status:
        failures.append("Worktree is not clean.")
    hidden_paths = hidden_index_paths(repo)
    if hidden_paths:
        failures.append(f"Index flags can hide worktree changes: {hidden_paths}.")

    branch_result = run_git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    branch = branch_result.stdout.strip() if branch_result.returncode == 0 else None
    if branch is None:
        failures.append("HEAD is detached.")
    elif branch != args.expected_branch:
        failures.append(f"Current branch is {branch!r}, expected {args.expected_branch!r}.")

    base = run_git(repo, "rev-parse", f"{args.base}^{{commit}}").stdout.strip()
    head = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    parent_line = run_git(repo, "show", "-s", "--format=%P", "HEAD").stdout.strip()
    parents = parent_line.split() if parent_line else []
    if len(parents) != 1:
        failures.append(f"HEAD must have exactly one parent, found {len(parents)}.")
    elif parents[0] != base:
        failures.append(f"HEAD parent is {parents[0]}, expected base {base}.")

    ahead_text = run_git(repo, "rev-list", "--count", f"{base}..{head}").stdout.strip()
    ahead = int(ahead_text)
    if ahead != 1:
        failures.append(f"HEAD must be exactly one commit ahead of base, found {ahead} commits.")

    shipped_manifest: str | None = None
    shipped_paths: list[str] | None = None
    if args.shipped_path_manifest is not None:
        manifest_path = args.shipped_path_manifest.expanduser().resolve()
        expected_paths = load_shipped_paths(manifest_path)
        actual_paths = {
            path
            for path in run_git(
                repo,
                "diff",
                "--name-only",
                "--no-renames",
                "-z",
                f"{base}..{head}",
            ).stdout.split("\0")
            if path
        }
        missing_paths = sorted(expected_paths - actual_paths)
        unexpected_paths = sorted(actual_paths - expected_paths)
        if missing_paths or unexpected_paths:
            failures.append(
                "Committed paths do not match the shipped-path manifest: "
                f"missing={missing_paths}, unexpected={unexpected_paths}."
            )
        shipped_manifest = str(manifest_path)
        shipped_paths = sorted(expected_paths)

    subject = run_git(repo, "show", "-s", "--format=%s", "HEAD").stdout.strip()
    if not subject:
        failures.append("HEAD commit subject is empty.")

    trailer_values = run_git(
        repo,
        "show",
        "-s",
        "--format=%(trailers:key=Co-authored-by,valueonly,unfold,separator=%x00)",
        "HEAD",
    ).stdout.rstrip("\n")
    email_pattern = re.compile(r"^.+\s+<([^>\n]+)>$")
    trailer_emails = {
        match.group(1).strip().casefold()
        for value in trailer_values.split("\0")
        if (match := email_pattern.match(value)) is not None
    }
    for email in args.required_trailer_email:
        if email.strip().casefold() not in trailer_emails:
            failures.append(f"Missing required Co-authored-by trailer for {email}.")

    report: dict[str, object] = {
        "repo": str(repo),
        "base": base,
        "head": head,
        "branch": branch,
        "subject": subject,
        "ahead": ahead,
        "clean": not status and not hidden_paths,
        "coauthor_trailer_emails": sorted(trailer_emails),
        "shipped_path_manifest": shipped_manifest,
        "shipped_paths": shipped_paths,
        "valid": not failures,
    }
    return report, failures


def main() -> int:
    args = parse_args()
    try:
        report, failures = validate(args)
    except (GitCommandError, OSError, UnicodeError, ValueError) as exc:
        report = {"repo": str(args.repo.expanduser().resolve()), "valid": False}
        failures = [str(exc)]

    if args.json:
        print(json.dumps({**report, "failures": failures}, indent=2, sort_keys=True))
    else:
        status = "valid" if not failures else "invalid"
        print(f"Implementation handoff: {status}")
        for key in (
            "repo",
            "base",
            "head",
            "branch",
            "subject",
            "ahead",
            "clean",
            "shipped_path_manifest",
            "shipped_paths",
        ):
            if key in report:
                print(f"{key}: {report[key]}")
        for failure in failures:
            print(f"error: {failure}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
