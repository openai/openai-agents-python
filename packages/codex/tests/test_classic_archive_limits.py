"""Classic archive extraction bounds resources and preserves trusted overrides."""

from __future__ import annotations

import bz2
import gc
import gzip
import io
import lzma
import struct
import subprocess
import sys
import tarfile
import weakref
import zipfile
import zlib
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.classic.run_config import SandboxArchiveLimits
from agents.classic.sandbox import Manifest
from agents.classic.sandbox.errors import WorkspaceArchiveWriteError
from agents.classic.sandbox.session import archive_ops
from agents.classic.sandbox.session.archive_extraction import WorkspaceArchiveExtractor
from agents.classic.sandbox.session.sandbox_session import SandboxSession
from agents.classic.sandbox.snapshot import NoopSnapshot

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="UnixLocal requires Unix")

if sys.platform != "win32":
    from agents.classic.sandbox.sandboxes.unix_local import (
        UnixLocalSandboxSession,
        UnixLocalSandboxSessionState,
    )

_ZSTD_CODEC = None
if sys.version_info >= (3, 14):
    try:
        from compression import zstd as _ZSTD_CODEC
    except ImportError:
        pass

_ZIP_RESOURCE_CODECS = [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA]
_TAR_COMPRESSION_SCHEMES = ["gzip"]
_ARCHIVE_SCHEMES = ["tar", "tar.gz", "tar.bz2", "tar.xz", "tar.lzma", "zip", "zip.lzma", "zip.bz2"]
if _ZSTD_CODEC is not None:
    _ZIP_RESOURCE_CODECS.append(zipfile.ZIP_ZSTANDARD)
    _TAR_COMPRESSION_SCHEMES.append("zstd")
    _ARCHIVE_SCHEMES.extend(["tar.zstd", "zip.zstd"])


def _compress_tar(data: bytes, compression: str) -> bytes:
    if compression == "gzip":
        return gzip.compress(data)
    assert _ZSTD_CODEC is not None
    return _ZSTD_CODEC.compress(data)


def _session(root: Path) -> UnixLocalSandboxSession:
    return UnixLocalSandboxSession(
        state=UnixLocalSandboxSessionState(
            manifest=Manifest(root=str(root)), snapshot=NoopSnapshot(id="archive-limits-test")
        )
    )


def _tar_with_file(name: str, payload: bytes) -> io.BytesIO:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        member = tarfile.TarInfo(name)
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    data.seek(0)
    return data


class _LargeStream(io.RawIOBase):
    def __init__(self, size: int) -> None:
        self.remaining = size
        self.chunk = b"x" * io.DEFAULT_BUFFER_SIZE

    def read(self, size: int = -1) -> bytes:
        if self.remaining == 0:
            return b""
        chunk = self.chunk[: min(self.remaining, size)]
        self.remaining -= len(chunk)
        return chunk


class _CountingSpool:
    def __init__(self) -> None:
        self.written = 0
        self.closed = False

    def write(self, data: bytes) -> int:
        self.written += len(data)
        return len(data)

    def seek(self, *_args: object) -> None:
        raise AssertionError("over-limit input reached sandbox write")

    def close(self) -> None:
        self.closed = True


async def test_default_input_limit_stops_host_spool_before_sandbox_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    limit = SandboxArchiveLimits().max_input_bytes
    assert limit is not None
    spool = _CountingSpool()
    monkeypatch.setattr(
        archive_ops,
        "tempfile",
        SimpleNamespace(SpooledTemporaryFile=lambda **_kwargs: spool),
    )

    with pytest.raises(WorkspaceArchiveWriteError) as error:
        await _session(tmp_path).extract("large.tar", _LargeStream(limit + 1))

    assert error.value.context["reason"] == "archive input size exceeds limit"
    assert spool.written <= limit
    assert spool.closed
    assert not (tmp_path / "large.tar").exists()


async def test_default_extracted_size_limit_rejects_tar_before_member_write(
    tmp_path: Path,
) -> None:
    limit = SandboxArchiveLimits().max_extracted_bytes
    assert limit is not None
    member = tarfile.TarInfo("huge.txt")
    member.size = limit + 1
    data = io.BytesIO(member.tobuf() + b"\0" * 1024)

    with pytest.raises(WorkspaceArchiveWriteError) as error:
        await _session(tmp_path).extract("huge.tar", data)

    assert error.value.context["reason"] == "archive extracted size exceeds limit"
    assert not (tmp_path / "huge.txt").exists()


