"""Decode v0 database records into the native history import shape."""

from __future__ import annotations

import json
import re
from typing import Any

_JSON_WHITESPACE = re.compile(r"[ \t\r\n]*")


def validate_legacy_history_limits(max_items: int, max_bytes: int) -> None:
    if type(max_items) is not int or not 0 < max_items < 2**53:
        raise ValueError("max_items must be a positive integer smaller than 2**53")
    if type(max_bytes) is not int or not 0 < max_bytes < 2**53:
        raise ValueError("max_bytes must be a positive integer smaller than 2**53")


def check_legacy_history_size(
    count: int, encoded_bytes: int, max_items: int, max_bytes: int
) -> None:
    if count > max_items:
        raise ValueError("v0 session history exceeds max_items")
    if encoded_bytes > max_bytes:
        raise ValueError("v0 session history exceeds max_bytes")


def decode_legacy_item(raw: str | bytes | dict[str, Any]) -> dict[str, Any]:
    item = raw.copy() if isinstance(raw, dict) else json.loads(raw)
    if not isinstance(item, dict):
        raise ValueError("A v0 session item must be a JSON object")
    role = item.get("role")
    if item.get("type", "message") == "message" and role in (
        "user",
        "assistant",
        "developer",
        "system",
    ):
        item["type"] = "message"
        if isinstance(content := item.get("content"), str):
            kind = "output_text" if role == "assistant" else "input_text"
            item["content"] = [{"type": kind, "text": content}]
    return item


def decode_legacy_history_array(raw: bytes, max_items: int, max_bytes: int) -> list[dict[str, Any]]:
    check_legacy_history_size(0, len(raw), max_items, max_bytes)
    text = raw.decode("utf-8")
    decoder = json.JSONDecoder()

    def skip_whitespace(start: int) -> int:
        matched = _JSON_WHITESPACE.match(text, start)
        assert matched is not None
        return matched.end()

    start = skip_whitespace(0)
    if text[start : start + 1] != "[":
        raise ValueError("Dapr v0 session messages must be a JSON list")
    start = skip_whitespace(start + 1)
    items: list[dict[str, Any]] = []
    if text[start : start + 1] != "]":
        while True:
            check_legacy_history_size(len(items) + 1, 0, max_items, max_bytes)
            item, end = decoder.raw_decode(text, start)
            items.append(decode_legacy_item(item))
            start = skip_whitespace(end)
            delimiter = text[start : start + 1]
            if delimiter == "]":
                break
            if delimiter != ",":
                raise ValueError("Dapr v0 session messages must be a JSON list")
            start = skip_whitespace(start + 1)
    if skip_whitespace(start + 1) != len(text):
        raise ValueError("Dapr v0 session messages must be a JSON list")
    return items
