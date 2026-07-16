from __future__ import annotations

import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True)
class FakeResolveWorkspaceResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""


def resolve_fake_workspace_path(
    command: str | Sequence[str],
    *,
    symlinks: dict[str, str],
    home_dir: str,
) -> FakeResolveWorkspaceResult | None:
    tokens = shlex.split(command) if isinstance(command, str) else list(command)
    helper_index = next(
        (
            index
            for index, token in enumerate(tokens)
            if token.startswith("/tmp/openai-agents/bin/resolve-workspace-path-")
        ),
        None,
    )
    if helper_index is None or len(tokens) < helper_index + 4:
        return None

    root = _resolve_fake_path(tokens[helper_index + 1], symlinks=symlinks, home_dir=home_dir)
    candidate = _resolve_fake_path(tokens[helper_index + 2], symlinks=symlinks, home_dir=home_dir)
    for_write = tokens[helper_index + 3]
    grant_tokens = tokens[helper_index + 4 :]

    if _fake_path_is_under(candidate, root):
        return FakeResolveWorkspaceResult(exit_code=0, stdout=candidate.as_posix())

    best_grant: tuple[PurePosixPath, str, str] | None = None
    for index in range(0, len(grant_tokens), 2):
        grant_original = grant_tokens[index]
        read_only = grant_tokens[index + 1]
        grant_root = _resolve_fake_path(grant_original, symlinks=symlinks, home_dir=home_dir)
        if not _fake_path_is_under(candidate, grant_root):
            continue
        if best_grant is None or len(grant_root.parts) > len(best_grant[0].parts):
            best_grant = (grant_root, grant_original, read_only)

    if best_grant is not None:
        _grant_root, grant_original, read_only = best_grant
        if for_write == "1" and read_only == "1":
            return FakeResolveWorkspaceResult(
                exit_code=114,
                stderr=(
                    f"read-only extra path grant: {grant_original}\n"
                    f"resolved path: {candidate.as_posix()}\n"
                ),
            )
        return FakeResolveWorkspaceResult(exit_code=0, stdout=candidate.as_posix())

    return FakeResolveWorkspaceResult(
        exit_code=111,
        stderr=f"workspace escape: {candidate.as_posix()}\n",
    )


def _resolve_fake_path(
    raw_path: str,
    *,
    symlinks: dict[str, str],
    home_dir: str,
    depth: int = 0,
) -> PurePosixPath:
    if depth > 64:
        raise RuntimeError(f"symlink resolution depth exceeded: {raw_path}")

    path = PurePosixPath(raw_path)
    if not path.is_absolute():
        path = PurePosixPath(home_dir) / path

    parts = path.parts
    current = PurePosixPath("/")
    for index, part in enumerate(parts[1:], start=1):
        current = current / part
        target = symlinks.get(current.as_posix())
        if target is None:
            continue

        target_path = PurePosixPath(target)
        if not target_path.is_absolute():
            target_path = current.parent / target_path
        for remaining in parts[index + 1 :]:
            target_path /= remaining
        return _resolve_fake_path(
            target_path.as_posix(),
            symlinks=symlinks,
            home_dir=home_dir,
            depth=depth + 1,
        )

    return path


def _fake_path_is_under(path: PurePosixPath, root: PurePosixPath) -> bool:
    return path == root or root in path.parents
