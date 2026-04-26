from __future__ import annotations

import io
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from ..errors import InvalidCompressionSchemeError
from .archive_extraction import WorkspaceArchiveExtractor, safe_zip_member_rel_path

if TYPE_CHECKING:
    from .base_sandbox_session import BaseSandboxSession


async def extract_archive(
    session: BaseSandboxSession,
    path: Path | str,
    data: io.IOBase,
    *,
    compression_scheme: Literal["tar", "zip"] | None = None,
) -> None:
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
        shutil.copyfileobj(data, spool)
        spool.seek(0)
        await session.write(normalized_path, spool)
        spool.seek(0)

        if compression_scheme == "tar":
            await session._extract_tar_archive(
                archive_path=normalized_path,
                destination_root=destination_root,
                data=spool,
            )
        else:
            await session._extract_zip_archive(
                archive_path=normalized_path,
                destination_root=destination_root,
                data=spool,
            )
    finally:
        spool.close()


async def extract_tar_archive(
    session: BaseSandboxSession,
    *,
    archive_path: Path,
    destination_root: Path,
    data: io.IOBase,
) -> None:
    extractor = _build_workspace_archive_extractor(session)
    await extractor.extract_tar_archive(
        archive_path=archive_path,
        destination_root=destination_root,
        data=data,
    )


async def extract_zip_archive(
    session: BaseSandboxSession,
    *,
    archive_path: Path,
    destination_root: Path,
    data: io.IOBase,
) -> None:
    extractor = _build_workspace_archive_extractor(session)
    await extractor.extract_zip_archive(
        archive_path=archive_path,
        destination_root=destination_root,
        data=data,
    )


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
