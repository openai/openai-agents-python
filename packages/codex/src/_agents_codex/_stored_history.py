"""Copied native store pages; no rollout reconstruction or replacement revision."""

from __future__ import annotations

import json
from typing import Any, Literal, cast

from typing_extensions import TypedDict


class StoredItem(TypedDict):
    """Core's indexed item projection, distinct from an original JSON record."""

    id: str
    turn_id: str
    updated_at_ordinal: int
    created_at_ms: int
    started_at_ms: int | None
    completed_at_ms: int | None
    data: dict[str, Any]


class StoredTurn(TypedDict):
    """Native turn metadata; Unix-second timestamps and millisecond durations."""

    id: str
    status: Literal["completed", "interrupted", "failed", "in_progress"]
    error: dict[str, Any] | None
    started_at: int | None
    completed_at: int | None
    duration_ms: int | None
    summary_items: list[StoredItem]


class StoredTurnPage(TypedDict):
    turns: list[StoredTurn]
    next_cursor: str | None
    backwards_cursor: str | None


class StoredItemPage(TypedDict):
    items: list[StoredItem]
    next_cursor: str | None
    backwards_cursor: str | None


class StoredMatchRange(TypedDict):
    """UTF-16 code-unit positions within the returned snippet."""

    start: int
    end: int


class StoredMatch(TypedDict):
    turn_id: str
    item_id: str
    snippet: str
    snippet_match_range: StoredMatchRange
    turn_cursor: str


class StoredMatchPage(TypedDict):
    items: list[StoredMatch]
    next_cursor: str | None


def _options(limit: int, cursor: str | None, direction: str = "asc") -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("Stored history limit must be an integer from 1 to 1000")
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise ValueError("Stored history cursor must be a nonempty string or None")
    if direction not in ("asc", "desc"):
        raise ValueError("Stored history direction must be asc or desc")
    return {"limit": limit, "cursor": cursor, "descending": direction == "desc"}


def _item(raw: dict[str, Any]) -> StoredItem:
    data = json.loads(raw.pop("data_json"))
    if not isinstance(data, dict):
        raise ValueError("Native stored item must contain a JSON object")
    raw["updated_at_ordinal"] = int(raw["updated_at_ordinal"])
    raw["data"] = data
    return cast(StoredItem, raw)


def _turn_page(raw: dict[str, Any]) -> StoredTurnPage:
    for turn in raw["turns"]:
        turn["summary_items"] = [_item(item) for item in turn.pop("items")]
    return cast(StoredTurnPage, raw)


def _item_page(raw: dict[str, Any]) -> StoredItemPage:
    raw["items"] = [_item(item) for item in raw["items"]]
    return cast(StoredItemPage, raw)
