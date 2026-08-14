from __future__ import annotations

import os
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Literal, cast

from pydantic import BaseModel, Field, field_validator, model_validator

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


def coerce_posix_path(path: str | PurePath) -> PurePosixPath:
    """Return a POSIX-flavored path for sandbox filesystem paths."""

    if isinstance(path, PurePath):
        path = path.as_posix()
    else:
        path = path.replace("\\", "/")
    return PurePosixPath(path)


def windows_absolute_path(path: str | PurePath) -> PureWindowsPath | None:
    """Return a Windows absolute path when the input uses Windows absolute syntax."""

    if isinstance(path, PureWindowsPath):
        windows_path = path
    else:
        windows_path = PureWindowsPath(path.as_posix() if isinstance(path, PurePath) else path)
    if windows_path.is_absolute() and not PurePosixPath(windows_path.as_posix()).is_absolute():
        return windows_path
    return None


def posix_path_as_path(path: PurePosixPath) -> Path:
    """Return a POSIX path through the public Path-typed sandbox API surface."""

    return Path(path.as_posix())


def posix_path_for_error(path: str | PurePath) -> Path:
    """Return a POSIX path object for sandbox error text and context."""

    return cast(Path, coerce_posix_path(path))


def sandbox_path_str(path: str | PurePath) -> str:
    """Return a POSIX string for a sandbox filesystem path."""

    return coerce_posix_path(path).as_posix()


def normalize_sandbox_cwd(cwd: str | PurePath) -> PurePosixPath:
    """Validate and normalize a run working directory relative to the workspace root."""

    if isinstance(cwd, PurePath):
        raw_cwd = cwd.as_posix()
    elif isinstance(cwd, str):
        if "\\" in cwd:
            raise ValueError("sandbox.cwd must use POSIX path separators")
        raw_cwd = cwd
    else:
        raise ValueError("sandbox.cwd must be a string or Path")

    if not raw_cwd.strip():
        raise ValueError("sandbox.cwd must be non-empty")
    if windows_absolute_path(cwd) is not None:
        raise ValueError("sandbox.cwd must be workspace-relative")

    posix_cwd = PurePosixPath(raw_cwd)
    if posix_cwd.is_absolute():
        raise ValueError("sandbox.cwd must be workspace-relative")
    if ".." in posix_cwd.parts:
        raise ValueError("sandbox.cwd must not contain parent segments")

    return PurePosixPath(posixpath.normpath(posix_cwd.as_posix()))


def _is_absolute_sandbox_path(path: str | PurePath) -> bool:
    if windows_absolute_path(path) is not None:
        return True
    raw_path = path.as_posix() if isinstance(path, PurePath) else path
    return PurePosixPath(raw_path).is_absolute()


@dataclass(frozen=True)
class SandboxWorkspaceScope:
    """Immutable model-facing relative-path base for one sandbox run.

    This scope changes only how relative paths are anchored. The owning sandbox session and its
    existing workspace policy remain responsible for access validation and filesystem operations.
    """

    cwd: PurePosixPath | None = None

    def __post_init__(self) -> None:
        if self.cwd is not None:
            object.__setattr__(self, "cwd", normalize_sandbox_cwd(self.cwd))

    @classmethod
    def from_cwd(cls, cwd: str | PurePath | None) -> SandboxWorkspaceScope:
        """Create a scope from an optional workspace-relative working directory."""

        return cls(cwd=normalize_sandbox_cwd(cwd) if cwd is not None else None)

    def anchor(self, path: str | PurePath) -> str | PurePath:
        """Anchor a relative sandbox path beneath this scope's working directory."""

        if self.cwd is None or _is_absolute_sandbox_path(path):
            return path
        raw_path = path.as_posix() if isinstance(path, PurePath) else path
        return self.cwd / PurePosixPath(raw_path)

    def model_path(self, workspace_relative_path: str | PurePath) -> PurePosixPath:
        """Render a workspace-root-relative path relative to the model-facing cwd."""

        relative_path = coerce_posix_path(workspace_relative_path)
        if relative_path.is_absolute():
            raise ValueError("workspace-relative display paths must not be absolute")
        if self.cwd is None:
            return PurePosixPath(posixpath.normpath(relative_path.as_posix()))
        return PurePosixPath(
            posixpath.relpath(
                posixpath.normpath(relative_path.as_posix()),
                start=self.cwd.as_posix(),
            )
        )

    def model_resource_path(
        self,
        *,
        workspace_root: str | PurePath,
        workspace_relative_path: str | PurePath,
    ) -> PurePosixPath:
        """Render a session-owned workspace resource for model-facing instructions.

        Without a run cwd, preserve the existing workspace-root-relative representation. With a
        run cwd, use an absolute sandbox path so the resource remains addressable after a shell
        command selects a nested workdir or changes directory.
        """

        if isinstance(workspace_relative_path, str) and "\\" in workspace_relative_path:
            raise ValueError("session resource paths must use POSIX path separators")
        if windows_absolute_path(workspace_relative_path) is not None:
            raise ValueError("session resource paths must be workspace-relative")

        relative_path = coerce_posix_path(workspace_relative_path)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("session resource paths must be workspace-relative")
        if relative_path.parts in [(), (".",)]:
            raise ValueError("session resource paths must be non-empty")

        normalized_path = PurePosixPath(posixpath.normpath(relative_path.as_posix()))
        if self.cwd is None:
            return normalized_path

        windows_root = windows_absolute_path(workspace_root)
        if windows_root is not None:
            root_path = PurePosixPath(windows_root.as_posix())
        else:
            if isinstance(workspace_root, str) and "\\" in workspace_root:
                raise ValueError("sandbox workspace root must be POSIX absolute")
            root_path = coerce_posix_path(workspace_root)
            if not root_path.is_absolute():
                raise ValueError("sandbox workspace root must be POSIX absolute")
        return PurePosixPath(posixpath.normpath((root_path / normalized_path).as_posix()))

    def display_path(
        self,
        *,
        original_path: str | PurePath,
        workspace_relative_path: str | PurePath,
    ) -> PurePosixPath:
        """Render a tool result path without changing existing absolute-input display behavior."""

        relative_path = coerce_posix_path(workspace_relative_path)
        if self.cwd is None or _is_absolute_sandbox_path(original_path):
            return PurePosixPath(posixpath.normpath(relative_path.as_posix()))
        return self.model_path(relative_path)


