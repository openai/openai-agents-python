"""Copied native store pages; no rollout reconstruction or replacement revision."""

from __future__ import annotations

import json
from typing import Any, Literal, cast

from typing_extensions import TypedDict


class StoredSession(TypedDict):
    """Native Exec application-session identity; no history or resume grant."""

    id: str
    name: str | None
    forked_from_id: str | None
    parent_thread_id: str | None
    created_at_ms: int
    updated_at_ms: int


class StoredSessionPage(TypedDict):
    sessions: list[StoredSession]
    next_cursor: str | None


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


def _sessions_request(limit: int, cursor: str | None, archived: bool) -> str:
    options = _options(limit, cursor)
    options.pop("descending")
    if type(archived) is not bool:
        raise TypeError("archived must be a bool")
    return json.dumps({"kind": "sessions", **options, "archived": archived})


def _turns_request(limit: int, cursor: str | None, direction: str, include_summary: bool) -> str:
    options = _options(limit, cursor, direction)
    if type(include_summary) is not bool:
        raise TypeError("include_summary must be a bool")
    return json.dumps({"kind": "turns", **options, "include_items": include_summary})


def _items_request(limit: int, cursor: str | None, direction: str, turn_id: str | None) -> str:
    options = _options(limit, cursor, direction)
    if turn_id is not None and (not isinstance(turn_id, str) or not turn_id):
        raise ValueError("turn_id must be a nonempty string or None")
    return json.dumps({"kind": "items", **options, "turn_id": turn_id})


def _search_request(limit: int, cursor: str | None, text: str) -> str:
    options = _options(limit, cursor)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Stored history search requires nonempty text")
    options.pop("descending")
    return json.dumps({"kind": "search", **options, "text": text})


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
