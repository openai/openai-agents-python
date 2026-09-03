from __future__ import annotations

import copy
import io
import os
import posixpath
import shutil
import tarfile
import tempfile
from collections.abc import Iterable
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import IO, cast


class UnsafeTarMemberError(ValueError):
    def __init__(self, *, member: str, reason: str) -> None:
        super().__init__(f"unsafe tar member {member!r}: {reason}")
        self.member = member
        self.reason = reason


def _validate_archive_root_member(member: tarfile.TarInfo) -> None:
    if member.isdir():
        return
    if member.issym():
        raise UnsafeTarMemberError(member=member.name, reason="archive root symlink")
    if member.islnk():
        raise UnsafeTarMemberError(member=member.name, reason="archive root hardlink")
    raise UnsafeTarMemberError(member=member.name, reason="archive root member must be directory")


def _raise_if_windows_member_path(member_name: str) -> None:
    windows_path = PureWindowsPath(member_name)
    if windows_path.drive:
        raise UnsafeTarMemberError(member=member_name, reason="windows drive path")
    if "\\" in member_name:
        raise UnsafeTarMemberError(member=member_name, reason="windows path separator")


def _normalize_posix_path_without_root(path: PurePosixPath) -> tuple[str, ...] | None:
    normalized: list[str] = []
    for part in path.parts:
        if part in ("", ".", "/"):
            continue
        if part == "..":
            if not normalized:
                return None
            normalized.pop()
            continue
        normalized.append(part)
    return tuple(normalized)


def _validate_symlink_target(
    member: tarfile.TarInfo,
    *,
    rel_path: Path,
    allow_external_symlink_targets: bool,
) -> None:
    if not member.issym() or allow_external_symlink_targets:
        return

    target = PurePosixPath(member.linkname)
    if target.is_absolute():
        raise UnsafeTarMemberError(
            member=member.name,
            reason=f"absolute symlink target not allowed: {member.linkname}",
        )

    member_parent = PurePosixPath(rel_path.as_posix()).parent
    normalized = _normalize_posix_path_without_root(member_parent / target)
    if normalized is None:
        raise UnsafeTarMemberError(
            member=member.name,
            reason=f"symlink target escapes archive root: {member.linkname}",
        )


def safe_tar_member_rel_path(
    member: tarfile.TarInfo,
    *,
    allow_symlinks: bool = False,
) -> Path | None:
    """Validate one tar member's path and return a non-root relative path."""

    if member.name in ("", ".", "./"):
        _validate_archive_root_member(member)
        return None
    _raise_if_windows_member_path(member.name)
    rel = PurePosixPath(member.name)
    if rel.is_absolute():
        raise UnsafeTarMemberError(member=member.name, reason="absolute path")
    if ".." in rel.parts:
        raise UnsafeTarMemberError(member=member.name, reason="parent traversal")
    if member.issym() and not allow_symlinks:
        raise UnsafeTarMemberError(member=member.name, reason="symlink member not allowed")
    if member.islnk():
        raise UnsafeTarMemberError(member=member.name, reason="hardlink member not allowed")
    if not (member.isdir() or member.isreg() or (allow_symlinks and member.issym())):
        raise UnsafeTarMemberError(member=member.name, reason="unsupported member type")
    return Path(*rel.parts)


