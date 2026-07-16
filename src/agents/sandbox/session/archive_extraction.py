from __future__ import annotations

import io
import shutil
import tarfile
import tempfile
import zipfile
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, cast

from ...run_config import SandboxArchiveLimits
from ..errors import ExecNonZeroError, WorkspaceArchiveWriteError
from ..files import EntryKind, FileEntry
from ..util.tar_utils import UnsafeTarMemberError, safe_tar_member_rel_path


class UnsafeZipMemberError(ValueError):
    """Raised when a zip member would escape or violate archive extraction rules."""

    def __init__(self, *, member: str, reason: str) -> None:
        super().__init__(f"unsafe zip member {member!r}: {reason}")
        self.member = member
        self.reason = reason


class ArchiveResourceLimitError(ValueError):
    """Raised when an archive exceeds extraction resource limits."""

    def __init__(
        self,
        *,
        reason: str,
        limit: int,
        actual: int,
        member: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.limit = limit
        self.actual = actual
        self.member = member


class WorkspaceArchiveExtractor:
    def __init__(
        self,
        *,
        mkdir: Callable[[Path], Awaitable[None]],
        write: Callable[[Path, io.IOBase], Awaitable[None]],
        ls: Callable[[Path], Awaitable[list[FileEntry]]],
    ) -> None:
        self._mkdir = mkdir
        self._write = write
        self._ls = ls

    async def extract_tar_archive(
        self,
        *,
        archive_path: Path,
        destination_root: Path,
        data: io.IOBase,
        archive_limits: SandboxArchiveLimits | None = None,
    ) -> None:
        child_entry_cache: dict[Path, dict[str, EntryKind]] = {}
        try:
            with tarfile.open(fileobj=data, mode="r|*") as archive:
                validate_tar_archive_for_extraction(archive, archive_limits=archive_limits)

            data.seek(0)
            with tarfile.open(fileobj=data, mode="r|*") as archive:
                for member in archive:
                    rel_path = safe_tar_member_rel_path(member)
                    if rel_path is None:
                        continue

                    await self._ensure_no_symlink_extract_parents(
                        destination_root=destination_root,
                        rel_path=rel_path,
                        member_name=member.name,
                        error_type="tar",
                        child_entry_cache=child_entry_cache,
                    )
                    dest = destination_root / rel_path
                    if member.isdir():
                        await self._mkdir(dest)
                        self._record_extract_entry(
                            child_entry_cache=child_entry_cache,
                            destination_root=destination_root,
                            path=dest,
                            kind=EntryKind.DIRECTORY,
                        )
                        continue

                    fileobj = archive.extractfile(member)
                    if fileobj is None:
                        raise UnsafeTarMemberError(
                            member=member.name,
                            reason="missing file payload",
                        )
                    try:
                        await self._mkdir(dest.parent)
                        self._record_extract_entry(
                            child_entry_cache=child_entry_cache,
                            destination_root=destination_root,
                            path=dest.parent,
                            kind=EntryKind.DIRECTORY,
                        )
                        await self._write(dest, cast(io.IOBase, fileobj))
                        self._record_extract_entry(
                            child_entry_cache=child_entry_cache,
                            destination_root=destination_root,
                            path=dest,
                            kind=EntryKind.FILE,
                        )
                    finally:
                        fileobj.close()
        except UnsafeTarMemberError as e:
            raise WorkspaceArchiveWriteError(
                path=archive_path,
                context={"member": e.member, "reason": e.reason},
                cause=e,
            ) from e
        except ArchiveResourceLimitError as e:
            raise WorkspaceArchiveWriteError(
                path=archive_path,
                context=_archive_resource_limit_context(e),
                cause=e,
            ) from e
        except (tarfile.TarError, OSError) as e:
            raise WorkspaceArchiveWriteError(path=archive_path, cause=e) from e

    async def extract_zip_archive(
        self,
        *,
        archive_path: Path,
        destination_root: Path,
        data: io.IOBase,
        archive_limits: SandboxArchiveLimits | None = None,
    ) -> None:
        child_entry_cache: dict[Path, dict[str, EntryKind]] = {}
        try:
            with zipfile_compatible_stream(data) as zip_data:
                with zipfile.ZipFile(zip_data) as archive:
                    validate_zipfile(archive, archive_limits=archive_limits)
                    for member in archive.infolist():
                        rel_path = safe_zip_member_rel_path(member)
                        if rel_path is None:
                            continue

                        await self._ensure_no_symlink_extract_parents(
                            destination_root=destination_root,
                            rel_path=rel_path,
                            member_name=member.filename,
                            error_type="zip",
                            child_entry_cache=child_entry_cache,
                        )
                        dest = destination_root / rel_path
                        if member.is_dir():
                            await self._mkdir(dest)
                            self._record_extract_entry(
                                child_entry_cache=child_entry_cache,
                                destination_root=destination_root,
                                path=dest,
                                kind=EntryKind.DIRECTORY,
                            )
                            continue

                        await self._mkdir(dest.parent)
                        self._record_extract_entry(
                            child_entry_cache=child_entry_cache,
                            destination_root=destination_root,
                            path=dest.parent,
                            kind=EntryKind.DIRECTORY,
                        )
                        with archive.open(member, mode="r") as member_data:
                            await self._write(dest, cast(io.IOBase, member_data))
                        self._record_extract_entry(
                            child_entry_cache=child_entry_cache,
                            destination_root=destination_root,
                            path=dest,
                            kind=EntryKind.FILE,
                        )
        except UnsafeZipMemberError as e:
            raise WorkspaceArchiveWriteError(
                path=archive_path,
                context={"member": e.member, "reason": e.reason},
                cause=e,
            ) from e
        except ArchiveResourceLimitError as e:
            raise WorkspaceArchiveWriteError(
                path=archive_path,
                context=_archive_resource_limit_context(e),
                cause=e,
            ) from e
        except ValueError as e:
            raise WorkspaceArchiveWriteError(path=archive_path, cause=e) from e
        except (zipfile.BadZipFile, OSError) as e:
            raise WorkspaceArchiveWriteError(path=archive_path, cause=e) from e

    async def _ensure_no_symlink_extract_parents(
        self,
        *,
        destination_root: Path,
        rel_path: Path,
        member_name: str,
        error_type: Literal["tar", "zip"],
        child_entry_cache: dict[Path, dict[str, EntryKind]],
    ) -> None:
        symlink_component = await self._find_symlink_component(
            base_dir=destination_root,
            rel_path=rel_path,
            child_entry_cache=child_entry_cache,
        )
        if symlink_component is None:
            return

        reason = f"symlink in parent path: {symlink_component.as_posix()}"
        if error_type == "tar":
            raise UnsafeTarMemberError(member=member_name, reason=reason)
        raise UnsafeZipMemberError(member=member_name, reason=reason)

    async def _find_symlink_component(
        self,
        *,
        base_dir: Path,
        rel_path: Path,
        child_entry_cache: dict[Path, dict[str, EntryKind]],
    ) -> Path | None:
        current_dir = base_dir
        traversed = Path()

        for part in rel_path.parts:
            entry_kind = await self._lookup_child_entry_kind(
                current_dir,
                part,
                child_entry_cache=child_entry_cache,
            )
            if entry_kind is None:
                return None

            traversed /= part
            if entry_kind == EntryKind.SYMLINK:
                return traversed

            current_dir = current_dir / part

        return None

    async def _lookup_child_entry_kind(
        self,
        parent_dir: Path,
        child_name: str,
        *,
        child_entry_cache: dict[Path, dict[str, EntryKind]],
    ) -> EntryKind | None:
        cached_entries = child_entry_cache.get(parent_dir)
        if cached_entries is None:
            try:
                entries = await self._ls(parent_dir)
            except ExecNonZeroError:
                return None
            cached_entries = {Path(entry.path).name: entry.kind for entry in entries}
            child_entry_cache[parent_dir] = cached_entries

        return cached_entries.get(child_name)

    @staticmethod
    def _record_extract_entry(
        *,
        child_entry_cache: dict[Path, dict[str, EntryKind]],
        destination_root: Path,
        path: Path,
        kind: EntryKind,
    ) -> None:
        try:
            rel_path = path.relative_to(destination_root)
        except ValueError:
            return

        if not rel_path.parts:
            return

        current_dir = destination_root
        for index, part in enumerate(rel_path.parts):
            child_kind = kind if index == len(rel_path.parts) - 1 else EntryKind.DIRECTORY
            cached_entries = child_entry_cache.get(current_dir)
            if cached_entries is not None:
                cached_entries[part] = child_kind
            current_dir = current_dir / part


def _supports_zip_random_access(stream: io.IOBase) -> bool:
    try:
        position = stream.tell()
        stream.seek(position, io.SEEK_SET)
    except (AttributeError, OSError, TypeError, ValueError):
        return False
    return True


@contextmanager
def zipfile_compatible_stream(stream: io.IOBase) -> Iterator[io.IOBase]:
    if _supports_zip_random_access(stream):
        yield _ZipFileStreamAdapter(stream)
        return

    spool = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode="w+b")
    try:
        shutil.copyfileobj(stream, spool)
        spool.seek(0)
        yield _ZipFileStreamAdapter(cast(io.IOBase, spool))
    finally:
        spool.close()


