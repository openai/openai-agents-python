from __future__ import annotations

import io
from pathlib import Path

from ..errors import WorkspaceWriteTypeError


def coerce_write_payload(*, path: Path, data: io.IOBase) -> bytes:
    payload = data.read()
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    if not isinstance(payload, (bytes, bytearray)):
        raise WorkspaceWriteTypeError(path=path, actual_type=type(payload).__name__)
    return bytes(payload)
