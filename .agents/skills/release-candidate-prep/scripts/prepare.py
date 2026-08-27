#!/usr/bin/env python3
"""Preflight and materialize an isolated release candidate from exact origin/main."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from collections.abc import Sequence

ROOT = Path(__file__).resolve().parents[4]
VERSION_PATTERN = re.compile(r"\d+\.\d+(?:\.\d+)*(?:[A-Za-z0-9.-]+)?\Z")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
PROJECT_VERSION_PATTERN = re.compile(r'(?m)^version\s*=\s*"[^"]+"')
RELEASE_PATHS = frozenset(
    {
        "pyproject.toml",
        "tests/fixtures/released_api_contract.json",
        "uv.lock",
    }
)


class ReleasePreparationError(RuntimeError):
    """Report a safe, actionable release preparation failure."""


@dataclass(frozen=True)
class ReleasePreflight:
    """Describe an isolated branch-free release readiness input."""

    base_commit: str
    branch: str
    source_commit: str
    version: str
    worktree: Path


@dataclass(frozen=True)
class PreparedCandidate:
    """Describe the successfully prepared isolated candidate."""

    base_commit: str
    branch: str
    changed_paths: tuple[str, ...]
    source_commit: str
    version: str
    worktree: Path


def _release_environment() -> dict[str, str]:
    env = os.environ.copy()
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "OPENAI_API_KEY"):
        env.pop(name, None)
    env["UV_DEFAULT_INDEX"] = "https://pypi.org/simple"
    return env


def _command_text(args: Sequence[str]) -> str:
    return shlex.join(str(arg) for arg in args)


def run_command(
    repo: Path,
    args: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    announce: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one command and preserve useful output for failures."""

    if announce:
        print(f"+ {_command_text(args)}", flush=True)
    effective_env = _release_environment() if env is None else env.copy()
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "OPENAI_API_KEY"):
        effective_env.pop(name, None)
    effective_env["UV_DEFAULT_INDEX"] = "https://pypi.org/simple"
    result = subprocess.run(
        [str(arg) for arg in args],
        cwd=repo,
        env=effective_env,
        check=False,
        capture_output=True,
        text=True,
    )
    if announce:
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown command failure"
        raise ReleasePreparationError(f"{_command_text(args)} failed: {detail}")
    return result


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a Git inspection command."""

    return run_command(repo, ["git", *args], check=check)


def validate_version(version: str) -> str:
    """Validate the release version accepted by the existing contract updater."""

    if version.startswith("v") or ".." in version or VERSION_PATTERN.fullmatch(version) is None:
        raise ReleasePreparationError(
            "Version must be semver-like without a leading v, for example 0.20.1 or 0.21.0-rc1."
        )
    return version


def validate_commit(commit: str) -> str:
    """Validate an exact lowercase Git commit identifier."""

    if COMMIT_PATTERN.fullmatch(commit) is None:
        raise ReleasePreparationError(
            "Expected base must be a full 40-character lowercase Git commit identifier."
        )
    return commit


def project_version(repo: Path) -> str:
    """Read the project version from pyproject.toml."""

    data = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    version = data.get("project", {}).get("version")
    if not isinstance(version, str):
        raise ReleasePreparationError("pyproject.toml is missing project.version.")
    return version


def project_version_at(repo: Path, commit: str) -> str:
    """Read the project version from one exact commit without changing a checkout."""

    text = git(repo, "show", f"{commit}:pyproject.toml").stdout
    data = tomllib.loads(text)
    version = data.get("project", {}).get("version")
    if not isinstance(version, str):
        raise ReleasePreparationError("pyproject.toml is missing project.version.")
    return version


def replace_project_version_text(text: str, version: str) -> str:
    """Replace the repository's single project version declaration."""

    updated, count = PROJECT_VERSION_PATTERN.subn(f'version = "{version}"', text)
    if count != 1:
        raise ReleasePreparationError(
            f"Expected exactly one version declaration in pyproject.toml, found {count}."
        )
    if updated == text:
        raise ReleasePreparationError(f"pyproject.toml already declares version {version}.")
    return updated


def replace_project_version(repo: Path, version: str) -> None:
    """Update pyproject.toml while preserving all unrelated text."""

    path = repo / "pyproject.toml"
    text = path.read_text(encoding="utf-8")
    path.write_text(replace_project_version_text(text, version), encoding="utf-8")


def _current_branch(repo: Path) -> str:
    result = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if result.returncode != 0:
        raise ReleasePreparationError("Release preparation requires a named main branch.")
    return result.stdout.strip()


