from __future__ import annotations

import copy
import io
import os
import shutil
import tarfile
import tempfile
from collections.abc import Iterable
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import cast


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

    The strict hydrate extractor refuses FIFOs, device nodes, and absolute symlink
    targets, and a staged workspace copy (`cp -R`) legitimately carries the first two
    kinds and absolute links into the workspace. FIFOs and device nodes are dropped, and
    when `relativize_symlinks_under` names the workspace root, an absolute symlink target
    under it is rebased onto the link's own directory with its components kept verbatim.
    Only *simple* targets are rebased: every component is a plain name and every directory
    the relative target walks through (the link's own parents and the target's parents) is
    a directory member of the archive, with a regular-file or directory leaf. A target
    whose meaning depends on how another symlink resolves (``alias/../x``), or that walks
    through a directory the archive does not create, is left absolute so the strict
    hydrate validation refuses it rather than this rewrite guessing where it lands.
    """

    prefix_rel = _normalize_rel(prefix)
    if prefix_rel == Path():
        raise ValueError("tar member prefix must not be empty")
    symlink_root: str | None = None
    if relativize_symlinks_under is not None:
        symlink_root = (
            relativize_symlinks_under.as_posix()
            if isinstance(relativize_symlinks_under, PurePath)
            else relativize_symlinks_under
        )

    staged = tempfile.TemporaryFile()
    candidates: dict[str, str] = {}
    try:
        with data:
            with tarfile.open(fileobj=data, mode="r|*") as src:
                with tarfile.open(fileobj=staged, mode="w|") as dst:
                    for member in src:
                        if member.isfifo() or member.ischr() or member.isblk():
                            continue
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
                        if rewritten.issym() and symlink_root is not None:
                            rebased = rebase_symlink_target(
                                rewritten.linkname, link_name=stripped_name, root=symlink_root
                            )
                            if rebased != rewritten.linkname:
                                candidates[stripped_name] = rebased
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

        # Second pass: now that every member is known, rewrite only the simple targets
        # whose every component the archive itself establishes; the rest stay absolute.
        staged.seek(0)
        out = tempfile.TemporaryFile()
        try:
            with tarfile.open(fileobj=staged, mode="r:*") as src:
                members = {member.name: member for member in src.getmembers()}
                with tarfile.open(fileobj=out, mode="w|") as dst:
                    for member in src.getmembers():
                        rewritten = member
                        candidate = candidates.get(member.name)
                        if candidate is not None and symlink_root is not None:
                            parts = _simple_rebase_components(member.linkname, root=symlink_root)
                            if parts is not None and _archive_establishes(
                                parts, link_name=member.name, members=members
                            ):
                                rewritten = copy.copy(member)
                                rewritten.linkname = candidate
                                # A long source target lives in a PAX "linkpath" record
                                # that would otherwise override the rewritten linkname.
                                rewritten.pax_headers = dict(member.pax_headers)
                                rewritten.pax_headers.pop("linkpath", None)
                        if member.isreg():
                            fileobj = src.extractfile(member)
                            if fileobj is None:
                                raise UnsafeTarMemberError(
                                    member=member.name, reason="missing file payload"
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
            return cast(io.IOBase, out)
        except Exception:
            out.close()
            raise
    finally:
        staged.close()


def _simple_rebase_components(linkname: str, *, root: str) -> tuple[str, ...] | None:
    """Return the root-relative components of a *simple* absolute target under `root`.

    Simple means every component after the root is a plain name: no ``.``, ``..``, or
    empty segment. A leading ``//`` is collapsed to ``/`` and the whole separator run at
    the root boundary is consumed (``/workspace//a.txt``). Targets outside the root, and
    targets whose meaning depends on how a symlink component resolves (``alias/../x``),
    return ``None`` and are left absolute for the strict hydrate check to refuse.
    """

    if not linkname.startswith("/"):
        return None
    target = "/" + linkname.lstrip("/")
    prefix = "/" + root.strip("/")
    if target == prefix:
        rest = ""
    elif target.startswith(prefix + "/"):
        rest = target[len(prefix) :].lstrip("/")
    else:
        return None
    parts = tuple(part for part in rest.split("/") if part)
    if any(part in (".", "..") for part in parts):
        return None
    return parts


def rebase_symlink_target(linkname: str, *, link_name: str, root: str) -> str:
    """Rewrite a simple absolute symlink target under `root` as a target relative to the link.

    Only targets whose components are all plain names are rewritten (see
    `_simple_rebase_components`): ``/workspace/sub/data.txt`` from ``sub/abs_up`` becomes
    ``../sub/data.txt``. Anything that would need a symlink component resolved to know
    where it lands (``/workspace/alias/../data.txt``) is returned unchanged, so the strict
    hydrate validation refuses it as an absolute target instead of this function guessing.
    Whether the components are established by the archive itself is checked by
    `strip_tar_member_prefix`, which sees every member.
    """

    parts = _simple_rebase_components(linkname, root=root)
    if parts is None:
        return linkname
    climb = "/".join([".."] * len(PurePosixPath(link_name).parent.parts))
    rest = "/".join(parts)
    if rest and climb:
        return f"{climb}/{rest}"
    return rest or climb or "."


def _archive_establishes(
    parts: tuple[str, ...], *, link_name: str, members: dict[str, tarfile.TarInfo]
) -> bool:
    """Whether every component a rebased target walks through is an ordinary archive path.

    Hydration extracts into an existing root, so a directory the archive does not create
    could already be a symlink in the destination and redirect the restored link. Every
    directory the relative target climbs out of (the link's own parents) and every
    directory it descends into must therefore be a directory member of the archive, and
    the leaf must be a regular file or directory member: a symlink leaf would make the
    result depend on another link, which is exactly what this rewrite refuses to model.
    """

    if not parts:
        return False
    link_parents = PurePosixPath(link_name).parent.parts
    for depth in range(1, len(link_parents) + 1):
        parent = members.get("/".join(link_parents[:depth]))
        if parent is None or not parent.isdir():
            return False
    for depth in range(1, len(parts)):
        intermediate = members.get("/".join(parts[:depth]))
        if intermediate is None or not intermediate.isdir():
            return False
    leaf = members.get("/".join(parts))
    return leaf is not None and (leaf.isreg() or leaf.isdir())


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
