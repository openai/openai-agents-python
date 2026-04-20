from __future__ import annotations

import copy
import io
import os
import shutil
import tarfile
import tempfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath


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


def safe_tar_member_rel_path(
    member: tarfile.TarInfo,
    *,
    allow_symlinks: bool = False,
) -> Path | None:
    """Validate one tar member's path and return a non-root relative path."""

    if member.name in ("", ".", "./"):
        _validate_archive_root_member(member)
        return None
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


def strip_tar_member_prefix(data: io.IOBase, *, prefix: str | Path) -> io.IOBase:
    """Return a seekable tar stream after replacing a leading member prefix with `.`.

    For example, Docker archives a workspace copied to `/tmp/stage/workspace`
    as `workspace/...`; portable workspace snapshots should store the same
    files as `.` and `...`, independent of the source backend's root name.
    """

    prefix_rel = _normalize_rel(prefix)
    if prefix_rel == Path():
        raise ValueError("tar member prefix must not be empty")

    out = tempfile.TemporaryFile()
    try:
        with data:
            with tarfile.open(fileobj=data, mode="r|*") as src:
                with tarfile.open(fileobj=out, mode="w|") as dst:
                    for member in src:
                        rel_path = safe_tar_member_rel_path(
                            member,
                            allow_symlinks=True,
                        )
                        if rel_path is None:
                            stripped_name = "."
                        elif rel_path == prefix_rel:
                            stripped_name = "."
                        elif rel_path.parts[: len(prefix_rel.parts)] == prefix_rel.parts:
                            stripped_name = Path(
                                *rel_path.parts[len(prefix_rel.parts) :]
                            ).as_posix()
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
                        if member.isreg():
                            fileobj = src.extractfile(member)
                            if fileobj is None:
                                raise UnsafeTarMemberError(
                                    member=member.name,
                                    reason="missing file payload",
                                )
                            try:
                                dst.addfile(rewritten, fileobj)
                            finally:
                                fileobj.close()
                        else:
                            dst.addfile(rewritten)

        out.seek(0)
        with tarfile.open(fileobj=out, mode="r:*") as tar:
            validate_tarfile(tar)
        out.seek(0)
        return out
    except Exception:
        out.close()
        raise


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

    raw_parts = [p for p in Path(member_name).parts if p not in ("", ".")]
    if raw_parts[:1] == ["/"]:
        raw_parts = raw_parts[1:]
    if not raw_parts:
        rel_variants = [Path()]
    else:
        rel_variants = [Path(*raw_parts)]
        if root_name and raw_parts and raw_parts[0] == root_name:
            rel_variants.append(Path(*raw_parts[1:]))

    prefixes = [_normalize_rel(p) for p in skip_rel_paths]
    return any(_is_within(rel, prefix) for rel in rel_variants for prefix in prefixes)


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
    reject_symlink_rel_paths: Iterable[str | Path] = (),
    skip_rel_paths: Iterable[str | Path] = (),
    root_name: str | None = None,
) -> None:
    """Validate a workspace tar before handing it to a local or remote extractor.

    Symlink entries are allowed because normal development workspaces contain them
    (for example, Python virtual environments). To keep extraction contained, no
    other archive member may be nested underneath a symlink entry from the archive.
    Symlink targets are preserved as link metadata instead of being followed.
    Local extraction creates symlinks only after directories and regular files have
    been restored.
    """

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
        rel_path = safe_tar_member_rel_path(member, allow_symlinks=True)
        if rel_path is None:
            continue

        previous = members_by_rel_path.get(rel_path)
        if previous is not None and not (previous.isdir() and member.isdir()):
            raise UnsafeTarMemberError(
                member=member.name,
                reason=f"duplicate archive path: {rel_path.as_posix()}",
            )
        members_by_rel_path[rel_path] = member

        if member.issym():
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


def validate_tar_bytes(
    raw: bytes,
    *,
    reject_symlink_rel_paths: Iterable[str | Path] = (),
    skip_rel_paths: Iterable[str | Path] = (),
    root_name: str | None = None,
) -> None:
    """Validate raw workspace tar bytes with the shared safe tar policy."""

    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tar:
            validate_tarfile(
                tar,
                reject_symlink_rel_paths=reject_symlink_rel_paths,
                skip_rel_paths=skip_rel_paths,
                root_name=root_name,
            )
    except UnsafeTarMemberError:
        raise
    except (tarfile.TarError, OSError) as e:
        raise UnsafeTarMemberError(member="<tar>", reason="invalid tar stream") from e


def safe_extract_tarfile(tar: tarfile.TarFile, *, root: Path) -> None:
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
    validate_tarfile(tar)

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