def _status(repo: Path) -> str:
    return git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout


def _require_repository_root(repo: Path) -> None:
    top_level = Path(git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if top_level != repo.resolve():
        raise ReleasePreparationError(
            f"Run release preparation from the repository root {top_level}, not {repo.resolve()}."
        )


def _require_clean_main(repo: Path) -> None:
    branch = _current_branch(repo)
    if branch != "main":
        raise ReleasePreparationError(
            f"Release preparation requires branch 'main', found {branch!r}."
        )
    if _status(repo):
        raise ReleasePreparationError("Release preparation requires a clean working tree.")


def _require_source_head(repo: Path, expected_source_head: str) -> None:
    """Require the user's source checkout to remain at its preflight commit."""

    source_head = git(repo, "rev-parse", "HEAD").stdout.strip()
    if source_head != expected_source_head:
        raise ReleasePreparationError(
            f"Source checkout HEAD changed from {expected_source_head} to {source_head}; "
            "leave both checkouts intact and restart release preparation."
        )


def _registered_worktrees(repo: Path) -> set[Path]:
    """Return canonical paths registered in the repository worktree inventory."""

    paths: set[Path] = set()
    for line in git(repo, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            paths.add(Path(line.removeprefix("worktree ")).resolve())
    return paths


def _choose_worktree_path(repo: Path, worktree_root: Path, version: str) -> Path:
    """Choose a unique release worktree path without reusing or deleting collisions."""

    worktree_root = worktree_root.expanduser().resolve()
    if worktree_root == repo or worktree_root.is_relative_to(repo):
        raise ReleasePreparationError(
            "The release worktree root must be outside the source checkout."
        )

    registered = _registered_worktrees(repo)
    stem = f"{repo.name}-release-v{version}"
    suffix = 1
    while True:
        name = stem if suffix == 1 else f"{stem}-{suffix}"
        candidate = worktree_root / name
        if not candidate.exists() and candidate.resolve() not in registered:
            return candidate
        suffix += 1


def _require_registered_detached_worktree(
    source_repo: Path,
    worktree: Path,
    expected_base: str,
) -> None:
    """Require a clean registered detached worktree at the reviewed base."""

    worktree = worktree.expanduser().resolve()
    if worktree not in _registered_worktrees(source_repo):
        raise ReleasePreparationError(
            f"Release worktree {worktree} is not registered for this repository."
        )
    if not worktree.is_dir():
        raise ReleasePreparationError(f"Release worktree path does not exist: {worktree}.")
    _require_repository_root(worktree)
    branch = git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if branch.returncode == 0:
        raise ReleasePreparationError(
            f"Release worktree must remain detached before materialization, found "
            f"{branch.stdout.strip()!r}."
        )
    if branch.returncode != 1:
        raise ReleasePreparationError("Unable to inspect the release worktree branch state.")
    head = git(worktree, "rev-parse", "HEAD").stdout.strip()
    if head != expected_base:
        raise ReleasePreparationError(
            f"Release worktree HEAD is {head}, expected reviewed base {expected_base}."
        )
    if _status(worktree):
        raise ReleasePreparationError("Release worktree must be clean before materialization.")


def _worktrees_using_branch(repo: Path, branch: str) -> tuple[Path, ...]:
    """Return registered worktrees that currently check out one local branch."""

    matches: list[Path] = []
    worktree: Path | None = None
    for line in [*git(repo, "worktree", "list", "--porcelain").stdout.splitlines(), ""]:
        if line.startswith("worktree "):
            worktree = Path(line.removeprefix("worktree ")).resolve()
        elif line == f"branch refs/heads/{branch}" and worktree is not None:
            matches.append(worktree)
        elif not line:
            worktree = None
    return tuple(matches)


def _require_branch_replaceable(repo: Path, branch: str) -> None:
    """Require an existing release branch to be safe to replace locally."""

    local = git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    if local.returncode not in (0, 1):
        raise ReleasePreparationError(f"Unable to inspect local branch {branch!r}.")
    if local.returncode == 0:
        worktrees = _worktrees_using_branch(repo, branch)
        if worktrees:
            locations = ", ".join(str(path) for path in worktrees)
            raise ReleasePreparationError(
                f"Local branch {branch!r} is checked out in {locations}; switch that worktree "
                "away from the branch before replacing the release candidate."
            )

    remote = git(repo, "ls-remote", "--exit-code", "--heads", "origin", branch, check=False)
    if remote.returncode not in (0, 2):
        detail = remote.stderr.strip() or remote.stdout.strip() or "unknown remote error"
        raise ReleasePreparationError(f"Unable to inspect remote branch {branch!r}: {detail}")


def _changed_paths(repo: Path) -> set[str]:
    changed: set[str] = set()
    for args in (
        ("diff", "--name-only"),
        ("diff", "--name-only", "--cached"),
        ("ls-files", "--others", "--exclude-standard"),
    ):
        changed.update(line for line in git(repo, *args).stdout.splitlines() if line)
    return changed


def _locked_project_version(repo: Path) -> str:
    data = tomllib.loads((repo / "uv.lock").read_text(encoding="utf-8"))
    packages = data.get("package", [])
    matches = [
        package
        for package in packages
        if package.get("name") == "openai-agents" and package.get("source") == {"editable": "."}
    ]
    if len(matches) != 1:
        raise ReleasePreparationError(
            "uv.lock must contain exactly one editable openai-agents package with a version."
        )
    locked_version = matches[0].get("version")
    if not isinstance(locked_version, str):
        raise ReleasePreparationError(
            "uv.lock must contain exactly one editable openai-agents package with a version."
        )
    return locked_version


def _validate_prepared_files(repo: Path, version: str, base_commit: str) -> tuple[str, ...]:
    changed = _changed_paths(repo)
    if changed != RELEASE_PATHS:
        missing = sorted(RELEASE_PATHS - changed)
        unexpected = sorted(changed - RELEASE_PATHS)
        raise ReleasePreparationError(
            "Prepared release paths do not match the required manifest; "
            f"missing={missing!r}, unexpected={unexpected!r}."
        )
    if project_version(repo) != version:
        raise ReleasePreparationError("pyproject.toml does not contain the requested version.")
    if _locked_project_version(repo) != version:
        raise ReleasePreparationError("uv.lock does not contain the requested project version.")

    contract = json.loads(
        (repo / "tests/fixtures/released_api_contract.json").read_text(encoding="utf-8")
    )
    if contract.get("baseline") != f"v{version}":
        raise ReleasePreparationError(
            "The released API contract baseline does not match the version."
        )
    if contract.get("baseline_commit") != base_commit:
        raise ReleasePreparationError(
            "The released API contract baseline_commit does not match "
            "the origin/main source commit."
        )
    if git(repo, "diff", "--cached", "--quiet", check=False).returncode != 0:
        raise ReleasePreparationError("The helper must leave all release changes unstaged.")
    return tuple(sorted(changed))


def preflight(repo: Path, version: str, worktree_root: Path) -> ReleasePreflight:
    """Refresh exact main and create an isolated branch-free readiness checkout."""

    repo = repo.resolve()
    version = validate_version(version)
    _require_repository_root(repo)
    _require_clean_main(repo)
    source_commit = git(repo, "rev-parse", "HEAD").stdout.strip()
    branch = f"release/v{version}"
    _require_branch_replaceable(repo, branch)
    env = _release_environment()
    run_command(
        repo,
        [
            "git",
            "fetch",
            "origin",
            "refs/heads/main:refs/remotes/origin/main",
            "--prune",
        ],
        env=env,
        announce=True,
    )
    base_commit = git(repo, "rev-parse", "origin/main").stdout.strip()
    if project_version_at(repo, base_commit) == version:
        raise ReleasePreparationError(f"Refreshed origin/main already declares version {version}.")

    _require_branch_replaceable(repo, branch)
    if _status(repo):
        raise ReleasePreparationError(
            "Release preflight must leave the source main working tree clean."
        )
    worktree = _choose_worktree_path(repo, worktree_root, version)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    run_command(
        repo,
        ["git", "worktree", "add", "--detach", str(worktree), base_commit],
        env=env,
        announce=True,
    )
    _require_registered_detached_worktree(repo, worktree, base_commit)
    _require_clean_main(repo)
    _require_source_head(repo, source_commit)
    return ReleasePreflight(
        base_commit=base_commit,
        branch=branch,
        source_commit=source_commit,
        version=version,
        worktree=worktree,
    )


def materialize(
    repo: Path,
    version: str,
    expected_base: str,
    expected_source_head: str,
    worktree: Path,
) -> PreparedCandidate:
    """Create the three-file candidate in the reviewed isolated worktree."""

    expected_base = validate_commit(expected_base)
    expected_source_head = validate_commit(expected_source_head)
    repo = repo.resolve()
    version = validate_version(version)
    worktree = worktree.expanduser().resolve()
    _require_repository_root(repo)
    _require_clean_main(repo)
    _require_source_head(repo, expected_source_head)
    _require_registered_detached_worktree(repo, worktree, expected_base)

    env = _release_environment()
    branch = f"release/v{version}"
    _require_branch_replaceable(repo, branch)
    run_command(
        repo,
        [
            "git",
            "fetch",
            "origin",
            "refs/heads/main:refs/remotes/origin/main",
            "--prune",
        ],
        env=env,
        announce=True,
    )
    base_commit = git(repo, "rev-parse", "origin/main").stdout.strip()
    if base_commit != expected_base:
        raise ReleasePreparationError(
            f"Preflight reviewed {expected_base}, but refreshed origin/main is {base_commit}; "
            "leave the detached worktree intact and rerun preflight plus both readiness gates."
        )
    _require_clean_main(repo)
    _require_source_head(repo, expected_source_head)
    _require_branch_replaceable(repo, branch)
    _require_registered_detached_worktree(repo, worktree, expected_base)
    if project_version(worktree) == version:
        raise ReleasePreparationError(f"Project version is already {version}.")

    replace_project_version(worktree, version)
    run_command(worktree, ["make", "sync"], env=env, announce=True)
    run_command(
        worktree,
        ["make", "update-released-api-contract", f"VERSION={version}"],
        env=env,
        announce=True,
    )
    run_command(
        worktree,
        ["make", "check-released-api-contract", f"VERSION={version}"],
        env=env,
        announce=True,
    )
    changed_paths = _validate_prepared_files(worktree, version, base_commit)
    _require_clean_main(repo)
    _require_source_head(repo, expected_source_head)
    _require_branch_replaceable(repo, branch)
    run_command(
        worktree,
        ["git", "switch", "--no-track", "-C", branch, expected_base],
        env=env,
        announce=True,
    )
    return PreparedCandidate(
        base_commit=base_commit,
        branch=branch,
        changed_paths=changed_paths,
        source_commit=expected_source_head,
        version=version,
        worktree=worktree,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preflight or materialize an isolated release candidate from exact origin/main."
    )
    subparsers = parser.add_subparsers(dest="phase", required=True)
    preflight_parser = subparsers.add_parser(
        "preflight",
        help="Refresh exact main and validate inputs without creating a release branch.",
    )
    preflight_parser.add_argument(
        "--version",
        required=True,
        help="Release version without a leading v, for example 0.20.1.",
    )
    preflight_parser.add_argument(
        "--worktree-root",
        type=Path,
        default=Path(os.environ.get("CODEX_WORKTREE_ROOT", Path.home() / ".codex/worktrees")),
        help="Directory under which to create a unique detached release worktree.",
    )
    materialize_parser = subparsers.add_parser(
        "materialize",
        help="Create an uncommitted candidate from the reviewed preflight commit.",
    )
    materialize_parser.add_argument(
        "--version",
        required=True,
        help="Release version without a leading v, for example 0.20.1.",
    )
    materialize_parser.add_argument(
        "--expected-base",
        required=True,
        help="Exact 40-character origin/main commit approved by release preflight.",
    )
    materialize_parser.add_argument(
        "--expected-source-head",
        required=True,
        help="Exact source-checkout HEAD recorded by release preflight.",
    )
    materialize_parser.add_argument(
        "--worktree",
        type=Path,
        required=True,
        help="Detached worktree created by the matching preflight.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.phase == "preflight":
            release_input = preflight(ROOT, args.version, args.worktree_root)
        else:
            candidate = materialize(
                ROOT,
                args.version,
                args.expected_base,
                args.expected_source_head,
                args.worktree,
            )
    except (OSError, ReleasePreparationError, tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        print(f"Release preparation failed: {exc}", file=sys.stderr)
        return 1

    if args.phase == "preflight":
        print("Release preflight passed without changing the source main checkout.")
        print(f"Base commit: {release_input.base_commit}")
        print(f"Source commit: {release_input.source_commit}")
        print(f"Planned branch: {release_input.branch}")
        print(f"Version: {release_input.version}")
        print(f"Worktree: {release_input.worktree}")
        print("Run both readiness gates from this detached worktree against the base commit.")
        return 0

    print("Release candidate prepared in its dedicated worktree and left uncommitted.")
    print(f"Base commit: {candidate.base_commit}")
    print(f"Source commit: {candidate.source_commit}")
    print(f"Branch: {candidate.branch}")
    print(f"Version: {candidate.version}")
    print(f"Worktree: {candidate.worktree}")
    print("Changed paths:")
    for path in candidate.changed_paths:
        print(f"- {path}")
    print("Review the diff before staging the three release-owned files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