@pytest.mark.parametrize("compression", _TAR_COMPRESSION_SCHEMES)
async def test_default_tar_metadata_limit_rejects_compressed_pax_bomb(
    tmp_path: Path, compression: str
) -> None:
    limit = SandboxArchiveLimits().max_tar_metadata_bytes
    assert limit is not None
    header = tarfile.TarInfo("pax")
    header.type = tarfile.XHDTYPE
    header.size = limit + 1
    compressed = _compress_tar(header.tobuf(format=tarfile.GNU_FORMAT) + b"\0" * 1024, compression)

    with pytest.raises(WorkspaceArchiveWriteError) as error:
        await _session(tmp_path).extract("bomb.tar", io.BytesIO(compressed))

    assert error.value.context["reason"] == "archive tar metadata size exceeds limit"
    assert not (tmp_path / "pax").exists()


@pytest.mark.parametrize("compression", _TAR_COMPRESSION_SCHEMES)
async def test_tar_decoded_stream_limit_rejects_compressed_payload(
    tmp_path: Path, compression: str
) -> None:
    data = _tar_with_file("payload.txt", b"x" * 4096)
    limits = SandboxArchiveLimits(max_tar_stream_bytes=2048)

    with pytest.raises(WorkspaceArchiveWriteError) as error:
        await _session(tmp_path).extract(
            "large.tar", io.BytesIO(_compress_tar(data.read(), compression)), archive_limits=limits
        )

    assert error.value.context["reason"] == "archive decoded tar size exceeds limit"
    assert not (tmp_path / "payload.txt").exists()


async def test_tar_metadata_limit_counts_gnu_sparse_map(tmp_path: Path) -> None:
    sparse_map = b"200\n" + b"0\n0\n" * 200
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        member = tarfile.TarInfo("sparse.txt")
        member.size = len(sparse_map)
        member.pax_headers = {
            "GNU.sparse.major": "1",
            "GNU.sparse.minor": "0",
            "GNU.sparse.name": "sparse.txt",
            "GNU.sparse.realsize": "0",
        }
        archive.addfile(member, io.BytesIO(sparse_map))

    with pytest.raises(WorkspaceArchiveWriteError) as error:
        await _session(tmp_path).extract(
            "sparse.tar",
            io.BytesIO(data.getvalue()),
            archive_limits=SandboxArchiveLimits(max_tar_metadata_bytes=1024),
        )

    assert error.value.context["reason"] == "archive tar metadata size exceeds limit"
    assert not (tmp_path / "sparse.txt").exists()


async def test_tar_member_limit_counts_root_directory_headers(tmp_path: Path) -> None:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        for _ in range(3):
            root = tarfile.TarInfo(".")
            root.type = tarfile.DIRTYPE
            archive.addfile(root)

    with pytest.raises(WorkspaceArchiveWriteError) as error:
        await _session(tmp_path).extract(
            "roots.tar",
            io.BytesIO(data.getvalue()),
            archive_limits=SandboxArchiveLimits(max_members=2),
        )

    assert error.value.context["reason"] == "archive member count exceeds limit"


async def test_corrupt_gzip_payload_raises_archive_error(tmp_path: Path) -> None:
    member = tarfile.TarInfo("payload.txt")
    member.size = 32_768
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    compressed = (
        b"\x1f\x8b\x08"
        + b"\0" * 7
        + compressor.compress(member.tobuf() + b"x" * 16_384)
        + compressor.flush(zlib.Z_SYNC_FLUSH)
        + b"\xff" * 32
    )

    with pytest.raises(WorkspaceArchiveWriteError):
        await _session(tmp_path).extract("corrupt.tar", io.BytesIO(compressed))

    assert not (tmp_path / "payload.txt").exists()


async def test_default_member_limit_rejects_zip_before_member_write(tmp_path: Path) -> None:
    limit = SandboxArchiveLimits().max_members
    assert limit is not None
    data = io.BytesIO()
    with zipfile.ZipFile(data, mode="w") as archive:
        for index in range(limit + 1):
            archive.writestr(f"{index}/", b"")
    data.seek(0)

    with pytest.raises(WorkspaceArchiveWriteError) as error:
        await _session(tmp_path).extract("many.zip", data)

    assert error.value.context["reason"] == "archive member count exceeds limit"
    assert not (tmp_path / "0").exists()


