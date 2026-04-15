from __future__ import annotations

import io
import json

from .events import SandboxSessionEvent


def _safe_decode(b: bytes, *, max_chars: int) -> str:
    # Decode bytes as UTF-8 with replacement to keep event JSON valid.
    # Truncation is on decoded string length, not raw bytes.
    s = b.decode("utf-8", errors="replace")
    if len(s) > max_chars:
        return s[:max_chars] + "…"
    return s


def _best_effort_stream_len(stream: io.IOBase) -> int | None:
    # Avoid consuming the stream. This only works for seekable streams.
    try:
        pos = stream.tell()
        stream.seek(0, io.SEEK_END)
        end = stream.tell()
        stream.seek(pos, io.SEEK_SET)
        return int(end - pos)
    except Exception:
        return None


def event_to_json_line(event: SandboxSessionEvent) -> str:
    payload = event.model_dump(mode="json")
    return json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n"
