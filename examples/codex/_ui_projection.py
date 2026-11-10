"""Shared canonical-text check for the copyable native UI examples."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence, Set
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agents import Session


async def _complete_display(
    session: Session[Any, Any],
    child_id: str | None,
    turns: Set[str],
    last: str,
    shown: Mapping[str, Sequence[str]],
    plain: bool,
    output: object,
) -> bool:
    recorded: dict[str, str] = {}
    candidate = fallback = None
    try:
        async for item in session.items(subagent_id=child_id):
            if (
                item["type"] != "message"
                or item["role"] != "assistant"
                or item["status"] != "completed"
                or item["turn_id"] not in turns
            ):
                continue
            text = item.get("display_text")
            if text is None:
                text = "".join(
                    part["text"] for part in item["content"] if part["type"] != "input_image"
                )
            recorded[item["id"]] = text
            if item["turn_id"] != last:
                continue
            if item["phase"] == "final_answer":
                candidate = item["id"]
            elif item["phase"] is None:
                fallback = item["id"]
                # Rust whitespace excludes Python's U+001C..U+001F.
                if re.search(r"\S|[\x1c-\x1f]", text):
                    candidate = item["id"]
    except Exception:
        return False
    displayed = {item_id: "".join(parts) for item_id, parts in shown.items()}
    if any(recorded.get(item_id) != text for item_id, text in displayed.items()):
        return False
    final_id = candidate or fallback
    if any(
        text and displayed.get(item_id) != text and (plain or item_id != final_id)
        for item_id, text in recorded.items()
    ):
        return False
    if not plain:
        # Typed output can complete without rendering its final JSON item.
        return True
    if output == "" and not any(displayed.values()):
        return True
    if final_id is None or final_id not in displayed:
        return False
    text = displayed[final_id]
    return text == output or (output == "" and re.fullmatch(r"[^\S\x1c-\x1f]*", text) is not None)