async def test_zip_member_limit_stops_directory_materialization(tmp_path: Path) -> None:
    data = io.BytesIO()
    with zipfile.ZipFile(data, mode="w") as archive:
        for index in range(12):
            archive.writestr(f"{index}.txt", b"")

    with pytest.raises(WorkspaceArchiveWriteError) as error:
        await _session(tmp_path).extract(
            "many.zip",
            io.BytesIO(data.getvalue()),
            archive_limits=SandboxArchiveLimits(max_members=5),
        )

    assert error.value.context["reason"] == "archive member count exceeds limit"
    assert not (tmp_path / "0.txt").exists()


async def test_zip_directory_size_limit_stops_parser(tmp_path: Path) -> None:
    data = io.BytesIO()
    with zipfile.ZipFile(data, mode="w") as archive:
        archive.writestr("long-name.txt", b"ordinary")

    with pytest.raises(WorkspaceArchiveWriteError) as error:
        await _session(tmp_path).extract(
            "large-directory.zip",
            io.BytesIO(data.getvalue()),
            archive_limits=SandboxArchiveLimits(max_zip_directory_bytes=50),
        )

    assert error.value.context["reason"] == "archive zip directory size exceeds limit"
    assert not (tmp_path / "long-name.txt").exists()


@pytest.mark.parametrize("scheme", _ARCHIVE_SCHEMES)
async def test_ordinary_archives_still_extract(tmp_path: Path, scheme: str) -> None:
    data = (
        _tar_with_file("ordinary.txt", b"ordinary")
        if not scheme.startswith("zip")
        else io.BytesIO()
    )
    if scheme.startswith("zip"):
        compression = {
            "zip": zipfile.ZIP_STORED,
            "zip.lzma": zipfile.ZIP_LZMA,
            "zip.bz2": zipfile.ZIP_BZIP2,
            "zip.zstd": getattr(zipfile, "ZIP_ZSTANDARD", -1),
        }[scheme]
        with zipfile.ZipFile(data, mode="w", compression=compression) as archive:
            archive.writestr("ordinary.txt", b"ordinary")
        data.seek(0)
    elif scheme == "tar.gz":
        data = io.BytesIO(gzip.compress(data.read()))
    elif scheme == "tar.bz2":
        data = io.BytesIO(bz2.compress(data.read()))
    elif scheme == "tar.xz":
        data = io.BytesIO(lzma.compress(data.read()))
    elif scheme == "tar.lzma":
        data = io.BytesIO(lzma.compress(data.read(), format=lzma.FORMAT_ALONE))
    elif scheme == "tar.zstd":
        data = io.BytesIO(_compress_tar(data.read(), "zstd"))

    await _session(tmp_path).extract(
        f"ordinary.{scheme}", data, compression_scheme=scheme.split(".")[0]
    )

    assert (tmp_path / "ordinary.txt").read_bytes() == b"ordinary"


async def test_tar_lzma_memory_limit_and_trusted_override(tmp_path: Path) -> None:
    data = lzma.compress(_tar_with_file("ordinary.txt", b"ordinary").read())
    session = _session(tmp_path)
    with pytest.raises(WorkspaceArchiveWriteError) as error:
        await session.extract(
            "limited.tar",
            io.BytesIO(data),
            archive_limits=SandboxArchiveLimits(max_tar_lzma_memory_bytes=1024),
        )
    assert isinstance(error.value.__cause__, tarfile.ReadError)
    assert isinstance(error.value.__cause__.__cause__, lzma.LZMAError)
    assert not (tmp_path / "ordinary.txt").exists()
    await session.extract(
        "trusted.tar",
        io.BytesIO(data),
        archive_limits=SandboxArchiveLimits(max_tar_lzma_memory_bytes=None),
    )
    assert (tmp_path / "ordinary.txt").read_bytes() == b"ordinary"


async def test_zip_lzma_dictionary_limit_and_trusted_override(tmp_path: Path) -> None:
    data = io.BytesIO()
    with zipfile.ZipFile(data, mode="w", compression=zipfile.ZIP_LZMA) as archive:
        archive.writestr("ordinary.txt", b"ordinary")
    session = _session(tmp_path)
    with pytest.raises(WorkspaceArchiveWriteError) as error:
        await session.extract(
            "limited.zip",
            io.BytesIO(data.getvalue()),
            archive_limits=SandboxArchiveLimits(max_zip_lzma_dictionary_bytes=1024),
        )
    assert error.value.context["reason"] == "archive zip LZMA dictionary size exceeds limit"
    await session.extract(
        "trusted.zip",
        io.BytesIO(data.getvalue()),
        archive_limits=SandboxArchiveLimits(max_zip_lzma_dictionary_bytes=None),
    )
    assert (tmp_path / "ordinary.txt").read_bytes() == b"ordinary"


