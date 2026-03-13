from __future__ import annotations

import os
import shutil
import tarfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath


class UnsafeTarMemberError(ValueError):
    def __init__(self, *, member: str, reason: str) -> None:
        super().__init__(f"unsafe tar member {member!r}: {reason}")
        self.member = member
        self.reason = reason


def safe_tar_member_rel_path(member: tarfile.TarInfo) -> Path | None:
    if member.name in ("", ".", "./"):
        return None
    rel = PurePosixPath(member.name)
    if rel.is_absolute():
        raise UnsafeTarMemberError(member=member.name, reason="absolute path")
    if ".." in rel.parts:
        raise UnsafeTarMemberError(member=member.name, reason="parent traversal")
    if member.issym() or member.islnk():
        raise UnsafeTarMemberError(member=member.name, reason="link member not allowed")
    if not (member.isdir() or member.isreg()):
        raise UnsafeTarMemberError(member=member.name, reason="unsupported member type")
    return Path(*rel.parts)


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


def _ensure_no_symlink_parents(*, root: Path, dest: Path) -> None:
    """
    Ensure that no existing parent directory in `dest` is a symlink.

    This helps prevent writing outside `root` via pre-existing symlink components.
    """

    root_resolved = root.resolve()
    dest_resolved = dest.resolve()
    if not (dest_resolved == root_resolved or dest_resolved.is_relative_to(root_resolved)):
        raise UnsafeTarMemberError(member=str(dest), reason="path escapes root after resolution")

    rel = dest.relative_to(root)
    cur = root
    for part in rel.parts[:-1]:
        cur = cur / part
        if cur.exists() and cur.is_symlink():
            raise UnsafeTarMemberError(member=str(rel.as_posix()), reason="symlink in parent path")


def validate_tarfile(tar: tarfile.TarFile) -> None:
    for member in tar.getmembers():
        safe_tar_member_rel_path(member)


def safe_extract_tarfile(tar: tarfile.TarFile, *, root: Path) -> None:
    """
    Safely extract a tar archive into `root`.

    This rejects:
    - absolute member paths
    - paths containing `..`
    - symlinks / hardlinks
    - non-regular-file and non-directory members (devices, fifos, etc.)

    It also ensures extraction doesn't traverse through existing symlink parents.
    """

    root.mkdir(parents=True, exist_ok=True)
    root_resolved = root.resolve()

    validate_tarfile(tar)
    for member in tar.getmembers():
        name = member.name
        rel_path = safe_tar_member_rel_path(member)
        if rel_path is None:
            continue

        dest = root_resolved / rel_path
        _ensure_no_symlink_parents(root=root_resolved, dest=dest)

        if member.isdir():
            dest.mkdir(parents=True, exist_ok=True)
            continue

        # Regular file
        fileobj = tar.extractfile(member)
        if fileobj is None:
            raise UnsafeTarMemberError(member=name, reason="missing file payload")

        dest.parent.mkdir(parents=True, exist_ok=True)
        _ensure_no_symlink_parents(root=root_resolved, dest=dest)

        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
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
