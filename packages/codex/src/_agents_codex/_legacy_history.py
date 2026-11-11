"""Decode v0 database records into the native history import shape."""

from __future__ import annotations

import json
from typing import Any


def decode_legacy_item(raw: str | bytes) -> dict[str, Any]:
    item = json.loads(raw)
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