async def test_trusted_override_can_disable_all_limits(tmp_path: Path) -> None:
    limits = SandboxArchiveLimits(**{field.name: None for field in fields(SandboxArchiveLimits)})
    await _session(tmp_path).extract(
        "trusted.tar", _tar_with_file("ordinary.txt", b"ordinary"), archive_limits=limits
    )
    assert (tmp_path / "ordinary.txt").read_bytes() == b"ordinary"


@pytest.mark.parametrize("compression", _ZIP_RESOURCE_CODECS)
@pytest.mark.parametrize("declared_size", [1, 256 * 1024])
async def test_zip_decoder_output_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compression: int, declared_size: int
) -> None:
    payload = b"x" * (256 * 1024)
    data = io.BytesIO()
    with zipfile.ZipFile(data, mode="w", compression=compression) as archive:
        archive.writestr("payload.txt", payload)
    raw = bytearray(data.getvalue())
    directory = raw.rfind(b"PK\x01\x02")
    struct.pack_into("<I", raw, directory + 16, zlib.crc32(payload[:declared_size]))
    struct.pack_into("<I", raw, directory + 24, declared_size)
    if compression == zipfile.ZIP_BZIP2:
        codec, decoder_name = bz2, "BZ2Decompressor"
    elif compression == zipfile.ZIP_LZMA:
        codec, decoder_name = lzma, "LZMADecompressor"
    else:
        assert _ZSTD_CODEC is not None
        codec, decoder_name = _ZSTD_CODEC, "ZstdDecompressor"
    decoder_type = getattr(codec, decoder_name)
    output_sizes: list[int] = []
    decoder_options: list[object] = []

    class ObservedDecoder:
        def __init__(self, *args: object, **kwargs: object) -> None:
            decoder_options.append(kwargs.get("options"))
            self.decoder = decoder_type(*args, **kwargs)

        def decompress(self, data: bytes, max_length: int = -1) -> bytes:
            output = self.decoder.decompress(data, max_length)
            output_sizes.append(len(output))
            return output

        def __getattr__(self, name: str) -> object:
            return getattr(self.decoder, name)

    monkeypatch.setattr(codec, decoder_name, ObservedDecoder)
    await _session(tmp_path).extract("bounded.zip", io.BytesIO(raw))
    assert (tmp_path / "payload.txt").read_bytes() == payload[:declared_size]
    assert output_sizes
    assert max(output_sizes) <= 64 * 1024
    if codec is _ZSTD_CODEC:
        assert {codec.DecompressionParameter.window_log_max: 27} in decoder_options


@pytest.mark.parametrize("compression", _ZIP_RESOURCE_CODECS)
async def test_compressed_zip_members_support_backend_rewinding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compression: int
) -> None:
    payload = b"ordinary" * 10_000
    data = io.BytesIO()
    with zipfile.ZipFile(data, mode="w", compression=compression) as archive:
        archive.writestr("payload.txt", payload)
    session = _session(tmp_path)
    original_write = session.write
    windows: list[int] = []
    if _ZSTD_CODEC is not None and compression == zipfile.ZIP_ZSTANDARD:
        decoder_type = _ZSTD_CODEC.ZstdDecompressor

        def observed_decoder(**kwargs: object) -> object:
            options = kwargs.get("options")
            if options:
                windows.append(options[_ZSTD_CODEC.DecompressionParameter.window_log_max])
            return decoder_type(**kwargs)

        monkeypatch.setattr(_ZSTD_CODEC, "ZstdDecompressor", observed_decoder)

    async def measuring_write(path: Path, stream: io.IOBase, **kwargs: object) -> None:
        if path.name == "payload.txt":
            assert stream.seek(0, io.SEEK_END) == len(payload)
            stream.seek(0)
        await original_write(path, stream, **kwargs)

    monkeypatch.setattr(session, "write", measuring_write)
    await session.extract("rewound.zip", io.BytesIO(data.getvalue()))
    assert (tmp_path / "payload.txt").read_bytes() == payload
    if _ZSTD_CODEC is not None and compression == zipfile.ZIP_ZSTANDARD:
        assert windows.count(27) >= 2


