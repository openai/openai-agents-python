from __future__ import annotations

import io
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from ...run_config import SandboxArchiveLimits
from ..errors import InvalidCompressionSchemeError, WorkspaceArchiveWriteError
from .archive_extraction import WorkspaceArchiveExtractor, safe_zip_member_rel_path

if TYPE_CHECKING:
    from .base_sandbox_session import BaseSandboxSession


async def extract_archive(
    session: BaseSandboxSession,
    path: Path | str,
    data: io.IOBase,
    *,
    compression_scheme: Literal["tar", "zip"] | None = None,
    archive_limits: SandboxArchiveLimits | None = None,
) -> None:
    if archive_limits is not None:
        archive_limits.validate()

    if isinstance(path, str):
        path = Path(path)

    if compression_scheme is None:
        suffix = path.suffix.removeprefix(".")
        compression_scheme = cast(Literal["tar", "zip"], suffix) if suffix else None

    if compression_scheme is None or compression_scheme not in ["zip", "tar"]:
        raise InvalidCompressionSchemeError(path=path, scheme=compression_scheme)

    normalized_path = await session._validate_path_access(path, for_write=True)
    destination_root = normalized_path.parent

    # Materialize the archive into a local spool once because both `write()` and the
    # extraction step consume the stream, and zip extraction may require seeking.
    spool = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode="w+b")
    try:
        _copy_archive(data, spool, path=normalized_path, archive_limits=archive_limits)
        spool.seek(0)
        await session.write(normalized_path, spool)
        spool.seek(0)

        if compression_scheme == "tar":
            await session._extract_tar_archive(
                archive_path=normalized_path,
                destination_root=destination_root,
                data=spool,
                archive_limits=archive_limits,
            )
        else:
            await session._extract_zip_archive(
                archive_path=normalized_path,
                destination_root=destination_root,
                data=spool,
                archive_limits=archive_limits,
            )
    finally:
        spool.close()


async def extract_tar_archive(
    session: BaseSandboxSession,
    *,
    archive_path: Path,
    destination_root: Path,
    data: io.IOBase,
    archive_limits: SandboxArchiveLimits | None = None,
) -> None:
    extractor = _build_workspace_archive_extractor(session)
    await extractor.extract_tar_archive(
        archive_path=archive_path,
        destination_root=destination_root,
        data=data,
        archive_limits=archive_limits,
    )


async def extract_zip_archive(
    session: BaseSandboxSession,
    *,
    archive_path: Path,
    destination_root: Path,
    data: io.IOBase,
    archive_limits: SandboxArchiveLimits | None = None,
) -> None:
    extractor = _build_workspace_archive_extractor(session)
    await extractor.extract_zip_archive(
        archive_path=archive_path,
        destination_root=destination_root,
        data=data,
        archive_limits=archive_limits,
    )


def _copy_archive(
    data: io.IOBase,
    out: io.IOBase,
    *,
    path: Path,
    archive_limits: SandboxArchiveLimits | None,
) -> None:
    max_input_bytes = archive_limits.max_input_bytes if archive_limits is not None else None
    total = 0
    while True:
        chunk = data.read(io.DEFAULT_BUFFER_SIZE)
        if chunk in ("", b""):
            return
        total += len(chunk)
        if max_input_bytes is not None and total > max_input_bytes:
            raise WorkspaceArchiveWriteError(
                path=path,
                context={
                    "reason": "archive input size exceeds limit",
                    "limit": max_input_bytes,
                    "actual": total,
                },
            )
        out.write(chunk)


def _build_workspace_archive_extractor(session: BaseSandboxSession) -> WorkspaceArchiveExtractor:
    return WorkspaceArchiveExtractor(
        mkdir=lambda path: session.mkdir(path, parents=True),
        write=session.write,
        ls=lambda path: session.ls(path),
    )


__all__ = [
    "extract_archive",
    "extract_tar_archive",
    "extract_zip_archive",
    "safe_zip_member_rel_path",
]