def strip_tar_member_prefix(
    data: io.IOBase,
    *,
    prefix: str | Path,
    relativize_symlinks_under: str | PurePath | None = None,
) -> io.IOBase:
    """Return a seekable tar stream after replacing a leading member prefix with `.`.

    For example, Docker archives a workspace copied to `/tmp/stage/workspace`
    as `workspace/...`; portable workspace snapshots should store the same
    files as `.` and `...`, independent of the source backend's root name.

    The rewritten archive only contains members that the strict hydrate extractor
    accepts. Archivers such as Docker's represent a second hardlinked path as a
    hardlink member and keep FIFOs and device nodes, and ordinary workspaces contain
    them (``uv`` and ``pnpm`` hardlink installed packages, dev servers leave FIFOs
    behind). Hardlink members are stored as regular files with the target's payload (read
    back from the rewritten archive, so the source is still streamed once),
    FIFOs and device nodes are dropped, and when `relativize_symlinks_under` names
    the workspace root, an absolute symlink target under that root becomes relative
    to the link's own directory so it restores under any root.
    """

    prefix_rel = _normalize_rel(prefix)
    if prefix_rel == Path():
        raise ValueError("tar member prefix must not be empty")
    symlink_root: PurePosixPath | None = None
    if relativize_symlinks_under is not None:
        symlink_root = PurePosixPath(
            relativize_symlinks_under.as_posix()
            if isinstance(relativize_symlinks_under, PurePath)
            else relativize_symlinks_under
        )

    out = tempfile.TemporaryFile()
    try:
        # Stream the source once. A hardlink member carries no payload of its own, so its
        # target's bytes are read back from the rewritten archive being written (recorded by
        # original member name), which keeps temp usage at one archive instead of two.
        written_payloads: dict[str, tuple[int, int]] = {}
        with data, tarfile.open(fileobj=data, mode="r|*") as src:
            with tarfile.open(fileobj=out, mode="w") as dst:
                for member in src:
                    if member.isfifo() or member.ischr() or member.isblk():
                        continue
                    payload: tuple[int, int] | None = None
                    if member.islnk():
                        payload = written_payloads.get(member.linkname)
                        if payload is None:
                            reason = (
                                f"hardlink target is not a file in the archive: {member.linkname}"
                            )
                            raise UnsafeTarMemberError(member=member.name, reason=reason)
                        member = copy.copy(member)
                        member.type = tarfile.REGTYPE
                        member.linkname = ""
                        member.size = payload[1]
                    rel_path = safe_tar_member_rel_path(
                        member,
                        allow_symlinks=True,
                    )
                    if rel_path is None:
                        stripped_name = "."
                    elif rel_path == prefix_rel:
                        stripped_name = "."
                    elif rel_path.parts[: len(prefix_rel.parts)] == prefix_rel.parts:
                        stripped_name = Path(*rel_path.parts[len(prefix_rel.parts) :]).as_posix()
                    else:
                        reason = f"member does not start with prefix: {prefix_rel.as_posix()}"
                        raise UnsafeTarMemberError(
                            member=member.name,
                            reason=reason,
                        )

                    rewritten = copy.copy(member)
                    rewritten.name = stripped_name
                    rewritten.pax_headers = dict(member.pax_headers)
                    rewritten.pax_headers.pop("path", None)
                    if rewritten.issym() and symlink_root is not None:
                        rewritten.linkname = _relative_symlink_target(
                            rewritten.linkname,
                            link_name=stripped_name,
                            root=symlink_root,
                        )
                    if not rewritten.isreg():
                        dst.addfile(rewritten)
                        continue
                    if payload is not None:
                        fileobj: IO[bytes] = cast(IO[bytes], _ArchivePayloadReader(out, *payload))
                    else:
                        extracted = src.extractfile(member)
                        if extracted is None:
                            raise UnsafeTarMemberError(
                                member=member.name,
                                reason="missing file payload",
                            )
                        fileobj = extracted
                    try:
                        dst.addfile(rewritten, fileobj)
                    finally:
                        fileobj.close()
                    padded = -(-rewritten.size // tarfile.BLOCKSIZE) * tarfile.BLOCKSIZE
                    written_payloads[member.name] = (dst.offset - padded, rewritten.size)

        out.seek(0)
        with tarfile.open(fileobj=out, mode="r:*") as tar:
            validate_tarfile(tar)
        out.seek(0)
        return cast(io.IOBase, out)
    except Exception:
        out.close()
        raise


class _ArchivePayloadReader(io.RawIOBase):
    """Read a member payload back from the archive file that is still being written.

    Every read seeks to the payload and then restores the writer's position, so the reader
    can be interleaved with `TarFile.addfile()` writing to the same file object.
    """

    def __init__(self, archive: IO[bytes], start: int, size: int) -> None:
        super().__init__()
        self._archive = archive
        self._position = start
        self._end = start + size

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        remaining = self._end - self._position
        if size is None or size < 0 or size > remaining:
            size = remaining
        if size <= 0:
            return b""
        write_position = self._archive.tell()
        try:
            self._archive.seek(self._position)
            data = self._archive.read(size)
        finally:
            self._archive.seek(write_position)
        self._position += len(data)
        return data

    def close(self) -> None:
        # The archive stays open for the writer; only this view closes.
        io.RawIOBase.close(self)


def _relative_symlink_target(linkname: str, *, link_name: str, root: PurePosixPath) -> str:
    """Make an absolute symlink target under `root` relative to the link's directory."""

    target = PurePosixPath(linkname)
    if not target.is_absolute():
        return linkname
    normalized = PurePosixPath(posixpath.normpath(linkname))
    try:
        target_rel = normalized.relative_to(root)
    except ValueError:
        return linkname
    link_dir = PurePosixPath(link_name).parent
    return posixpath.relpath(target_rel.as_posix() or ".", start=link_dir.as_posix())


def _normalize_rel(prefix: str | Path) -> Path:
    rel = prefix if isinstance(prefix, Path) else Path(prefix)
    posix = rel.as_posix()
    parts = [p for p in Path(posix).parts if p not in ("", ".")]
    if parts[:1] == ["/"]:
        parts = parts[1:]
    return Path(*parts)


def _is_within(path: Path, prefix: Path) -> bool:
    if prefix == Path():
        return True
    if path == prefix:
        return True
    return path.parts[: len(prefix.parts)] == prefix.parts


def _tar_member_rel_variants(member_name: str, root_name: str | None) -> list[Path]:
    raw_parts = [p for p in Path(member_name).parts if p not in ("", ".")]
    if raw_parts[:1] == ["/"]:
        raw_parts = raw_parts[1:]
    if not raw_parts:
        return [Path()]

    variants = [Path(*raw_parts)]
    if root_name and raw_parts[0] == root_name:
        variants.append(Path(*raw_parts[1:]))
    return variants


def should_skip_tar_member(
    member_name: str,
    *,
    skip_rel_paths: Iterable[str | Path],
    root_name: str | None,
) -> bool:
    """
    Decide whether a tar member should be excluded based on workspace-relative prefixes.

    `member_name` is the raw name from the tar, which may include `.` or the workspace root
    directory name depending on how the tar was produced.
    """

    rel_variants = _tar_member_rel_variants(member_name, root_name)
    prefixes = [_normalize_rel(p) for p in skip_rel_paths]
    return any(_is_within(rel, prefix) for rel in rel_variants for prefix in prefixes)


def _restored_regular_file_mode(mode: int) -> int:
    """Return the permission bits to restore for an extracted regular file.

    This mirrors the mode policy of the standard library's ``tarfile`` ``data`` filter, which
    this extractor replaces: setuid, setgid, sticky and group/other write bits are dropped,
    execute bits are kept only when the owner had them, and the owner is always left able to
    read and write the file.
    """

    restored = mode & 0o755
    if not restored & 0o100:
        restored &= ~0o111
    return restored | 0o600


def _ensure_no_symlink_parents(*, root: Path, dest: Path, check_leaf: bool = True) -> None:
    """
    Ensure that no existing parent directory in `dest` is a symlink.

    This helps prevent writing outside `root` via pre-existing symlink components.
    """

    root_resolved = root.resolve()
    path_to_resolve = dest if check_leaf else dest.parent
    dest_resolved = path_to_resolve.resolve()
    if not (dest_resolved == root_resolved or dest_resolved.is_relative_to(root_resolved)):
        raise UnsafeTarMemberError(
            member=dest.as_posix(), reason="path escapes root after resolution"
        )

    rel = dest.relative_to(root)
    cur = root
    for part in rel.parts[:-1]:
        cur = cur / part
        if cur.exists() and cur.is_symlink():
            raise UnsafeTarMemberError(member=str(rel.as_posix()), reason="symlink in parent path")


def validate_tarfile(
    tar: tarfile.TarFile,
    *,
    reject_rel_paths: Iterable[str | Path] = (),
    reject_symlink_rel_paths: Iterable[str | Path] = (),
    skip_rel_paths: Iterable[str | Path] = (),
    root_name: str | None = None,
    allow_symlinks: bool = True,
    allow_external_symlink_targets: bool = True,
) -> None:
    """Validate a workspace tar before handing it to a local or remote extractor.

    Symlink entries are allowed because normal development workspaces contain them
    (for example, Python virtual environments). To keep extraction contained, no
    other archive member may be nested underneath a symlink entry from the archive.
    Symlink targets are preserved as link metadata instead of being followed.
    Local extraction creates symlinks only after directories and regular files have
    been restored.
    """

    rejected_rel_paths = {_normalize_rel(path) for path in reject_rel_paths}
    rejected_symlink_rel_paths = {_normalize_rel(path) for path in reject_symlink_rel_paths}
    members_by_rel_path: dict[Path, tarfile.TarInfo] = {}
    symlink_rel_paths: set[Path] = set()
    members: list[tuple[tarfile.TarInfo, Path]] = []

    for member in tar.getmembers():
        if should_skip_tar_member(
            member.name,
            skip_rel_paths=skip_rel_paths,
            root_name=root_name,
        ):
            continue
        rel_path = safe_tar_member_rel_path(member, allow_symlinks=allow_symlinks)
        if rel_path is None:
            continue
        rel_variants = _tar_member_rel_variants(member.name, root_name)
        for rejected_path in rejected_rel_paths:
            if any(
                _is_within(variant, rejected_path)
                or (not member.isdir() and _is_within(rejected_path, variant))
                for variant in rel_variants
            ):
                raise UnsafeTarMemberError(
                    member=member.name,
                    reason=f"archive member overlaps protected path: {rejected_path.as_posix()}",
                )

        previous = members_by_rel_path.get(rel_path)
        if previous is not None and not (previous.isdir() and member.isdir()):
            raise UnsafeTarMemberError(
                member=member.name,
                reason=f"duplicate archive path: {rel_path.as_posix()}",
            )
        members_by_rel_path[rel_path] = member

        if member.issym():
            _validate_symlink_target(
                member,
                rel_path=rel_path,
                allow_external_symlink_targets=allow_external_symlink_targets,
            )
            if rel_path in rejected_symlink_rel_paths:
                raise UnsafeTarMemberError(
                    member=member.name,
                    reason=f"symlink member not allowed: {rel_path.as_posix()}",
                )
            symlink_rel_paths.add(rel_path)
        members.append((member, rel_path))

    for member, rel_path in members:
        for parent in rel_path.parents:
            if parent == Path():
                break
            if parent in symlink_rel_paths:
                raise UnsafeTarMemberError(
                    member=member.name,
                    reason=f"archive path descends through symlink: {parent.as_posix()}",
                )
            parent_member = members_by_rel_path.get(parent)
            if parent_member is not None and not parent_member.isdir():
                raise UnsafeTarMemberError(
                    member=member.name,
                    reason=f"archive path descends through non-directory: {parent.as_posix()}",
                )


def validate_tar_bytes(
    raw: bytes,
    *,
    reject_rel_paths: Iterable[str | Path] = (),
    reject_symlink_rel_paths: Iterable[str | Path] = (),
    skip_rel_paths: Iterable[str | Path] = (),
    root_name: str | None = None,
    allow_external_symlink_targets: bool = True,
) -> None:
    """Validate raw workspace tar bytes with the shared safe tar policy."""

    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tar:
            validate_tarfile(
                tar,
                reject_rel_paths=reject_rel_paths,
                reject_symlink_rel_paths=reject_symlink_rel_paths,
                skip_rel_paths=skip_rel_paths,
                root_name=root_name,
                allow_external_symlink_targets=allow_external_symlink_targets,
            )
    except UnsafeTarMemberError:
        raise
    except (tarfile.TarError, OSError) as e:
        raise UnsafeTarMemberError(member="<tar>", reason="invalid tar stream") from e


def safe_extract_tarfile(
    tar: tarfile.TarFile,
    *,
    root: Path,
    allow_external_symlink_targets: bool = True,
) -> None:
    """
    Safely extract a tar archive into `root`.

    This rejects:
    - absolute member paths
    - paths containing `..`
    - hardlinks
    - non-regular-file and non-directory members (devices, fifos, etc.)
    - archive members nested underneath archive symlink members

    It also ensures extraction doesn't traverse through existing symlink parents
    and creates archive symlinks only after directories and regular files.
    """

    root.mkdir(parents=True, exist_ok=True)
    root_resolved = root.resolve()

    members = tar.getmembers()
    validate_tarfile(
        tar,
        allow_external_symlink_targets=allow_external_symlink_targets,
    )

    def _prepare_replaceable_leaf(*, dest: Path, rel_path: Path, name: str) -> None:
        _ensure_no_symlink_parents(root=root_resolved, dest=dest, check_leaf=False)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_dir() and not dest.is_symlink():
            raise UnsafeTarMemberError(
                member=name,
                reason=f"destination directory already exists: {rel_path.as_posix()}",
            )
        try:
            dest.unlink()
        except FileNotFoundError:
            pass

    def _prepare_directory_leaf(*, dest: Path) -> None:
        _ensure_no_symlink_parents(root=root_resolved, dest=dest, check_leaf=False)
        if dest.is_symlink() or (dest.exists() and not dest.is_dir()):
            dest.unlink()

    def _write_file(member: tarfile.TarInfo, *, dest: Path, rel_path: Path, name: str) -> None:
        fileobj = tar.extractfile(member)
        if fileobj is None:
            raise UnsafeTarMemberError(member=name, reason="missing file payload")

        _prepare_replaceable_leaf(dest=dest, rel_path=rel_path, name=name)

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(dest, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(fileobj, out)
                out.flush()
                if hasattr(os, "fchmod"):
                    # Restore the archived permissions so a workspace snapshot round-trip keeps
                    # executable scripts executable. This runs on the still-open descriptor,
                    # after the payload is written and flushed: the file keeps its private
                    # creation mode while it holds partial data, and a failed copy leaves the
                    # partial file at 0o600 instead of its final readable/executable mode.
                    os.fchmod(out.fileno(), _restored_regular_file_mode(member.mode))
        finally:
            try:
                fileobj.close()
            except Exception:
                pass

    for member in members:
        name = member.name
        rel_path = safe_tar_member_rel_path(member, allow_symlinks=True)
        if rel_path is None:
            continue
        if member.issym():
            continue

        dest = root_resolved / rel_path

        if member.isdir():
            _prepare_directory_leaf(dest=dest)
            dest.mkdir(parents=True, exist_ok=True)
            continue

        _write_file(member, dest=dest, rel_path=rel_path, name=name)

    for member in members:
        if not member.issym():
            continue
        rel_path = safe_tar_member_rel_path(member, allow_symlinks=True)
        if rel_path is None:
            continue
        dest = root_resolved / rel_path
        _prepare_replaceable_leaf(dest=dest, rel_path=rel_path, name=member.name)
        os.symlink(member.linkname, dest)