def safe_zip_member_rel_path(member: zipfile.ZipInfo) -> Path | None:
    if member.filename in ("", ".", "./"):
        return None

    windows_path = PureWindowsPath(member.filename)
    if windows_path.drive:
        raise UnsafeZipMemberError(member=member.filename, reason="windows drive path")
    if "\\" in member.filename:
        raise UnsafeZipMemberError(member=member.filename, reason="windows path separator")

    rel = PurePosixPath(member.filename)
    if rel.is_absolute():
        raise UnsafeZipMemberError(member=member.filename, reason="absolute path")
    if ".." in rel.parts:
        raise UnsafeZipMemberError(member=member.filename, reason="parent traversal")

    mode = (member.external_attr >> 16) & 0o170000
    if mode == 0o120000:
        raise UnsafeZipMemberError(member=member.filename, reason="link member not allowed")

    return Path(*rel.parts)


def _archive_resource_limit_context(error: ArchiveResourceLimitError) -> dict[str, object]:
    context: dict[str, object] = {
        "reason": error.reason,
        "limit": error.limit,
        "actual": error.actual,
    }
    if error.member is not None:
        context["member"] = error.member
    return context


def _check_archive_member_count(
    *,
    count: int,
    member: str,
    archive_limits: SandboxArchiveLimits | None,
) -> None:
    if archive_limits is None or archive_limits.max_members is None:
        return

    if count > archive_limits.max_members:
        raise ArchiveResourceLimitError(
            reason="archive member count exceeds limit",
            limit=archive_limits.max_members,
            actual=count,
            member=member,
        )


