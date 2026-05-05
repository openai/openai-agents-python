from __future__ import annotations

import io
from pathlib import Path
from typing import Any, cast

import pytest

from agents.sandbox.errors import ErrorCode, WorkspaceWriteTypeError
from agents.sandbox.session.workspace_payloads import coerce_write_payload


class _Headers:
    def __init__(self, value: str | None) -> None:
        self._value = value

    def get(self, name: str) -> str | None:
        assert name == "Content-Length"
        return self._value


class _HeaderStream(io.BytesIO):
    def __init__(self, data: bytes, content_length: str | None) -> None:
        super().__init__(data)
        self.headers = _Headers(content_length)


class _LengthStream(io.BytesIO):
    def __init__(self, data: bytes, length: int) -> None:
        super().__init__(data)
        self.length = length


class _NoneReadStream:
    def read(self, size: int = -1) -> Any:
        _ = size
        return None


class _BytearrayReadStream:
    def read(self, size: int = -1) -> Any:
        _ = size
        return bytearray(b"abc")


class _TextReadStream:
    def read(self, size: int = -1) -> Any:
        _ = size
        return "not-bytes"


class _UnseekableStream(io.BytesIO):
    def tell(self) -> int:
        raise OSError("not seekable")


def test_coerce_write_payload_adapts_binary_reads() -> None:
    payload = coerce_write_payload(path=Path("/workspace/file.bin"), data=io.BytesIO(b"abc"))

    assert payload.content_length == 3
    assert payload.stream.readable() is True
    assert payload.stream.read(1) == b"a"
    assert payload.stream.read() == b"bc"


def test_coerce_write_payload_adapts_bytearray_and_none_reads() -> None:
    bytearray_payload = coerce_write_payload(
        path=Path("/workspace/file.bin"),
        data=cast(io.IOBase, _BytearrayReadStream()),
    )
    none_payload = coerce_write_payload(
        path=Path("/workspace/empty.bin"),
        data=cast(io.IOBase, _NoneReadStream()),
    )

    assert bytearray_payload.stream.read() == b"abc"
    assert none_payload.stream.read() == b""


def test_coerce_write_payload_supports_readinto_seek_and_tell() -> None:
    payload = coerce_write_payload(path=Path("/workspace/file.bin"), data=io.BytesIO(b"abcdef"))
    buffer = bytearray(3)

    assert cast(Any, payload.stream).readinto(buffer) == 3
    assert bytes(buffer) == b"abc"
    assert payload.stream.tell() == 3
    assert payload.stream.seek(1) == 1
    assert payload.stream.read(2) == b"bc"


def test_coerce_write_payload_rejects_text_chunks() -> None:
    path = Path("/workspace/file.txt")
    payload = coerce_write_payload(
        path=path,
        data=cast(io.IOBase, _TextReadStream()),
    )

    with pytest.raises(WorkspaceWriteTypeError) as exc_info:
        payload.stream.read()

    assert exc_info.value.error_code is ErrorCode.WORKSPACE_WRITE_TYPE_ERROR
    assert exc_info.value.context == {
        "path": str(path),
        "actual_type": "str",
    }


@pytest.mark.parametrize(
    ("stream", "expected"),
    [
        (_LengthStream(b"abc", 5), 5),
        (_HeaderStream(b"abc", "7"), 7),
        (_HeaderStream(b"abc", "-1"), 3),
        (_HeaderStream(b"abc", "invalid"), 3),
        (_UnseekableStream(b"abc"), None),
    ],
)
def test_coerce_write_payload_uses_best_effort_content_length(
    stream: io.IOBase,
    expected: int | None,
) -> None:
    payload = coerce_write_payload(path=Path("/workspace/file.bin"), data=stream)

    assert payload.content_length == expected