def _native_path_from_windows_absolute(path: PureWindowsPath) -> Path | None:
    native_path = Path(path)
    return native_path if native_path.is_absolute() else None


class SandboxPathGrant(BaseModel):
    """Extra absolute path access outside the sandbox workspace.

    ``path`` is the POSIX path visible inside the sandbox. ``host_path`` is an optional
    native host source used for local materialization and Docker bind mounts.
    """

    path: str
    read_only: bool = False
    description: str | None = None
    host_path: str | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("path", mode="before")
    @classmethod
    def _coerce_path(cls, value: object) -> str:
        if isinstance(value, PurePath):
            return value.as_posix()
        if isinstance(value, str):
            return value
        raise ValueError("sandbox path grant path must be a string or Path")

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        if (windows_path := windows_absolute_path(value)) is not None:
            native_path = _native_path_from_windows_absolute(windows_path)
            if native_path is not None:
                _raise_if_filesystem_root(native_path)
                return str(native_path)
            raise ValueError("sandbox path grant path must be POSIX absolute")

        path = PurePosixPath(posixpath.normpath(value))
        if path.is_absolute():
            _raise_if_filesystem_root(path)
            return path.as_posix()

        raise ValueError("sandbox path grant path must be POSIX absolute")

    @field_validator("host_path", mode="before")
    @classmethod
    def _coerce_host_path(cls, value: object) -> str | None:
        if value is None:
            return None
        if isinstance(value, PurePath):
            return str(value)
        if isinstance(value, str):
            return value
        raise ValueError("sandbox path grant host_path must be a string or Path")

    @field_validator("host_path")
    @classmethod
    def _validate_host_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value.startswith(("\\\\", "//")):
            raise ValueError("sandbox path grant host_path does not support UNC or device paths")
        if any(part == ".." for part in re.split(r"[\\/]", value)):
            raise ValueError("sandbox path grant host_path must not contain parent segments")

        windows_path = PureWindowsPath(value)
        if windows_path.is_absolute():
            if not re.fullmatch(r"[A-Za-z]:", windows_path.drive):
                raise ValueError(
                    "sandbox path grant host_path does not support UNC or device paths"
                )
            _raise_if_filesystem_root(windows_path)
            return str(windows_path)

        posix_path = PurePosixPath(posixpath.normpath(value))
        if posix_path.is_absolute():
            _raise_if_filesystem_root(posix_path)
            return posix_path.as_posix()

        raise ValueError("sandbox path grant host_path must be an absolute host path")

    @model_validator(mode="after")
    def _validate_split_path_grant(self) -> SandboxPathGrant:
        if self.host_path is not None and windows_absolute_path(self.path) is not None:
            raise ValueError(
                "sandbox path grant path must be POSIX absolute when host_path is configured"
            )
        return self


