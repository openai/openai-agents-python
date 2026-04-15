from __future__ import annotations

from collections.abc import Mapping

from agents.sandbox import Manifest
from agents.sandbox.entries import File


def text_manifest(files: Mapping[str, str]) -> Manifest:
    """Build a manifest from in-memory UTF-8 text files."""

    return Manifest(
        entries={path: File(content=contents.encode("utf-8")) for path, contents in files.items()}
    )


def tool_call_name(raw_item: object) -> str:
    """Return a readable name for a raw tool call item."""

    if isinstance(raw_item, dict):
        name = raw_item.get("name")
        item_type = raw_item.get("type")
    else:
        name = getattr(raw_item, "name", None)
        item_type = getattr(raw_item, "type", None)

    if isinstance(name, str) and name:
        return name
    if item_type == "shell_call":
        return "shell"
    if isinstance(item_type, str):
        return item_type
    return ""
