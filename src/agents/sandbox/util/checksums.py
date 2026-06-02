from __future__ import annotations

import hashlib
import io
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_io(stream: io.IOBase, *, chunk_size: int = 1024 * 1024) -> str:
    """Hash a readable stream and rewind it when possible."""

    start_position: int | None = None
    if stream.seekable():
        start_position = stream.tell()

    digest = hashlib.sha256()
    while True:
        chunk = stream.read(chunk_size)
        if chunk in ("", b""):
            break
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8")
        if not isinstance(chunk, bytes | bytearray):
            raise TypeError("sha256_io() requires a bytes-or-str readable stream")
        digest.update(chunk)

    if start_position is not None:
        stream.seek(start_position)

    return digest.hexdigest()