@pytest.mark.parametrize("compression", _ZIP_RESOURCE_CODECS)
async def test_compressed_zip_members_release_decoder_without_cyclic_gc(
    tmp_path: Path, compression: int
) -> None:
    data = io.BytesIO()
    with zipfile.ZipFile(data, mode="w", compression=compression) as archive:
        archive.writestr("ordinary.txt", b"ordinary")
    references: list[weakref.ReferenceType[io.IOBase]] = []

    async def mkdir(path: Path) -> None:
        pass

    async def ls(path: Path) -> list:
        return []

    async def write(path: Path, stream: io.IOBase) -> None:
        references.append(weakref.ref(stream))
        assert stream.read() == b"ordinary"

    # In-memory callbacks isolate decoder ownership from backend worker lifetimes.
    extractor = WorkspaceArchiveExtractor(mkdir=mkdir, write=write, ls=ls)
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        await extractor.extract_zip_archive(
            archive_path=tmp_path / "ordinary.zip",
            destination_root=tmp_path,
            data=io.BytesIO(data.getvalue()),
        )
        assert references
        assert all(reference() is None for reference in references)
    finally:
        if gc_was_enabled:
            gc.enable()
        gc.collect()


def test_plain_archives_work_without_optional_compression_backends(tmp_path: Path) -> None:
    script = """
import importlib.abc
import sys
for name in ("bz2", "_bz2", "lzma", "_lzma", "compression.zstd", "_zstd"):
    sys.modules.pop(name, None)
class MissingCompression(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ("_bz2", "_lzma", "_zstd"):
            raise ModuleNotFoundError("optional compression backend unavailable", name=fullname)
sys.meta_path.insert(0, MissingCompression())
import asyncio, io, tarfile, zipfile
from pathlib import Path
from agents.classic.sandbox import Manifest
from agents.classic.sandbox.sandboxes.unix_local import (
    UnixLocalSandboxSession, UnixLocalSandboxSessionState)
from agents.classic.sandbox.snapshot import NoopSnapshot
async def main():
    root = Path(sys.argv[1])
    session = UnixLocalSandboxSession(state=UnixLocalSandboxSessionState(
        manifest=Manifest(root=str(root)), snapshot=NoopSnapshot(id="optional-codecs")))
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        member = tarfile.TarInfo("plain.txt")
        member.size = 8
        archive.addfile(member, io.BytesIO(b"ordinary"))
    data.seek(0)
    await session.extract("plain.tar", data)
    data = io.BytesIO()
    with zipfile.ZipFile(data, mode="w") as archive:
        archive.writestr("stored.txt", b"ordinary")
    data.seek(0)
    await session.extract("stored.zip", data)
    assert (root / "plain.txt").read_bytes() == b"ordinary"
    assert (root / "stored.txt").read_bytes() == b"ordinary"
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


async def test_uncompressed_tar_name_starting_with_bzip_magic_still_extracts(
    tmp_path: Path,
) -> None:
    await _session(tmp_path).extract("named.tar", _tar_with_file("BZhnotes.txt", b"ordinary"))

    assert (tmp_path / "BZhnotes.txt").read_bytes() == b"ordinary"


async def test_per_call_override_can_raise_session_limits(tmp_path: Path) -> None:
    session = SandboxSession(_session(tmp_path))
    session._set_archive_limits(
        SandboxArchiveLimits(max_input_bytes=1, max_extracted_bytes=1, max_members=1)
    )

    await session.extract(
        "larger.tar",
        _tar_with_file("ordinary.txt", b"ordinary"),
        archive_limits=SandboxArchiveLimits(
            max_input_bytes=20_000, max_extracted_bytes=100, max_members=2
        ),
    )

    assert (tmp_path / "ordinary.txt").read_bytes() == b"ordinary"


async def test_per_call_override_can_raise_tar_metadata_limit(tmp_path: Path) -> None:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz", pax_headers={"comment": "x" * 4096}) as archive:
        member = tarfile.TarInfo("ordinary.txt")
        member.size = 8
        archive.addfile(member, io.BytesIO(b"ordinary"))

    session = _session(tmp_path)
    with pytest.raises(WorkspaceArchiveWriteError) as error:
        await session.extract(
            "metadata.tar",
            io.BytesIO(data.getvalue()),
            archive_limits=SandboxArchiveLimits(max_tar_metadata_bytes=1024),
        )
    assert error.value.context["reason"] == "archive tar metadata size exceeds limit"

    await session.extract(
        "metadata.tar",
        io.BytesIO(data.getvalue()),
        archive_limits=SandboxArchiveLimits(max_tar_metadata_bytes=8192),
    )
    assert (tmp_path / "ordinary.txt").read_bytes() == b"ordinary"
