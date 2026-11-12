from __future__ import annotations

import errno
import hashlib
import io
import ipaddress
import os
import re
import stat
import uuid
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import ExitStack
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Literal

from pydantic import Field, field_serializer, field_validator

from ...exceptions import _detach_source_exception_traceback, _raise_data_redacted_error
from ..errors import (
    GitCloneError,
    GitCopyError,
    GitMissingInImageError,
    GitSubpathError,
    LocalChecksumError,
    LocalDirReadError,
    LocalFileReadError,
    _InvalidGitHostError,
    _redact_git_host_error_data,
)
from ..materialization import MaterializedFile, _SkillCopyBudget, gather_in_order
from ..types import ExecResult, User
from ..workspace_paths import SandboxPathGrant, sandbox_path_grant_host_path, sandbox_path_str
from .base import BaseEntry

if TYPE_CHECKING:
    from ..session.base_sandbox_session import BaseSandboxSession

_COMMIT_REF_RE = re.compile(r"[0-9a-fA-F]{7,40}")
_GIT_AUTHORITY_RE = re.compile(r"(?:([^:\[\]]+)|\[([0-9a-fA-F:.]+)\])(?::([0-9]+))?")
_GIT_REG_NAME_RE = re.compile(r"[A-Za-z0-9._~!$&'()*+,;=-]+")
_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd
_HAS_O_DIRECTORY = hasattr(os, "O_DIRECTORY")

_GIT_COPY_SOURCE_BOUNDARY = """case "$source_root" in
    "$repo_root"|"$repo_root"/*) ;;
    *) echo 'Git source directory is outside the checkout' >&2; exit 1 ;;
esac
"""

_GIT_COPY = (
    """set -eu
CDPATH=
cd -P .
# Resolve relative PATH entries from the provider's original working directory.
# Changing to the checkout must not change where copy tools look for helpers.
remaining_path=${PATH-/usr/bin:/bin}
copy_path=
while :; do
    directory=${remaining_path%%:*}
    case "$directory" in
        /*) ;;
        *) directory="${PWD%/}/${directory:-.}" ;;
    esac
    case "$directory" in
        *:*) ;;
        *) copy_path="${copy_path:+$copy_path:}$directory" ;;
    esac
    case "$remaining_path" in
        *:*) remaining_path=${remaining_path#*:} ;;
        *) break ;;
    esac
done
PATH="${copy_path:-/usr/bin:/bin}"
export PATH
destination=$3
case "$destination" in
    /*) ;;
    *) destination="$PWD/$destination" ;;
esac
cd -P /tmp
tmp_root=$PWD
cd -P "$1"
repo_root=$PWD
[ "$repo_root" = "$tmp_root/${1##*/}" ] || {
    echo 'Git checkout is outside its temporary directory' >&2
    exit 1
}
cd -P "./$2"
source_root=$PWD
"""
    + _GIT_COPY_SOURCE_BOUNDARY
    + """exec cp -R -P -- . "$destination/"
"""
)


