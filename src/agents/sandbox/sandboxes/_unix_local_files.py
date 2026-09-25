"""Descriptor-relative host I/O for the UnixLocal workspace boundary."""

from __future__ import annotations

import sys

if sys.platform == "win32":  # pragma: no cover
    raise ImportError("UnixLocal file operations are not supported on Windows.")

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..manifest import Manifest
from ..workspace_paths import WorkspacePathPolicy
from ._unix_local_file_ops import _FileOps


class _UnixLocalFiles(_FileOps):
    def __init__(self, manifest: Manifest) -> None:
        self._roots: dict[str, Path] = {}
        self.configure(manifest)

    def configure(self, manifest: Manifest) -> None:
        # Trusted configuration can change grants, but unchanged aliases must stay pinned even
        # when an occupant replaces their filesystem entries between operations.
        paths = (manifest.root, *(grant.path for grant in manifest.extra_path_grants))
        self._roots = {
            path: self._roots[path] if path in self._roots else Path(path).resolve()
            for path in paths
        }
        self._policy = WorkspacePathPolicy(
            root=self._roots[manifest.root],
            extra_path_grants=tuple(
                grant.model_copy(update={"path": str(self._roots[grant.path])})
                for grant in manifest.extra_path_grants
            ),
        )

    def authorize(self, path: Path, *, for_write: bool = False) -> Path:
        # Reauthorize resolved paths against captured roots without following new symlinks.
        return self._policy.normalize_path(path, for_write=for_write)

    @contextmanager
    def parent(
        self, path: Path, *, for_write: bool = False, create_parents: bool = False
    ) -> Iterator[tuple[int, str]]:
        path = self.authorize(path, for_write=for_write)
        with super().parent(path, for_write=for_write, create_parents=create_parents) as parent:
            yield parent
