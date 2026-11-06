from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from ..errors import WorkspaceWriteTypeError


@dataclass(frozen=True)
class WritePayload:
    stream: io.IOBase
    content_length: int | None = None


class _BinaryReadAdapter(io.IOBase):
    def __init__(self, *, path: Path, stream: io.IOBase) -> None:
        self._path = path
        self._stream = stream

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        chunk = self._stream.read(size)
        if chunk is None:
            return b""
        if isinstance(chunk, bytes):
            return chunk
        if isinstance(chunk, bytearray):
            return bytes(chunk)
        raise WorkspaceWriteTypeError(path=self._path, actual_type=type(chunk).__name__)

    def readinto(self, b: bytearray) -> int:
        data = self.read(len(b))
        n = len(data)
        b[:n] = data
        return n

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        return int(self._stream.seek(offset, whence))

    def tell(self) -> int:
        return int(self._stream.tell())


def coerce_write_payload(*, path: Path, data: io.IOBase) -> WritePayload:
    stream = _BinaryReadAdapter(path=path, stream=data)
    return WritePayload(stream=stream, content_length=_best_effort_content_length(data))


def _best_effort_content_length(stream: io.IOBase) -> int | None:
    for attr in ("content_length", "length"):
        value = getattr(stream, attr, None)
        if isinstance(value, int) and value >= 0:
            return value

    headers = getattr(stream, "headers", None)
    if headers is not None:
        content_length = None
        get = getattr(headers, "get", None)
        if callable(get):
            content_length = get("Content-Length")
        if isinstance(content_length, str):
            try:
                parsed = int(content_length)
            except ValueError:
                parsed = None
            if parsed is not None and parsed >= 0:
                return parsed

    try:
        pos = stream.tell()
        stream.seek(0, io.SEEK_END)
        end = stream.tell()
        stream.seek(pos, io.SEEK_SET)
        return int(end - pos)
    except Exception:
        return None