def _check_archive_extracted_bytes(
    *,
    total: int,
    member: str,
    archive_limits: SandboxArchiveLimits | None,
) -> None:
    if archive_limits is None or archive_limits.max_extracted_bytes is None:
        return

    if total > archive_limits.max_extracted_bytes:
        raise ArchiveResourceLimitError(
            reason="archive extracted size exceeds limit",
            limit=archive_limits.max_extracted_bytes,
            actual=total,
            member=member,
        )


def validate_tar_archive_for_extraction(
    archive: tarfile.TarFile,
    *,
    archive_limits: SandboxArchiveLimits | None = None,
) -> None:
    members_by_rel_path: dict[Path, tarfile.TarInfo] = {}
    descendant_by_parent_path: dict[Path, tarfile.TarInfo] = {}
    member_count = 0
    extracted_bytes = 0

    for member in archive:
        rel_path = safe_tar_member_rel_path(member)
        if rel_path is None:
            continue

        member_count += 1
        _check_archive_member_count(
            count=member_count,
            member=member.name,
            archive_limits=archive_limits,
        )
        if member.isreg():
            extracted_bytes += max(member.size, 0)
            _check_archive_extracted_bytes(
                total=extracted_bytes,
                member=member.name,
                archive_limits=archive_limits,
            )

        previous = members_by_rel_path.get(rel_path)
        if previous is not None and not (previous.isdir() and member.isdir()):
            raise UnsafeTarMemberError(
                member=member.name,
                reason=f"duplicate archive path: {rel_path.as_posix()}",
            )

        for parent in rel_path.parents:
            if parent == Path():
                break
            parent_member = members_by_rel_path.get(parent)
            if parent_member is not None and not parent_member.isdir():
                raise UnsafeTarMemberError(
                    member=member.name,
                    reason=f"archive path descends through non-directory: {parent.as_posix()}",
                )

        if not member.isdir():
            descendant = descendant_by_parent_path.get(rel_path)
            if descendant is not None:
                raise UnsafeTarMemberError(
                    member=descendant.name,
                    reason=f"archive path descends through non-directory: {rel_path.as_posix()}",
                )

        members_by_rel_path[rel_path] = member
        for parent in rel_path.parents:
            if parent == Path():
                break
            descendant_by_parent_path.setdefault(parent, member)


def validate_zipfile(
    archive: zipfile.ZipFile,
    *,
    archive_limits: SandboxArchiveLimits | None = None,
) -> None:
    members_by_rel_path: dict[Path, zipfile.ZipInfo] = {}
    members: list[tuple[zipfile.ZipInfo, Path]] = []
    extracted_bytes = 0

    for member in archive.infolist():
        rel_path = safe_zip_member_rel_path(member)
        if rel_path is None:
            continue

        _check_archive_member_count(
            count=len(members) + 1,
            member=member.filename,
            archive_limits=archive_limits,
        )
        extracted_bytes += max(member.file_size, 0)
        _check_archive_extracted_bytes(
            total=extracted_bytes,
            member=member.filename,
            archive_limits=archive_limits,
        )

        previous = members_by_rel_path.get(rel_path)
        if previous is not None and not (previous.is_dir() and member.is_dir()):
            raise UnsafeZipMemberError(
                member=member.filename,
                reason=f"duplicate archive path: {rel_path.as_posix()}",
            )
        members_by_rel_path[rel_path] = member
        members.append((member, rel_path))

    for member, rel_path in members:
        for parent in rel_path.parents:
            if parent == Path():
                break
            parent_member = members_by_rel_path.get(parent)
            if parent_member is not None and not parent_member.is_dir():
                raise UnsafeZipMemberError(
                    member=member.filename,
                    reason=f"archive path descends through non-directory: {parent.as_posix()}",
                )


class _ZipFileStreamAdapter(io.IOBase):
    # Python 3.10's zipfile._SharedFile reads `file.seekable` directly, so this
    # adapter keeps ZIP-compatible random-access streams working across versions.
    def __init__(self, stream: io.IOBase) -> None:
        self._stream = stream

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return int(self._stream.tell())

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        return int(self._stream.seek(offset, whence))

    def read(self, size: int = -1) -> bytes:
        data = self._stream.read(size)
        if isinstance(data, bytes):
            return data
        raise TypeError(f"expected bytes from wrapped stream, got {type(data).__name__}")

    def close(self) -> None:
        return
