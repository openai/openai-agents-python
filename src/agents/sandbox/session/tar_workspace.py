from __future__ import annotations

import shlex
from collections.abc import Iterable
from pathlib import Path

__all__ = ["shell_tar_exclude_args"]


def shell_tar_exclude_args(skip_relpaths: Iterable[Path]) -> list[str]:
    excludes: list[str] = []
    for rel in sorted(skip_relpaths, key=lambda p: p.as_posix()):
        rel_posix = rel.as_posix().lstrip("/")
        if not rel_posix or rel_posix in {".", "/"}:
            continue
        excludes.append(f"--exclude={shlex.quote(rel_posix)}")
        excludes.append(f"--exclude={shlex.quote(f'./{rel_posix}')}")
    return excludes