def _absolute_without_symlink_resolution(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _sha256_handle(handle: io.BufferedIOBase) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = handle.read(1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


_DOCKER_GIT_REPO_ERROR = "Docker sandboxes do not support GitRepo entries."


def _git_repos(entries: Iterable[BaseEntry]) -> Iterator[GitRepo]:
    for entry in entries:
        if isinstance(entry, Dir):
            yield from _git_repos(entry.children.values())
        elif isinstance(entry, GitRepo):
            yield entry


def _validate_git_repo_entries(entries: Iterable[BaseEntry], *, supports_git_repo: bool) -> None:
    has_git_repo = False
    for entry in _git_repos(entries):
        if entry._validated_host() is None:
            raise _InvalidGitHostError()
        has_git_repo = True
    if has_git_repo and not supports_git_repo:
        raise ValueError(_DOCKER_GIT_REPO_ERROR)


class Dir(BaseEntry):
    type: Literal["dir"] = "dir"
    is_dir: bool = True
    children: dict[str | Path, BaseEntry] = Field(default_factory=dict)

    @field_validator("children", mode="before")
    @classmethod
    def _parse_children(cls, value: object) -> dict[str | Path, BaseEntry]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise TypeError(f"Artifact mapping must be a mapping, got {type(value).__name__}")
        return {key: BaseEntry.parse(entry) for key, entry in value.items()}

    @field_serializer("children", when_used="json")
    def _serialize_children(self, children: Mapping[str | Path, BaseEntry]) -> dict[str, object]:
        out: dict[str, object] = {}
        for key, entry in children.items():
            key_str = key.as_posix() if isinstance(key, Path) else str(key)
            out[key_str] = entry.model_dump(mode="json")
        return out

    def model_post_init(self, context: object, /) -> None:
        _ = context
        self.permissions.directory = True

    async def apply(
        self,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        try:
            _validate_git_repo_entries(
                self.children.values(), supports_git_repo=session._supports_git_repo
            )
            await session.mkdir(dest, parents=True)
            await self._apply_metadata(session, dest)
            return await session._apply_entry_batch(
                [(dest / Path(rel_dest), artifact) for rel_dest, artifact in self.children.items()],
                base_dir=base_dir,
            )
        except _InvalidGitHostError as error:
            _detach_source_exception_traceback(error)

        del self, session, dest, base_dir
        _raise_data_redacted_error(_InvalidGitHostError())


class File(BaseEntry):
    type: Literal["file"] = "file"
    content: bytes

    async def apply(
        self,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        await session.write(dest, io.BytesIO(self.content))
        await self._apply_metadata(session, dest)
        return []


class LocalFile(BaseEntry):
    type: Literal["local_file"] = "local_file"
    src: Path

    async def apply(
        self,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        src = _absolute_without_symlink_resolution(base_dir / self.src)
        local_dir = LocalDir(src=self.src.parent)
        rel_child = Path(self.src.name)
        fd: int | None = None
        try:
            source_grants = session.state.manifest.extra_path_grants
            src_root = local_dir._resolve_local_dir_src_root(
                base_dir,
                source_grants=source_grants,
            )
            fd = local_dir._open_local_dir_file_for_copy(
                base_dir=base_dir,
                src_root=src_root,
                rel_child=rel_child,
                source_grants=source_grants,
            )
            with os.fdopen(fd, "rb") as f:
                fd = None
                try:
                    checksum = _sha256_handle(f)
                    f.seek(0)
                except OSError as e:
                    raise LocalChecksumError(src=src, cause=e) from e
                await session.mkdir(Path(dest).parent, parents=True)
                await session.write(dest, f)
        except LocalDirReadError as e:
            context = dict(e.context)
            context.pop("src", None)
            raise LocalFileReadError(src=src, context=context, cause=e.cause) from e
        except OSError as e:
            raise LocalFileReadError(src=src, cause=e) from e
        finally:
            if fd is not None:
                os.close(fd)
        await self._apply_metadata(session, dest)
        return [MaterializedFile(path=dest, sha256=checksum)]


class _LocalDirBudget:
    def __init__(self) -> None:
        self.members = 0
        self.scanned_bytes = 0
        self.copied_bytes = 0
        self.sandbox: _SkillCopyBudget | None = None
        self.reserved_bytes = 0

    def admit(self, sandbox: _SkillCopyBudget) -> None:
        sandbox.reserve(members=self.members + 1, bytes=self.scanned_bytes)
        self.sandbox = sandbox
        self.reserved_bytes = self.scanned_bytes

    def observe(self, path: Path, info: os.stat_result) -> None:
        self.members += 1
        if self.members > 4096:
            raise ValueError("Skill materialization exceeds the member limit")
        if len(path.parts) > 32:
            raise ValueError("Skill materialization exceeds the depth limit")
        if stat.S_ISREG(info.st_mode):
            self.scanned_bytes += info.st_size
            if self.scanned_bytes > 64 * 1024 * 1024:
                raise ValueError("Skill materialization exceeds the byte limit")

    def snapshot(self, handle: io.BufferedReader) -> io.BytesIO:
        remaining = 64 * 1024 * 1024 - self.copied_bytes
        content = handle.read(remaining + 1)
        if len(content) > remaining:
            raise ValueError("Skill materialization exceeds the byte limit")
        copied_bytes = self.copied_bytes + len(content)
        if self.sandbox is not None and copied_bytes > self.reserved_bytes:
            self.sandbox.reserve(bytes=copied_bytes - self.reserved_bytes)
            self.reserved_bytes = copied_bytes
        self.copied_bytes = copied_bytes
        return io.BytesIO(content)


class LocalDir(BaseEntry):
    type: Literal["local_dir"] = "local_dir"
    is_dir: bool = True
    src: Path | None = Field(default=None)

    def model_post_init(self, context: object, /) -> None:
        _ = context
        self.permissions.directory = True

    async def apply(
        self,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
        *,
        user: str | User | None = None,
    ) -> list[MaterializedFile]:
        return await self._apply(session, dest, base_dir, user=user)

    async def _apply_bounded(
        self,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
        *,
        user: str | User | None = None,
    ) -> list[MaterializedFile]:
        sandbox = session._skill_copy_budget()
        async with sandbox.lock:
            sandbox.reserve()
            return await self._apply(session, dest, base_dir, user=user, budget=_LocalDirBudget())

    async def _apply(
        self,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
        *,
        user: str | User | None,
        budget: _LocalDirBudget | None = None,
    ) -> list[MaterializedFile]:
        files: list[MaterializedFile] = []
        if self.src:
            source_grants = session.state.manifest.extra_path_grants
            src_root = self._resolve_local_dir_src_root(
                base_dir,
                source_grants=source_grants,
            )
            # Minimal v1: copy all files recursively.
            try:
                if budget is None:
                    await session.mkdir(dest, parents=True, user=user)
                files = []
                local_files = self._list_local_dir_files(
                    base_dir=base_dir,
                    src_root=src_root,
                    source_grants=source_grants,
                    budget=budget,
                )
                if budget is not None:
                    budget.admit(session._skill_copy_budget())
                    await session.mkdir(dest, parents=True, user=user)

                def _make_copy_task(child: Path) -> Callable[[], Awaitable[MaterializedFile]]:
                    async def _copy() -> MaterializedFile:
                        return await self._copy_local_dir_file(
                            base_dir=base_dir,
                            session=session,
                            src_root=src_root,
                            src=src_root / child,
                            dest_root=dest,
                            user=user,
                            source_grants=source_grants,
                            budget=budget,
                        )

                    return _copy

                copied_files = await gather_in_order(
                    [_make_copy_task(child) for child in local_files],
                    max_concurrency=session._max_local_dir_file_concurrency,
                )
                files.extend(copied_files)
            except OSError as e:
                raise LocalDirReadError(src=src_root, cause=e) from e
            if user is None:
                await self._apply_metadata(session, dest)
        else:
            await session.mkdir(dest, parents=True, user=user)
            if user is None:
                await self._apply_metadata(session, dest)
        return files

    def _resolve_local_dir_src_root(
        self,
        base_dir: Path,
        *,
        source_grants: tuple[SandboxPathGrant, ...] = (),
    ) -> Path:
        assert self.src is not None
        src_input = self._resolved_src_input(base_dir, source_grants=source_grants)
        for current in self._iter_local_dir_source_paths(base_dir):
            try:
                current_stat = current.lstat()
            except FileNotFoundError:
                raise LocalDirReadError(
                    src=src_input,
                    context={"reason": "path_not_found"},
                ) from None
            except OSError as e:
                raise LocalDirReadError(src=current, cause=e) from e
            if stat.S_ISLNK(current_stat.st_mode):
                raise LocalDirReadError(
                    src=src_input,
                    context={
                        "reason": "symlink_not_supported",
                        "child": self._local_dir_source_child_label(base_dir, current),
                    },
                )
        return src_input

    def _resolved_src_input(
        self,
        base_dir: Path,
        *,
        source_grants: tuple[SandboxPathGrant, ...] = (),
    ) -> Path:
        assert self.src is not None
        src_input = _absolute_without_symlink_resolution(base_dir / self.src)

        base = _absolute_without_symlink_resolution(base_dir)
        try:
            src_input.relative_to(base)
            return src_input
        except ValueError as base_error:
            matching_grant = self._matching_source_grant(src_input, source_grants)
            if matching_grant is not None:
                return src_input
            grant_paths = [grant.path for grant in source_grants]
            context: dict[str, object] = {"reason": "outside_base_dir", "base_dir": str(base)}
            if grant_paths:
                context["extra_path_grants"] = grant_paths
            raise LocalDirReadError(
                src=src_input,
                context=context,
                cause=base_error,
            ) from base_error

    @staticmethod
    def _matching_source_grant(
        src_input: Path,
        source_grants: tuple[SandboxPathGrant, ...],
    ) -> SandboxPathGrant | None:
        for grant in source_grants:
            grant_root = _absolute_without_symlink_resolution(sandbox_path_grant_host_path(grant))
            try:
                src_input.relative_to(grant_root)
                return grant
            except ValueError:
                continue
        return None

    def _iter_local_dir_source_paths(self, base_dir: Path) -> list[Path]:
        assert self.src is not None
        if self.src.is_absolute():
            current = Path(self.src.anchor)
            parts = self.src.parts[1:]
        else:
            current = base_dir
            parts = self.src.parts

        paths: list[Path] = []
        if not parts:
            paths.append(current)
            return paths

        for part in parts:
            current = current / part
            paths.append(current)
        return paths

    def _local_dir_source_child_label(self, base_dir: Path, current: Path) -> str:
        try:
            return current.relative_to(base_dir).as_posix()
        except ValueError:
            return current.as_posix()

    def _list_local_dir_files(
        self,
        *,
        base_dir: Path,
        src_root: Path,
        source_grants: tuple[SandboxPathGrant, ...] = (),
        budget: _LocalDirBudget | None = None,
    ) -> list[Path]:
        if _OPEN_SUPPORTS_DIR_FD and _HAS_O_DIRECTORY:
            return self._list_local_dir_files_pinned(
                base_dir=base_dir,
                src_root=src_root,
                source_grants=source_grants,
                budget=budget,
            )

        if budget is not None:
            return self._list_bounded_local_dir_files(src_root, Path(), budget)

        local_files: list[Path] = []
        for child in src_root.rglob("*"):
            child_stat = child.lstat()
            if stat.S_ISLNK(child_stat.st_mode):
                raise LocalDirReadError(
                    src=src_root,
                    context={
                        "reason": "symlink_not_supported",
                        "child": child.relative_to(src_root).as_posix(),
                    },
                )
            if stat.S_ISREG(child_stat.st_mode):
                local_files.append(child.relative_to(src_root))
        return local_files

    def _list_bounded_local_dir_files(
        self, src_root: Path, relative: Path, budget: _LocalDirBudget
    ) -> list[Path]:
        files: list[Path] = []
        with os.scandir(src_root / relative) as entries:
            for entry in entries:
                child = relative / entry.name
                info = entry.stat(follow_symlinks=False)
                budget.observe(child, info)
                if stat.S_ISLNK(info.st_mode):
                    raise LocalDirReadError(
                        src=src_root,
                        context={"reason": "symlink_not_supported", "child": child.as_posix()},
                    )
                if stat.S_ISREG(info.st_mode):
                    files.append(child)
                elif stat.S_ISDIR(info.st_mode):
                    files.extend(self._list_bounded_local_dir_files(src_root, child, budget))
        return files

    def _list_local_dir_files_pinned(
        self,
        *,
        base_dir: Path,
        src_root: Path,
        source_grants: tuple[SandboxPathGrant, ...] = (),
        budget: _LocalDirBudget | None = None,
    ) -> list[Path]:
        root_fd: int | None = None
        try:
            root_fd = self._open_local_dir_src_root_fd(
                base_dir=base_dir,
                src_root=src_root,
                source_grants=source_grants,
            )
            return self._list_local_dir_files_from_dir_fd(
                src_root=src_root, dir_fd=root_fd, budget=budget
            )
        finally:
            if root_fd is not None:
                os.close(root_fd)

    def _list_local_dir_files_from_dir_fd(
        self,
        *,
        src_root: Path,
        dir_fd: int,
        rel_dir: Path = Path(),
        budget: _LocalDirBudget | None = None,
    ) -> list[Path]:
        dir_flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        local_files: list[Path] = []
        for entry in os.scandir(dir_fd):
            rel_child = rel_dir / entry.name if rel_dir.parts else Path(entry.name)
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                raise LocalDirReadError(
                    src=src_root,
                    context={"reason": "path_changed_during_copy", "child": rel_child.as_posix()},
                ) from None
            except OSError as e:
                raise LocalDirReadError(src=src_root, cause=e) from e
            if budget is not None:
                budget.observe(rel_child, entry_stat)
            if stat.S_ISLNK(entry_stat.st_mode):
                raise LocalDirReadError(
                    src=src_root,
                    context={"reason": "symlink_not_supported", "child": rel_child.as_posix()},
                )
            if stat.S_ISREG(entry_stat.st_mode):
                local_files.append(rel_child)
                continue
            if not stat.S_ISDIR(entry_stat.st_mode):
                continue

            child_fd: int | None = None
            try:
                child_fd = os.open(entry.name, dir_flags, dir_fd=dir_fd)
                child_stat = os.fstat(child_fd)
                if not stat.S_ISDIR(child_stat.st_mode):
                    raise LocalDirReadError(
                        src=src_root,
                        context={
                            "reason": "path_changed_during_copy",
                            "child": rel_child.as_posix(),
                        },
                    )
                local_files.extend(
                    self._list_local_dir_files_from_dir_fd(
                        src_root=src_root,
                        dir_fd=child_fd,
                        rel_dir=rel_child,
                        budget=budget,
                    )
                )
            except FileNotFoundError:
                raise LocalDirReadError(
                    src=src_root,
                    context={"reason": "path_changed_during_copy", "child": rel_child.as_posix()},
                ) from None
            except OSError as e:
                raise self._local_dir_open_error(
                    src_root=src_root,
                    parent_fd=dir_fd,
                    entry_name=entry.name,
                    rel_child=rel_child,
                    expect_dir=True,
                    error=e,
                ) from e
            finally:
                if child_fd is not None:
                    os.close(child_fd)
        return local_files

    async def _copy_local_dir_file(
        self,
        *,
        base_dir: Path,
        session: BaseSandboxSession,
        src_root: Path,
        src: Path,
        dest_root: Path,
        user: str | User | None = None,
        source_grants: tuple[SandboxPathGrant, ...] = (),
        budget: _LocalDirBudget | None = None,
    ) -> MaterializedFile:
        rel_child = src.relative_to(src_root)
        child_dest = dest_root / rel_child
        fd: int | None = None
        try:
            fd = self._open_local_dir_file_for_copy(
                base_dir=base_dir,
                src_root=src_root,
                rel_child=rel_child,
                source_grants=source_grants,
                nonblocking=budget is not None,
            )
            with ExitStack() as stack:
                original = stack.enter_context(os.fdopen(fd, "rb"))
                fd = None
                f = original if budget is None else stack.enter_context(budget.snapshot(original))
                checksum = _sha256_handle(f)
                f.seek(0)
                await session.mkdir(child_dest.parent, parents=True, user=user)
                await session.write(child_dest, f, user=user)
        except OSError as e:
            raise LocalFileReadError(src=src, cause=e) from e
        finally:
            if fd is not None:
                os.close(fd)
        return MaterializedFile(path=child_dest, sha256=checksum)

    def _open_local_dir_file_for_copy(
        self,
        *,
        base_dir: Path,
        src_root: Path,
        rel_child: Path,
        source_grants: tuple[SandboxPathGrant, ...] = (),
        nonblocking: bool = False,
    ) -> int:
        if not _OPEN_SUPPORTS_DIR_FD or not _HAS_O_DIRECTORY:
            return self._open_local_dir_file_for_copy_fallback(
                base_dir=base_dir,
                src_root=src_root,
                rel_child=rel_child,
                source_grants=source_grants,
                nonblocking=nonblocking,
            )

        dir_flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        file_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if nonblocking:
            file_flags |= getattr(os, "O_NONBLOCK", 0)
        dir_fds: list[int] = []
        current_rel = Path()
        try:
            current_fd = self._open_local_dir_src_root_fd(
                base_dir=base_dir,
                src_root=src_root,
                source_grants=source_grants,
            )
            dir_fds.append(current_fd)
            for part in rel_child.parts[:-1]:
                current_rel = current_rel / part if current_rel.parts else Path(part)
                try:
                    next_fd = os.open(part, dir_flags, dir_fd=current_fd)
                except OSError as e:
                    raise self._local_dir_open_error(
                        src_root=src_root,
                        parent_fd=current_fd,
                        entry_name=part,
                        rel_child=current_rel,
                        expect_dir=True,
                        error=e,
                    ) from e
                next_stat = os.fstat(next_fd)
                if not stat.S_ISDIR(next_stat.st_mode):
                    raise LocalDirReadError(
                        src=src_root,
                        context={
                            "reason": "path_changed_during_copy",
                            "child": rel_child.as_posix(),
                        },
                    )
                dir_fds.append(next_fd)
                current_fd = next_fd

            try:
                leaf_fd = os.open(rel_child.name, file_flags, dir_fd=current_fd)
            except OSError as e:
                raise self._local_dir_open_error(
                    src_root=src_root,
                    parent_fd=current_fd,
                    entry_name=rel_child.name,
                    rel_child=rel_child,
                    expect_dir=False,
                    error=e,
                ) from e
            leaf_stat = os.fstat(leaf_fd)
            if not stat.S_ISREG(leaf_stat.st_mode):
                os.close(leaf_fd)
                raise LocalDirReadError(
                    src=src_root,
                    context={"reason": "path_changed_during_copy", "child": rel_child.as_posix()},
                )
            return leaf_fd
        except FileNotFoundError:
            raise LocalDirReadError(
                src=src_root,
                context={"reason": "path_changed_during_copy", "child": rel_child.as_posix()},
            ) from None
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise LocalDirReadError(
                    src=src_root,
                    context={"reason": "symlink_not_supported", "child": rel_child.as_posix()},
                ) from e
            raise LocalFileReadError(src=src_root / rel_child, cause=e) from e
        finally:
            for dir_fd in reversed(dir_fds):
                os.close(dir_fd)

    def _open_local_dir_src_root_fd(
        self,
        *,
        base_dir: Path,
        src_root: Path,
        source_grants: tuple[SandboxPathGrant, ...] = (),
    ) -> int:
        assert self.src is not None
        self._resolved_src_input(base_dir, source_grants=source_grants)

        dir_flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        dir_fds: list[int] = []
        current_rel = Path()
        if self.src.is_absolute():
            current_path = Path(self.src.anchor)
            parts = self.src.parts[1:]
        else:
            current_path = base_dir
            parts = self.src.parts

        try:
            current_fd = os.open(current_path, dir_flags)
            dir_fds.append(current_fd)
            for part in parts:
                current_rel = current_rel / part if current_rel.parts else Path(part)
                try:
                    next_fd = os.open(part, dir_flags, dir_fd=current_fd)
                except OSError as e:
                    raise self._local_dir_open_error(
                        src_root=src_root,
                        parent_fd=current_fd,
                        entry_name=part,
                        rel_child=current_rel,
                        expect_dir=True,
                        error=e,
                    ) from e
                next_stat = os.fstat(next_fd)
                if not stat.S_ISDIR(next_stat.st_mode):
                    raise LocalDirReadError(
                        src=src_root,
                        context={
                            "reason": "path_changed_during_copy",
                            "child": current_rel.as_posix(),
                        },
                    )
                dir_fds.append(next_fd)
                current_fd = next_fd
            return dir_fds.pop()
        except FileNotFoundError:
            raise LocalDirReadError(
                src=src_root, context={"reason": "path_changed_during_copy"}
            ) from None
        except OSError as e:
            raise LocalDirReadError(src=src_root, cause=e) from e
        finally:
            for dir_fd in reversed(dir_fds):
                os.close(dir_fd)

    def _local_dir_open_error(
        self,
        *,
        src_root: Path,
        parent_fd: int,
        entry_name: str,
        rel_child: Path,
        expect_dir: bool,
        error: OSError,
    ) -> LocalDirReadError:
        try:
            entry_stat = os.stat(entry_name, dir_fd=parent_fd, follow_symlinks=False)
        except (AttributeError, NotImplementedError, TypeError):
            entry_stat = None
        except FileNotFoundError:
            return LocalDirReadError(
                src=src_root,
                context={"reason": "path_changed_during_copy", "child": rel_child.as_posix()},
            )
        except OSError:
            entry_stat = None

        if entry_stat is not None and stat.S_ISLNK(entry_stat.st_mode):
            return LocalDirReadError(
                src=src_root,
                context={"reason": "symlink_not_supported", "child": rel_child.as_posix()},
            )
        if entry_stat is not None and (
            (expect_dir and not stat.S_ISDIR(entry_stat.st_mode))
            or (not expect_dir and not stat.S_ISREG(entry_stat.st_mode))
        ):
            return LocalDirReadError(
                src=src_root,
                context={"reason": "path_changed_during_copy", "child": rel_child.as_posix()},
            )
        if error.errno == errno.ELOOP:
            return LocalDirReadError(
                src=src_root,
                context={"reason": "symlink_not_supported", "child": rel_child.as_posix()},
            )
        return LocalDirReadError(src=src_root, cause=error)

    def _open_local_dir_file_for_copy_fallback(
        self,
        *,
        base_dir: Path,
        src_root: Path,
        rel_child: Path,
        source_grants: tuple[SandboxPathGrant, ...] = (),
        nonblocking: bool = False,
    ) -> int:
        assert self.src is not None
        src = src_root / rel_child
        validation_dir = LocalDir(src=self.src / rel_child.parent)
        try:
            src_stat = src.lstat()
        except FileNotFoundError:
            raise LocalDirReadError(
                src=src_root,
                context={"reason": "path_changed_during_copy", "child": rel_child.as_posix()},
            ) from None
        except OSError as e:
            raise LocalDirReadError(src=src_root, cause=e) from e
        if stat.S_ISLNK(src_stat.st_mode):
            raise LocalDirReadError(
                src=src_root,
                context={"reason": "symlink_not_supported", "child": rel_child.as_posix()},
            )
        if not stat.S_ISREG(src_stat.st_mode):
            raise LocalDirReadError(
                src=src_root,
                context={"reason": "path_changed_during_copy", "child": rel_child.as_posix()},
            )

        file_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if nonblocking:
            file_flags |= getattr(os, "O_NONBLOCK", 0)
        try:
            leaf_fd = os.open(src, file_flags)
            try:
                validation_dir._resolve_local_dir_src_root(
                    base_dir,
                    source_grants=source_grants,
                )
                leaf_stat = os.fstat(leaf_fd)
                if not stat.S_ISREG(leaf_stat.st_mode) or not os.path.samestat(src_stat, leaf_stat):
                    raise LocalDirReadError(
                        src=src_root,
                        context={
                            "reason": "path_changed_during_copy",
                            "child": rel_child.as_posix(),
                        },
                    )
                return leaf_fd
            except Exception:
                os.close(leaf_fd)
                raise
        except FileNotFoundError:
            validation_dir._resolve_local_dir_src_root(base_dir, source_grants=source_grants)
            raise LocalDirReadError(
                src=src_root,
                context={"reason": "path_changed_during_copy", "child": rel_child.as_posix()},
            ) from None
        except OSError as e:
            try:
                validation_dir._resolve_local_dir_src_root(
                    base_dir,
                    source_grants=source_grants,
                )
            except LocalDirReadError as root_error:
                raise root_error from e
            if e.errno == errno.ELOOP:
                raise LocalDirReadError(
                    src=src_root,
                    context={"reason": "symlink_not_supported", "child": rel_child.as_posix()},
                ) from e
            raise LocalFileReadError(src=src, cause=e) from e


class GitRepo(BaseEntry):
    type: Literal["git_repo"] = "git_repo"
    is_dir: bool = True
    host: str = Field(default="github.com", repr=False)
    repo: str  # "owner/name" (or any host-specific path)
    ref: str  # tag/branch/sha
    subpath: str | None = None

    def model_post_init(self, context: object, /) -> None:
        _ = context
        self.permissions.directory = True

    @_redact_git_host_error_data
    async def apply(
        self,
        session: BaseSandboxSession,
        dest: Path,
        base_dir: Path,
    ) -> list[MaterializedFile]:
        host = self._validated_host()
        if host is None:
            raise _InvalidGitHostError()
        if not session._supports_git_repo:
            raise ValueError(_DOCKER_GIT_REPO_ERROR)
        git_subpath = self._validate_subpath()

        # Check Git in the same environment that will be used to clone.
        git_check = await session.exec("command -v git >/dev/null 2>&1")
        if not git_check.ok():
            context: dict[str, object] = {"repo": self.repo, "ref": self.ref}
            image = getattr(session.state, "image", None)
            if image is not None:
                context["image"] = image
            raise GitMissingInImageError(context=context)

        tmp_dir = f"/tmp/sandbox-git-{session.state.session_id.hex}-{uuid.uuid4().hex}"
        git_src_root = self._git_src_root(tmp_dir, git_subpath)
        url = f"https://{host}/{self.repo}.git"

        _ = await session.exec("rm", "-rf", "--", tmp_dir, shell=False)
        try:
            clone_error: ExecResult | None = None
            if self._looks_like_commit_ref(self.ref):
                clone = await self._fetch_commit_ref(session=session, url=url, tmp_dir=tmp_dir)
                if not clone.ok():
                    clone_error = clone
                    _ = await session.exec("rm", "-rf", "--", tmp_dir, shell=False)
                    clone = await self._clone_named_ref(session=session, url=url, tmp_dir=tmp_dir)
            else:
                clone = await self._clone_named_ref(session=session, url=url, tmp_dir=tmp_dir)
            if not clone.ok():
                if clone_error is not None:
                    clone = clone_error
                raise GitCloneError(
                    url=url,
                    ref=self.ref,
                    stderr=clone.stderr.decode("utf-8", errors="replace"),
                    context={"repo": self.repo, "subpath": self.subpath},
                )

            # Copy into destination in the container.
            await session.mkdir(dest, parents=True)
            copy = await session._exec_git_copy(
                _GIT_COPY,
                tmp_dir,
                git_subpath.as_posix() if git_subpath is not None else ".",
                sandbox_path_str(dest),
            )
            if not copy.ok():
                raise GitCopyError(
                    src_root=git_src_root,
                    dest=dest,
                    stderr=copy.stderr.decode("utf-8", errors="replace"),
                    context={"repo": self.repo, "ref": self.ref, "subpath": self.subpath},
                )
        finally:
            _ = await session.exec("rm", "-rf", "--", tmp_dir, shell=False)
        await self._apply_metadata(session, dest)

        # Receipt: leave checksums empty for now. (Computing them would
        # require reading each file back out of the container.)
        return []

    def _validated_host(self) -> str | None:
        host = self.host
        authority = _GIT_AUTHORITY_RE.fullmatch(host)
        if authority is None:
            return None
        name, ipv6, port = authority.groups()
        port = (port or "").lstrip("0")
        if len(port) > 5 or (port and int(port) > 65535):
            return None
        try:
            if ipv6 is not None:
                ipaddress.IPv6Address(ipv6)
                return host
            if not name.isascii():
                name = name.encode("idna").decode("ascii")
            return host if _GIT_REG_NAME_RE.fullmatch(name) is not None else None
        except ValueError:
            return None

    @staticmethod
    def _looks_like_commit_ref(ref: str) -> bool:
        return _COMMIT_REF_RE.fullmatch(ref) is not None

    def _validate_subpath(self) -> PurePosixPath | None:
        if self.subpath is None:
            return None

        original_subpath = self.subpath
        if original_subpath == "":
            return None

        subpath = original_subpath.strip()
        if not subpath:
            raise GitSubpathError(repo=self.repo, subpath=original_subpath, reason="empty")

        posix_subpath = PurePosixPath(subpath)
        windows_subpath = PureWindowsPath(subpath)
        if posix_subpath.as_posix() == ".":
            return None
        if posix_subpath.is_absolute():
            raise GitSubpathError(repo=self.repo, subpath=original_subpath, reason="absolute")
        if "\\" in original_subpath or windows_subpath.drive:
            raise GitSubpathError(repo=self.repo, subpath=original_subpath, reason="windows_path")
        if ".." in posix_subpath.parts:
            raise GitSubpathError(
                repo=self.repo, subpath=original_subpath, reason="parent_traversal"
            )

        return posix_subpath

    def _git_src_root(self, tmp_dir: str, subpath: PurePosixPath | None) -> str:
        if subpath is None:
            return tmp_dir
        return f"{tmp_dir}/{subpath.as_posix()}"

    async def _clone_named_ref(
        self,
        *,
        session: BaseSandboxSession,
        url: str,
        tmp_dir: str,
    ) -> ExecResult:
        return await session.exec(
            "git",
            "clone",
            "--depth",
            "1",
            "--no-tags",
            "--branch",
            self.ref,
            url,
            tmp_dir,
            shell=False,
        )

    async def _fetch_commit_ref(
        self,
        *,
        session: BaseSandboxSession,
        url: str,
        tmp_dir: str,
    ) -> ExecResult:
        init = await session.exec("git", "init", tmp_dir, shell=False)
        if not init.ok():
            return init

        remote_add = await session.exec(
            "git",
            "-C",
            tmp_dir,
            "remote",
            "add",
            "origin",
            url,
            shell=False,
        )
        if not remote_add.ok():
            return remote_add

        fetch = await session.exec(
            "git",
            "-C",
            tmp_dir,
            "fetch",
            "--depth",
            "1",
            "--no-tags",
            "origin",
            self.ref,
            shell=False,
        )
        if not fetch.ok():
            return fetch

        return await session.exec(
            "git",
            "-C",
            tmp_dir,
            "checkout",
            "--detach",
            "FETCH_HEAD",
            shell=False,
        )
