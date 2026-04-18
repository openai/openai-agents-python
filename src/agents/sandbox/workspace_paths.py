from __future__ import annotations

import posixpath
from pathlib import Path, PurePath, PurePosixPath
from typing import Literal

from pydantic import BaseModel, field_validator

from .errors import InvalidManifestPathError, WorkspaceArchiveWriteError

_ROOT_PATH_GRANT_ERROR = "sandbox path grant path must not be filesystem root"
_RESOLVED_ROOT_PATH_GRANT_ERROR = "sandbox path grant path must not resolve to filesystem root"


def _is_filesystem_root(path: PurePath) -> bool:
    return path.is_absolute() and path == path.parent


def _raise_if_filesystem_root(path: PurePath, *, resolved: bool = False) -> None:
    if not _is_filesystem_root(path):
        return
    if resolved:
        raise ValueError(_RESOLVED_ROOT_PATH_GRANT_ERROR)
    raise ValueError(_ROOT_PATH_GRANT_ERROR)


class SandboxPathGrant(BaseModel):
    """Extra absolute path access outside the sandbox workspace."""

    path: str
    read_only: bool = False
    description: str | None = None

    @field_validator("path", mode="before")
    @classmethod
    def _coerce_path(cls, value: object) -> str:
        if isinstance(value, Path):
            return value.as_posix()
        if isinstance(value, str):
            return value
        raise ValueError("sandbox path grant path must be a string or Path")

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        path = PurePosixPath(posixpath.normpath(value))
        if not path.is_absolute():
            raise ValueError("sandbox path grant path must be absolute")
        _raise_if_filesystem_root(path)
        return path.as_posix()


class WorkspacePathPolicy:
    """Validate and format paths that are interpreted relative to a sandbox workspace root."""

    def __init__(
        self,
        *,
        root: str | Path,
        extra_path_grants: tuple[SandboxPathGrant, ...] = (),
    ) -> None:
        self._root = Path(root)
        self._extra_path_grants = extra_path_grants

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

    def normalize_path(
        self,
        path: str | Path,
        *,
        for_write: bool = False,
        resolve_symlinks: bool = False,
    ) -> Path:
        """Return a validated absolute path under the workspace or an extra grant.

        `resolve_symlinks` follows symlinks on the host filesystem. Use it only when the sandbox
        workspace is a real local host directory, such as UnixLocalSandboxSession.
        """

        original = Path(path)
        if resolve_symlinks:
            result, grant = self._resolved_host_path_and_grant(original)
        else:
            result, grant = self._sandbox_path_and_grant(original)
        if for_write:
            self._raise_if_read_only_grant(result, grant)
        return result

    def _resolved_host_path_and_grant(
        self,
        original: Path,
    ) -> tuple[Path, SandboxPathGrant | None]:
        workspace_root = self._root.resolve(strict=False)
        if original.is_absolute():
            resolved = original.resolve(strict=False)
        else:
            absolute = self._absolute_workspace_posix_path(original)
            resolved = Path(str(absolute)).resolve(strict=False)

        if self._is_under(resolved, workspace_root):
            return resolved, None
        grant = self._matching_grant(resolved, resolve_roots=True)
        if grant is None:
            raise self._invalid_path_error(original)
        return resolved, grant

    def _sandbox_path_and_grant(
        self,
        original: Path,
    ) -> tuple[Path, SandboxPathGrant | None]:
        normalized = (
            self._absolute_posix_path(original)
            if original.is_absolute()
            else self._absolute_workspace_posix_path(original)
        )
        if self._is_under(normalized, self._normalized_root()):
            return Path(str(normalized)), None
        grant = self._matching_grant(normalized)
        if original.is_absolute() and grant is not None:
            return Path(str(normalized)), grant
        raise self._invalid_path_error(original)

    def _raise_if_read_only_grant(
        self,
        path: Path,
        grant: SandboxPathGrant | None,
    ) -> None:
        if grant is None or not grant.read_only:
            return
        raise WorkspaceArchiveWriteError(
            path=path,
            context={
                "reason": "read_only_extra_path_grant",
                "grant_path": grant.path,
            },
        )

    def extra_path_grant_rules(self) -> tuple[tuple[Path, bool], ...]:
        """Return normalized extra grant roots and access modes for remote realpath checks."""

        rules: list[tuple[Path, bool]] = []
        for grant in self._extra_path_grants:
            root = Path(grant.path)
            _raise_if_filesystem_root(root)
            rules.append((root, grant.read_only))
        return tuple(rules)

    def _absolute_workspace_posix_path(self, path: Path) -> PurePosixPath:
        normalized = self._absolute_posix_path(path)
        root = self._normalized_root()
        try:
            normalized.relative_to(root)
        except ValueError as exc:
            raise self._invalid_path_error(path, cause=exc) from exc
        return normalized

    def _absolute_posix_path(self, path: Path) -> PurePosixPath:
        root = self._normalized_root()
        raw_candidate = path.as_posix() if path.is_absolute() else str(root / path.as_posix())
        return PurePosixPath(posixpath.normpath(str(raw_candidate)))

    def _normalized_root(self) -> PurePosixPath:
        return PurePosixPath(posixpath.normpath(self._root.as_posix()))

    def _matching_grant(
        self,
        path: PurePath,
        *,
        resolve_roots: bool = False,
    ) -> SandboxPathGrant | None:
        matches: list[tuple[SandboxPathGrant, PurePath]] = []
        for grant in self._extra_path_grants:
            grant_root: PurePath = (
                Path(grant.path).resolve(strict=False)
                if resolve_roots
                else PurePosixPath(grant.path)
            )
            _raise_if_filesystem_root(grant_root, resolved=resolve_roots)
            if self._is_under(path, grant_root):
                matches.append((grant, grant_root))
        if not matches:
            return None
        return max(matches, key=lambda item: len(item[1].parts))[0]

    @staticmethod
    def _is_under(path: PurePath, root: PurePath) -> bool:
        return path == root or root in path.parents

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
