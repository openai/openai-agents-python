from __future__ import annotations

import io
import tarfile
import tempfile

from agents.sandbox.util.tar_utils import strip_tar_member_prefix


def _prefixed_workspace_tar() -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        directory = tarfile.TarInfo("workspace")
        directory.type = tarfile.DIRTYPE
        tar.addfile(directory)
        payload = b"hello"
        member = tarfile.TarInfo("workspace/hello.txt")
        member.size = len(payload)
        tar.addfile(member, io.BytesIO(payload))
    buf.seek(0)
    return buf


def test_strip_tar_member_prefix_returns_seekable_stream() -> None:
    """Windows TemporaryFile wrappers must remain usable as binary IO streams."""

    stream = strip_tar_member_prefix(_prefixed_workspace_tar(), prefix="workspace")
    stream.seek(0)
    with tarfile.open(fileobj=stream, mode="r:*") as tar:
        assert tar.getnames() == [".", "hello.txt"]


def test_temporary_file_supports_hydrate_style_copy() -> None:
    """Docker hydrate copies the archive through TemporaryFile, then seeks it."""

    payload = b"ustar-payload"
    with tempfile.TemporaryFile() as archive:
        archive.write(payload)
        archive.seek(0)
        assert archive.read() == payload