def sandbox_path_grant_host_path(grant: SandboxPathGrant) -> Path:
    """Return and validate the native host path used by a sandbox path grant."""

    raw_path = grant.host_path if grant.host_path is not None else grant.path
    native_path = Path(raw_path)
    if grant.host_path is not None and not native_path.is_absolute():
        raise ValueError(
            f"sandbox path grant host_path must be absolute on the current host: {raw_path}"
        )
    if (
        grant.host_path is not None
        and os.name == "nt"
        and not re.fullmatch(r"[A-Za-z]:", PureWindowsPath(raw_path).drive)
    ):
        raise ValueError(
            f"sandbox path grant host_path must be drive-qualified on Windows: {raw_path}"
        )
    _raise_if_filesystem_root(native_path)
    resolved_path = native_path.resolve(strict=False)
    _raise_if_filesystem_root(resolved_path, resolved=True)
    return resolved_path


class WorkspacePathPolicy:
    """Validate and format paths that are interpreted relative to a sandbox workspace root."""

    def __init__(
        self,
        *,
        root: str | PurePath,
        extra_path_grants: tuple[SandboxPathGrant, ...] = (),
    ) -> None:
        self._root = Path(root)
        self._sandbox_root = coerce_posix_path(root)
        if not self._root.is_absolute() and not self._sandbox_root.is_absolute():
            raise ValueError("sandbox workspace root must be absolute")
        self._root_is_existing_host_path = self._path_exists(self._root)
        self._extra_path_grants = extra_path_grants

    def absolute_workspace_path(self, path: str | PurePath) -> Path:
        """Return an absolute workspace path without following symlinks.

        Examples with root `/workspace`:
        - `absolute_workspace_path("src/app.py")` returns `/workspace/src/app.py`.
        - `absolute_workspace_path("/workspace/src/app.py")` returns `/workspace/src/app.py`.
        - `absolute_workspace_path("/tmp/app.py")` raises `InvalidManifestPathError`.
        """

        if (windows_path := windows_absolute_path(path)) is not None:
            native_path = _native_path_from_windows_absolute(windows_path)
            if self._root_is_existing_host_path and native_path is not None:
                result, _grant = self._resolved_host_path_and_grant(native_path)
                return result
            raise self._invalid_path_error(windows_path)
        normalized = self._absolute_workspace_posix_path(coerce_posix_path(path))
        return self._path_result(normalized)

    def relative_path(self, path: str | PurePath) -> Path:
        """Return a path relative to the workspace root.

        Examples with root `/workspace`:
        - `relative_path("src/app.py")` returns `src/app.py`.
        - `relative_path("/workspace/src/app.py")` returns `src/app.py`.
        - `relative_path("/workspace")` returns `.`.
        """

        if (windows_path := windows_absolute_path(path)) is not None:
            raise self._invalid_path_error(windows_path)
        normalized = self._absolute_workspace_posix_path(coerce_posix_path(path))
        root = self._normalized_root()
        posix_relative = normalized.relative_to(root)
        return (
            self._path_result(posix_relative)
            if posix_relative.parts
            else self._path_result(PurePosixPath("."))
        )

    def normalize_path(
        self,
        path: str | PurePath,
        *,
        for_write: bool = False,
        resolve_symlinks: bool = False,
    ) -> Path:
        """Return a validated absolute path under the workspace or an extra grant.

        `resolve_symlinks` follows symlinks on the host filesystem. Use it only when the sandbox
        workspace is a real local host directory, such as UnixLocalSandboxSession.
        """

        if resolve_symlinks:
            if (windows_path := windows_absolute_path(path)) is not None:
                original = _native_path_from_windows_absolute(windows_path)
                if original is None:
                    raise self._invalid_path_error(windows_path)
            else:
                original = Path(path)
            result, grant = self._resolved_host_path_and_grant(original)
        else:
            if (windows_path := windows_absolute_path(path)) is not None:
                native_path = _native_path_from_windows_absolute(windows_path)
                if self._root_is_existing_host_path and native_path is not None:
                    result, grant = self._resolved_host_path_and_grant(native_path)
                    if for_write:
                        self._raise_if_read_only_grant(result, grant)
                    return result
                raise self._invalid_path_error(windows_path)
            sandbox_result, grant = self._sandbox_path_and_grant(coerce_posix_path(path))
            result = self._path_result(sandbox_result)
        if for_write:
            self._raise_if_read_only_grant(result, grant)
        return result

    def normalize_sandbox_path(
        self,
        path: str | PurePath,
        *,
        for_write: bool = False,
    ) -> PurePosixPath:
        """Return a validated POSIX path for a Unix-like remote sandbox filesystem."""

        if (windows_path := windows_absolute_path(path)) is not None:
            raise self._invalid_path_error(windows_path)
        original = coerce_posix_path(path)
        result, grant = self._sandbox_path_and_grant(original)
        if for_write:
            self._raise_if_read_only_grant(posix_path_for_error(result), grant)
        return result

    def sandbox_root(self) -> PurePosixPath:
        """Return the workspace root as a POSIX path for remote sandbox commands."""

        return self._normalized_root()

    def root_is_existing_host_path(self) -> bool:
        """Return whether the configured root currently exists on the host filesystem."""

        return self._root_is_existing_host_path

    def _resolved_host_path_and_grant(
        self,
        original: Path,
    ) -> tuple[Path, SandboxPathGrant | None]:
        workspace_root = self._root.resolve(strict=False)
        if original.is_absolute():
            resolved = original.resolve(strict=False)
        else:
            absolute = self._absolute_workspace_posix_path(coerce_posix_path(original))
            resolved = Path(str(absolute)).resolve(strict=False)

        if self._is_under(resolved, workspace_root):
            return resolved, None
        grant = self._matching_grant(resolved, resolve_roots=True)
        if grant is None:
            raise self._invalid_path_error(original)
        return resolved, grant

    def _sandbox_path_and_grant(
        self,
        original: PurePosixPath,
    ) -> tuple[PurePosixPath, SandboxPathGrant | None]:
        normalized = (
            self._absolute_posix_path(original)
            if original.is_absolute()
            else self._absolute_workspace_posix_path(original)
        )
        if self._is_under(normalized, self._normalized_root()):
            return normalized, None
        grant = self._matching_grant(normalized)
        if original.is_absolute() and grant is not None:
            return normalized, grant
        raise self._invalid_path_error(original)

    def _raise_if_read_only_grant(
        self,
        path: Path,
        grant: SandboxPathGrant | None,
    ) -> None:
        if grant is None or not grant.read_only:
            return
        error_path = path if self._root_is_existing_host_path else posix_path_for_error(path)
        raise WorkspaceArchiveWriteError(
            path=error_path,
            context={
                "reason": "read_only_extra_path_grant",
                "grant_path": grant.path,
            },
        )

    def extra_path_grant_rules(self) -> tuple[tuple[PurePosixPath, bool], ...]:
        """Return normalized extra grant roots and access modes for remote realpath checks."""

        rules: list[tuple[PurePosixPath, bool]] = []
        for grant in self._extra_path_grants:
            if windows_absolute_path(grant.path) is not None:
                raise ValueError("sandbox path grant path must be POSIX absolute")
            root = coerce_posix_path(grant.path)
            _raise_if_filesystem_root(root)
            rules.append((root, grant.read_only))
        return tuple(rules)

    def _absolute_workspace_posix_path(self, path: PurePosixPath) -> PurePosixPath:
        normalized = self._absolute_posix_path(path)
        root = self._normalized_root()
        try:
            normalized.relative_to(root)
        except ValueError as exc:
            raise self._invalid_path_error(path, cause=exc) from exc
        return normalized

    def _absolute_posix_path(self, path: PurePosixPath) -> PurePosixPath:
        root = self._normalized_root()
        raw_candidate = path.as_posix() if path.is_absolute() else str(root / path.as_posix())
        return PurePosixPath(posixpath.normpath(str(raw_candidate)))

    def _normalized_root(self) -> PurePosixPath:
        return PurePosixPath(posixpath.normpath(self._sandbox_root.as_posix()))

    @staticmethod
    def _path_exists(path: Path) -> bool:
        try:
            return path.exists()
        except OSError:
            return False

    def _path_result(self, path: PurePosixPath) -> Path:
        if self._root_is_existing_host_path:
            return Path(path.as_posix())
        return posix_path_as_path(path)

    def _matching_grant(
        self,
        path: PurePath,
        *,
        resolve_roots: bool = False,
    ) -> SandboxPathGrant | None:
        matches: list[tuple[SandboxPathGrant, PurePath]] = []
        for grant in self._extra_path_grants:
            grant_root: PurePath = (
                sandbox_path_grant_host_path(grant).resolve(strict=False)
                if resolve_roots
                else coerce_posix_path(grant.path)
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
        path: PurePath,
        *,
        cause: BaseException | None = None,
    ) -> InvalidManifestPathError:
        reason: Literal["absolute", "escape_root"] = (
            "absolute" if path.is_absolute() else "escape_root"
        )
        return InvalidManifestPathError(rel=path.as_posix(), reason=reason, cause=cause)
