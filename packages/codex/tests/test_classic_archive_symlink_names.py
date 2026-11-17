"""Archive extraction must inspect exact destination names on remote backends."""

import io
import subprocess
import tarfile
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest

from agents.classic.sandbox.errors import WorkspaceArchiveWriteError
from agents.classic.sandbox.session import archive_ops
from agents.classic.sandbox.types import ExecResult
from agents.classic.sandbox.util.parse_utils import parse_ls_la


class _RemoteStyleSession:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def _validate_path_access(self, path: Path, *, for_write: bool = False) -> Path:
        return self.root / path

    async def exec(self, *command: str, shell: bool = False) -> ExecResult:
        result = subprocess.run(command, capture_output=True, check=False)
        return ExecResult(stdout=result.stdout, stderr=result.stderr, exit_code=result.returncode)

    async def ls(self, path: Path) -> list:
        result = await self.exec("ls", "-la", "--", str(path))
        assert result.ok()
        return parse_ls_la(result.stdout.decode("utf-8", errors="replace"), base=str(path))

    async def mkdir(self, path: Path, *, parents: bool = False) -> None:
        path.mkdir(parents=parents, exist_ok=True)

    async def write(self, path: Path, data: io.IOBase) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data.read())

    async def _extract_tar_archive(self, **kwargs: Any) -> None:
        await archive_ops.extract_tar_archive(cast(Any, self), **kwargs)

    async def _extract_zip_archive(self, **kwargs: Any) -> None:
        await archive_ops.extract_zip_archive(cast(Any, self), **kwargs)


def _archive(scheme: str, name: str, payload: bytes) -> io.BytesIO:
    data = io.BytesIO()
    if scheme == "tar":
        with tarfile.open(fileobj=data, mode="w") as archive:
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    else:
        with zipfile.ZipFile(data, mode="w") as archive:
            archive.writestr(name, payload)
    data.seek(0)
    return data


@pytest.mark.parametrize("scheme", ["tar", "zip"])
@pytest.mark.parametrize("link_name", ["alias -> token", "alias\nsecond"])
async def test_archive_rejects_existing_symlink_with_ambiguous_listing_name(
    tmp_path: Path, scheme: str, link_name: str
) -> None:
    target = tmp_path / "other -> target.txt"
    target.write_bytes(b"original")
    (tmp_path / link_name).symlink_to(target)

    with pytest.raises(WorkspaceArchiveWriteError):
        await archive_ops.extract_archive(
            cast(Any, _RemoteStyleSession(tmp_path)),
            Path(f"input.{scheme}"),
            _archive(scheme, link_name, b"changed"),
        )

    assert target.read_bytes() == b"original"
    assert (tmp_path / link_name).is_symlink()


@pytest.mark.parametrize("scheme", ["tar", "zip"])
async def test_archive_rejects_existing_symlinked_parent_with_delimiter_name(
    tmp_path: Path, scheme: str
) -> None:
    target = tmp_path / "other-dir"
    target.mkdir()
    (tmp_path / "dir -> token").symlink_to(target, target_is_directory=True)

    with pytest.raises(WorkspaceArchiveWriteError):
        await archive_ops.extract_archive(
            cast(Any, _RemoteStyleSession(tmp_path)),
            Path(f"input.{scheme}"),
            _archive(scheme, "dir -> token/payload.txt", b"changed"),
        )

    assert not (target / "payload.txt").exists()


@pytest.mark.parametrize("scheme", ["tar", "zip"])
async def test_archive_preserves_ordinary_delimiter_name_and_existing_file(
    tmp_path: Path, scheme: str
) -> None:
    name = "ordinary -> file.txt"
    (tmp_path / name).write_bytes(b"original")

    await archive_ops.extract_archive(
        cast(Any, _RemoteStyleSession(tmp_path)),
        Path(f"input.{scheme}"),
        _archive(scheme, name, b"changed"),
    )

    assert (tmp_path / name).read_bytes() == b"changed"
