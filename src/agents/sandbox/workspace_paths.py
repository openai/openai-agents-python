from __future__ import annotations

import posixpath
from pathlib import Path, PurePosixPath
from typing import Literal

from .errors import InvalidManifestPathError


class WorkspacePathPolicy:
    """Validate and format paths that are interpreted relative to a sandbox workspace root."""

    def __init__(self, *, root: str | Path) -> None:
        self._root = Path(root)

    def absolute_workspace_path(self, path: str | Path) -> Path:
        """Return an absolute workspace path without following symlinks.

        Examples with root `/workspace`:
        - `absolute_workspace_path("src/app.py")` returns `/workspace/src/app.py`.
        - `absolute_workspace_path("/workspace/src/app.py")` returns `/workspace/src/app.py`.
        - `absolute_workspace_path("/tmp/app.py")` raises `InvalidManifestPathError`.
        """

        normalized = self._absolute_workspace_posix_path(Path(path))
        return Path(str(normalized))

    def relative_path(self, path: str | Path) -> Path:
        """Return a path relative to the workspace root.

        Examples with root `/workspace`:
        - `relative_path("src/app.py")` returns `src/app.py`.
        - `relative_path("/workspace/src/app.py")` returns `src/app.py`.
        - `relative_path("/workspace")` returns `.`.
        """

        normalized = self._absolute_workspace_posix_path(Path(path))
        root = self._normalized_root()
        relative = normalized.relative_to(root)
        return Path(str(relative)) if relative.parts else Path(".")

    def normalize_path_for_host_io(self, path: str | Path) -> Path:
        """Return a resolved host path and reject symlink escapes from the workspace root.

        Examples with root `/tmp/workspace`:
        - `normalize_path_for_host_io("src/app.py")` returns the resolved host path for
          `/tmp/workspace/src/app.py`.
        - If `/tmp/workspace/link.txt` points to `/tmp/workspace/target.txt`,
          `normalize_path_for_host_io("link.txt")` returns `/tmp/workspace/target.txt`.
        - If `/tmp/workspace/link` points outside the workspace,
          `normalize_path_for_host_io("link/secret.txt")` raises `InvalidManifestPathError`.
        """

        original = Path(path)
        workspace_root = self._root.resolve(strict=False)
        if original.is_absolute():
            resolved = original.resolve(strict=False)
        else:
            absolute = self._absolute_workspace_posix_path(original)
            resolved = Path(str(absolute)).resolve(strict=False)
        try:
            resolved.relative_to(workspace_root)
        except ValueError as exc:
            raise self._invalid_path_error(original, cause=exc) from exc
        return resolved

    def _absolute_workspace_posix_path(self, path: Path) -> PurePosixPath:
        root = self._normalized_root()
        raw_candidate = path.as_posix() if path.is_absolute() else str(root / path.as_posix())
        normalized = PurePosixPath(posixpath.normpath(str(raw_candidate)))
        try:
            normalized.relative_to(root)
        except ValueError as exc:
            raise self._invalid_path_error(path, cause=exc) from exc
        return normalized

    def _normalized_root(self) -> PurePosixPath:
        return PurePosixPath(posixpath.normpath(self._root.as_posix()))

    def _invalid_path_error(
        self,
        path: Path,
        *,
        cause: BaseException | None = None,
    ) -> InvalidManifestPathError:
        reason: Literal["absolute", "escape_root"] = (
            "absolute" if path.is_absolute() else "escape_root"
        )
        return InvalidManifestPathError(rel=path, reason=reason, cause=cause)
