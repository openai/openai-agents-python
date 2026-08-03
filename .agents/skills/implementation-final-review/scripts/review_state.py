#!/usr/bin/env python3
"""Print deterministic content and repository fingerprints for a review state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(("git", "-C", os.fspath(repo), *args), stderr=subprocess.PIPE)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_pathspecs(pathspecs: tuple[str, ...]) -> tuple[str, ...]:
    canonical: list[str] = []
    seen: set[str] = set()
    for pathspec in pathspecs:
        if not pathspec:
            raise ValueError("Pathspecs must not be empty.")
        if "\0" in pathspec:
            raise ValueError("Pathspecs must not contain NUL bytes.")
        if pathspec not in seen:
            canonical.append(pathspec)
            seen.add(pathspec)
    return tuple(canonical)


def _load_pathspec_file(path: Path) -> tuple[str, ...]:
    try:
        values = [line for line in path.read_text().splitlines() if line]
    except (OSError, UnicodeError) as error:
        raise ValueError(f"Cannot read pathspec file {path}: {error}") from error
    return _canonical_pathspecs(tuple(values))


def _workspace_entry(repo: Path, relative_path: str) -> dict[str, object]:
    path = repo / relative_path
    if path.is_symlink():
        content = b"symlink\0" + os.fsencode(os.readlink(path))
        return {
            "path": relative_path,
            "kind": "symlink",
            "sha256": _digest(content),
        }
    if path.is_file():
        content = b"file\0" + path.read_bytes()
        return {
            "path": relative_path,
            "kind": "file",
            "executable": bool(path.stat().st_mode & 0o111),
            "sha256": _digest(content),
        }
    if path.is_dir():
        try:
            submodule_head = _git(path, "rev-parse", "HEAD^{commit}").decode().strip()
            submodule_status = _git(path, "status", "--porcelain=v1", "-z")
        except (subprocess.CalledProcessError, FileNotFoundError):
            return {"path": relative_path, "kind": "directory"}
        return {
            "path": relative_path,
            "kind": "gitlink",
            "head": submodule_head,
            "status_sha256": _digest(submodule_status),
        }
    return {"path": relative_path, "kind": "missing"}


def _workspace_entries(
    repo: Path, base: str, pathspecs: tuple[str, ...]
) -> list[dict[str, object]]:
    tracked_paths = _git(
        repo,
        "diff",
        "--name-only",
        "--no-renames",
        "-z",
        base,
        "--",
        *pathspecs,
    )
    untracked_paths = _git(
        repo,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        *pathspecs,
    )
    paths = {
        os.fsdecode(raw_path)
        for raw_path in (*tracked_paths.split(b"\0"), *untracked_paths.split(b"\0"))
        if raw_path
    }
    return [_workspace_entry(repo, relative_path) for relative_path in sorted(paths)]


def _content_fingerprint(base: str, workspace: list[dict[str, object]]) -> str:
    canonical = json.dumps(
        {"base": base, "workspace": workspace},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _digest(canonical.encode())


def review_state(
    repo: Path,
    base: str,
    pathspecs: tuple[str, ...] = (),
    components: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, object]:
    repo = repo.resolve()
    pathspecs = _canonical_pathspecs(pathspecs)
    if components and not pathspecs:
        pathspecs = _canonical_pathspecs(
            tuple(
                pathspec
                for component_pathspecs in components.values()
                for pathspec in component_pathspecs
            )
        )
    resolved_base = _git(repo, "rev-parse", f"{base}^{{commit}}").decode().strip()
    head = _git(repo, "rev-parse", "HEAD^{commit}").decode().strip()
    try:
        _git(repo, "merge-base", "--is-ancestor", resolved_base, head)
    except subprocess.CalledProcessError as error:
        raise ValueError("Base must be an ancestor of HEAD.") from error
    tracked_diff = _git(
        repo,
        "diff",
        "--binary",
        "--full-index",
        resolved_base,
        "--",
        *pathspecs,
    )
    status = _git(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        *pathspecs,
    )
    workspace = _workspace_entries(repo, resolved_base, pathspecs)

    content_fingerprint = _content_fingerprint(resolved_base, workspace)
    component_states: dict[str, dict[str, object]] = {}
    component_owners: dict[str, list[str]] = {}
    for name, component_pathspecs in sorted((components or {}).items()):
        canonical_component_pathspecs = _canonical_pathspecs(component_pathspecs)
        if not canonical_component_pathspecs:
            raise ValueError(f"Component manifest is empty: {name}")
        component_workspace = _workspace_entries(repo, resolved_base, canonical_component_pathspecs)
        for entry in component_workspace:
            component_owners.setdefault(str(entry["path"]), []).append(name)
        component_states[name] = {
            "content_fingerprint": _content_fingerprint(resolved_base, component_workspace),
            "pathspecs": list(canonical_component_pathspecs),
            "workspace": component_workspace,
        }
    if component_states:
        combined_paths = {str(entry["path"]) for entry in workspace}
        component_paths = set(component_owners)
        missing_paths = sorted(combined_paths - component_paths)
        extra_paths = sorted(component_paths - combined_paths)
        overlapping_paths = {
            path: owners for path, owners in component_owners.items() if len(owners) > 1
        }
        if missing_paths or extra_paths or overlapping_paths:
            raise ValueError(
                "Component manifests must partition the combined review content exactly: "
                f"missing={missing_paths}, extra={extra_paths}, "
                f"overlapping={overlapping_paths}"
            )

    repository_state = {
        "content_fingerprint": content_fingerprint,
        "head": head,
        "status_sha256": _digest(status),
        "tracked_diff_sha256": _digest(tracked_diff),
    }
    repository_canonical = json.dumps(
        repository_state, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    repository_fingerprint = _digest(repository_canonical.encode())
    return {
        "fingerprint": content_fingerprint,
        "content_fingerprint": content_fingerprint,
        "repository_fingerprint": repository_fingerprint,
        "base": resolved_base,
        "pathspecs": list(pathspecs),
        "workspace": workspace,
        "components": component_states,
        **repository_state,
    }


def _parse_component_files(values: list[str]) -> dict[str, tuple[str, ...]]:
    components: dict[str, tuple[str, ...]] = {}
    for value in values:
        name, separator, raw_path = value.partition("=")
        if not separator or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) or not raw_path:
            raise ValueError(
                "Component pathspec files must use lowercase NAME=FILE with a nonempty file."
            )
        if name in components:
            raise ValueError(f"Duplicate component name: {name}")
        components[name] = _load_pathspec_file(Path(raw_path))
    return components


def _component(value: str) -> tuple[str, str]:
    name, separator, pathspec = value.partition("=")
    if not separator or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) or not pathspec:
        raise argparse.ArgumentTypeError("component must use lowercase NAME=PATHSPEC")
    if "\0" in pathspec:
        raise argparse.ArgumentTypeError("component pathspec must not contain NUL bytes")
    return name, pathspec


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, help="Resolved merge-base commit or revision.")
    parser.add_argument(
        "--pathspec",
        action="append",
        default=[],
        help="Task-owned Git pathspec. Repeat to scope the review; omit to include all changes.",
    )
    parser.add_argument(
        "--pathspec-file",
        action="append",
        default=[],
        type=Path,
        help="File containing canonical task-owned pathspecs, one per line.",
    )
    parser.add_argument(
        "--component-pathspec-file",
        action="append",
        default=[],
        metavar="NAME=FILE",
        help="Named component manifest. Repeat for runtime, tests-examples, or metadata.",
    )
    parser.add_argument(
        "--component",
        action="append",
        default=[],
        type=_component,
        metavar="NAME=PATHSPEC",
        help="Named component pathspec. Repeat a name to group paths into one fingerprint.",
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="Repository worktree path.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print the JSON output.")
    args = parser.parse_args()
    try:
        loaded_pathspec_files = [_load_pathspec_file(path) for path in args.pathspec_file]
        if any(not pathspecs for pathspecs in loaded_pathspec_files):
            raise ValueError("A supplied pathspec file must contain at least one pathspec.")
        file_pathspecs = tuple(
            pathspec for pathspecs in loaded_pathspec_files for pathspec in pathspecs
        )
        pathspecs = _canonical_pathspecs((*args.pathspec, *file_pathspecs))
        component_files = _parse_component_files(args.component_pathspec_file)
        component_values: dict[str, list[str]] = {
            name: list(component_pathspecs) for name, component_pathspecs in component_files.items()
        }
        for name, pathspec in args.component:
            component_values.setdefault(name, []).append(pathspec)
        components = {
            name: _canonical_pathspecs(tuple(component_pathspecs))
            for name, component_pathspecs in component_values.items()
        }
        state = review_state(args.repo, args.base, pathspecs, components)
    except ValueError as error:
        parser.error(str(error))
    except subprocess.CalledProcessError as error:
        parser.error(f"Git command failed with exit status {error.returncode}.")
    except (OSError, UnicodeError) as error:
        parser.error(f"Cannot inspect repository state: {error}")
    print(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
