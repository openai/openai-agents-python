"""Private serialization of authenticated configured child result prefixes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

_MAX_RECORDS = 4096
_MAX_BYTES = 8 * 1024 * 1024
_REPLAYED_OUTPUT_TYPES = {
    "function_call_output",
    "custom_tool_call_output",
    "shell_call_output",
    "apply_patch_call_output",
}


@dataclass(frozen=True, repr=False)
class _ProducedPrefix:
    count: int = 0
    parts: tuple[str, ...] = ()
    bytes: int = 2

    def extend(
        self, encoded: str, *, replayed_calls: frozenset[str] = frozenset()
    ) -> _ProducedPrefix:
        value = part = kept = None
        try:
            if (
                not isinstance(encoded, str)
                or not encoded.startswith("[")
                or not encoded.endswith("]")
            ):
                raise ValueError("Invalid native produced item records")
            part = encoded[1:-1]
            if not part:
                return self
            part_bytes = len(part.encode())
            if part_bytes + 2 > _MAX_BYTES:
                raise ValueError("Produced run items exceed the one-shot result limit")
            value = json.loads(encoded)
            if not isinstance(value, list) or any(
                not isinstance(row, dict)
                or not isinstance(row.get("record"), dict)
                or "agent" not in row
                or not (row["agent"] is None or isinstance(row["agent"], dict))
                or ("tool_success" in row and not isinstance(row["tool_success"], bool))
                for row in value
            ):
                raise ValueError("Invalid native produced item records")
            if replayed_calls:
                # The native checkpoint removes outputs it owns for later
                # replay. The collector can also see the synthetic abort of
                # a parked request. Neither belongs to the accepted prefix.
                kept = [
                    row for row in value if not _output_replayed_by_checkpoint(row, replayed_calls)
                ]
                if len(kept) != len(value):
                    if not kept:
                        return self
                    value = kept
                    part = json.dumps(value, separators=(",", ":"), ensure_ascii=False)[1:-1]
                    part_bytes = len(part.encode())
            new_size = self.bytes + part_bytes + bool(self.parts)
            total = self.count + len(value)
            if total > _MAX_RECORDS or new_size > _MAX_BYTES:
                raise ValueError("Produced run items exceed the one-shot result limit")
            return _ProducedPrefix(total, (*self.parts, part), new_size)
        finally:
            del self, encoded, replayed_calls, value, part, kept

    def encode(self) -> str:
        return "[" + ",".join(self.parts) + "]"

    def rows(self) -> list[dict[str, Any]]:
        # extend() has checked each row before storing its serialized form.
        return cast(list[dict[str, Any]], json.loads(self.encode()))


def _output_replayed_by_checkpoint(row: dict[str, Any], calls: frozenset[str]) -> bool:
    record = row["record"]
    if record.get("type") != "response_item":
        return False
    item = record.get("payload")
    if not isinstance(item, dict):
        return False
    kind, call_id = item.get("type"), item.get("call_id")
    return (
        isinstance(kind, str)
        and kind in _REPLAYED_OUTPUT_TYPES
        and isinstance(call_id, str)
        and call_id in calls
    )
