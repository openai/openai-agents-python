import io
from collections.abc import Iterator


class IteratorIO(io.IOBase):
    def __init__(self, it: Iterator[bytes]):
        self._it = it
        self._buffer = bytearray()
        self._closed = False

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if self._closed:
            return b""

        if size < 0:
            # Read all remaining data.
            chunks: list[bytes] = []
            if self._buffer:
                chunks.append(bytes(self._buffer))
                self._buffer.clear()
            for chunk in self._it:
                if chunk:
                    chunks.append(chunk)
            self._closed = True
            return b"".join(chunks)

        if size == 0:
            return b""

        # Fill buffer until we can satisfy the request or iterator is exhausted.
        while len(self._buffer) < size and not self._closed:
            try:
                chunk = next(self._it)
                if not chunk:
                    continue
                self._buffer.extend(chunk)
            except StopIteration:
                self._closed = True

        out = bytes(self._buffer[:size])
        del self._buffer[:size]
        return out

    def readinto(self, b: bytearray) -> int:
        if self._closed:
            return 0

        # Fill buffer until we have something or iterator is exhausted
        while not self._buffer:
            try:
                chunk = next(self._it)
                if not chunk:
                    continue
                self._buffer.extend(chunk)
            except StopIteration:
                self._closed = True
                return 0

        n = min(len(b), len(self._buffer))
        b[:n] = self._buffer[:n]
        del self._buffer[:n]
        return n

    def close(self) -> None:
        self._closed = True
        super().close()
