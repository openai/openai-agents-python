"""Bound the JSON values captured for an opted-in operation."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

CONTENT_LIMIT = 32 * 1024
_TRUNCATED = "[truncated]"
_OMIT_FIELDS = {
    "api_key",
    "authorization",
    "access_token",
    "refresh_token",
    "password",
    "headers",
    "encrypted_content",
    "reasoning",
    "reasoning_content",
    "reasoning_details",
    "thinking",
    "image_url",
    "file_data",
    "image_data",
    "audio_data",
    "b64_json",
    "client_metadata",
    "x_api_key",
}
_OMIT_TYPES = {
    "reasoning",
    "reasoning_text",
    "thinking",
    "redacted_thinking",
    "compaction",
    "image",
    "input_image",
    "output_image",
    "image_url",
    "computer_screenshot",
    "image_generation_call",
    "audio",
    "input_audio",
    "output_audio",
    "file",
    "input_file",
    "video",
    "input_video",
    "encrypted_content",
}
_OMIT_FIELDS = {key.replace("_", "") for key in _OMIT_FIELDS}
_DATA_URI = re.compile(r"data:[^\s;,]+(?:;[^\s,]*)?,[^\s\"'<>]+", re.IGNORECASE)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def _text(value: str, budget: int) -> str:
    if len(value) <= budget and _size(value) <= budget:
        return value
    low, high = 0, min(budget, len(value))
    while low < high:
        pivot = (low + high + 1) // 2
        if _size(value[:pivot] + _TRUNCATED) <= budget:
            low = pivot
        else:
            high = pivot - 1
    return value[:low] + _TRUNCATED


def _project(value: Any, budget: int, depth: int) -> Any:
    if depth > 32:
        return _TRUNCATED
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                embedded = json.loads(value)
            except ValueError:
                pass
            else:
                safe = _project(embedded, budget, depth + 1)
                if safe == embedded and _size(value) <= budget:
                    return value
                encoded = _json(safe)
                if _size(encoded) > budget:
                    # Escaping can double the bytes. Rebudget the value rather than
                    # slicing encoded JSON into an invalid tool result.
                    encoded = _json(_project(embedded, (budget - 2) // 2, depth + 1))
                return encoded
        value = _DATA_URI.sub("[omitted: media]", value)
        return _text(value, budget)
    if value is None or isinstance(value, (bool, float, int)):
        return value if _size(value) <= budget else _TRUNCATED
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, Mapping):
        kind = value.get("type")
        if isinstance(kind, str) and kind in _OMIT_TYPES:
            return {"type": kind, "omitted": True, "reason": "unsupported_content"}
        resource = value.get("resource")
        if kind == "resource" and isinstance(resource, Mapping) and "blob" in resource:
            return {"type": kind, "omitted": True, "reason": "unsupported_content"}
        result: dict[str, Any] = {}
        used = 2
        identity = {"type", "role", "id", "call_id", "name"}
        for key, item in sorted(value.items(), key=lambda entry: entry[0] not in identity):
            if not isinstance(key, str):
                continue
            # Reserve space for a trailing marker before accepting another field.
            available = budget - used - _size(key) - 4 - 32
            if available < 84:
                result["truncated"] = True
                break
            if key.lower().replace("-", "").replace("_", "") in _OMIT_FIELDS:
                item = "[omitted]"
            result[key] = _project(item, available, depth + 1)
            used += _size(key) + _size(result[key]) + 4
        return result
    if isinstance(value, (list, tuple)):
        items: list[Any] = []
        used = 2
        for item in value:
            available = budget - used - 2 - 32
            if available < 84:
                items.append({"truncated": True})
                break
            items.append(_project(item, available, depth + 1))
            used += _size(items[-1]) + 2
        return items
    return "[omitted: binary or unsupported]"


def bound_content(value: Any) -> Any:
    """Keep readable text and structure within a 32 KiB serialized UTF-8 budget."""
    try:
        captured = _project(value, CONTENT_LIMIT - 256, 0)
        return captured if _size(captured) <= CONTENT_LIMIT else {"truncated": True}
    except Exception:
        # A tool result may have a custom serializer. Tracing must not fail its caller.
        return "[omitted: serialization failed]"
