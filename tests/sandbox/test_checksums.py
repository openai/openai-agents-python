from __future__ import annotations

import hashlib
import io

import pytest

from agents.sandbox.util.checksums import sha256_io


class _InvalidChunkStream(io.BytesIO):
    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self._read_count = 0

    def read(self, size: int = -1) -> bytes:
        self._read_count += 1
        if self._read_count == 2:
            return object()  # type: ignore[return-value]
        return super().read(size)


class _RewindFailureStream(io.BytesIO):
    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        raise OSError("rewind failed")


class _InvalidChunkAndRewindFailureStream(_InvalidChunkStream):
    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        raise OSError("rewind failed")


def test_sha256_io_rewinds_seekable_stream_when_hashing_fails() -> None:
    stream = _InvalidChunkStream(b"zab")
    stream.seek(1)

    with pytest.raises(TypeError, match="requires a bytes-or-str readable stream"):
        sha256_io(stream, chunk_size=1)

    assert stream.tell() == 1


def test_sha256_io_hashes_from_and_rewinds_to_original_position() -> None:
    stream = io.BytesIO(b"zab")
    stream.seek(1)

    assert sha256_io(stream, chunk_size=1) == hashlib.sha256(b"ab").hexdigest()
    assert stream.tell() == 1


def test_sha256_io_preserves_hashing_error_when_rewind_also_fails() -> None:
    stream = _InvalidChunkAndRewindFailureStream(b"ab")

    with pytest.raises(TypeError, match="requires a bytes-or-str readable stream"):
        sha256_io(stream, chunk_size=1)


def test_sha256_io_raises_rewind_error_after_successful_hashing() -> None:
    stream = _RewindFailureStream(b"ab")

    with pytest.raises(OSError, match="rewind failed"):
        sha256_io(stream, chunk_size=1)
