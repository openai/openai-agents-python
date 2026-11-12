"""Pure request encoding checks without a native runtime."""

import pytest

from _agents_codex._stored_history import (
    _items_request,
    _search_request,
    _sessions_request,
    _turns_request,
)


@pytest.mark.parametrize(
    ("encode", "args", "expected"),
    [
        (
            _sessions_request,
            (50, None, False),
            '{"kind": "sessions", "limit": 50, "cursor": null, "archived": false}',
        ),
        (
            _sessions_request,
            (1, "next", True),
            '{"kind": "sessions", "limit": 1, "cursor": "next", "archived": true}',
        ),
        (
            _turns_request,
            (50, None, "asc", False),
            '{"kind": "turns", "limit": 50, "cursor": null, '
            '"descending": false, "include_items": false}',
        ),
        (
            _turns_request,
            (1000, "next", "desc", True),
            '{"kind": "turns", "limit": 1000, "cursor": "next", '
            '"descending": true, "include_items": true}',
        ),
        (
            _items_request,
            (50, None, "asc", None),
            '{"kind": "items", "limit": 50, "cursor": null, "descending": false, "turn_id": null}',
        ),
        (
            _items_request,
            (1, "next", "desc", "turn"),
            '{"kind": "items", "limit": 1, "cursor": "next", '
            '"descending": true, "turn_id": "turn"}',
        ),
        (
            _search_request,
            (50, None, "  Needle é😀\n"),
            '{"kind": "search", "limit": 50, "cursor": null, '
            '"text": "  Needle \\u00e9\\ud83d\\ude00\\n"}',
        ),
    ],
)
def test_exact_request(encode, args, expected):
    assert encode(*args) == expected


@pytest.mark.parametrize(
    ("encode", "tail"),
    [
        (_sessions_request, (False,)),
        (_turns_request, ("asc", False)),
        (_items_request, ("asc", None)),
        (_search_request, ("needle",)),
    ],
)
@pytest.mark.parametrize("limit", [True, 1.0, 0, 1001])
def test_limit_validation(encode, tail, limit):
    with pytest.raises(ValueError) as caught:
        encode(limit, None, *tail)
    assert str(caught.value) == "Stored history limit must be an integer from 1 to 1000"


@pytest.mark.parametrize(
    ("encode", "args", "error", "message"),
    [
        (_sessions_request, (50, None, 1), TypeError, "archived must be a bool"),
        (_turns_request, (50, None, "asc", 0), TypeError, "include_summary must be a bool"),
        (
            _items_request,
            (50, None, "asc", ""),
            ValueError,
            "turn_id must be a nonempty string or None",
        ),
        (
            _search_request,
            (50, None, "  "),
            ValueError,
            "Stored history search requires nonempty text",
        ),
        (
            _sessions_request,
            (50, "", 1),
            ValueError,
            "Stored history cursor must be a nonempty string or None",
        ),
        (
            _turns_request,
            (50, None, "invalid", 1),
            ValueError,
            "Stored history direction must be asc or desc",
        ),
        (
            _items_request,
            (50, None, "invalid", ""),
            ValueError,
            "Stored history direction must be asc or desc",
        ),
        (
            _search_request,
            (50, "", ""),
            ValueError,
            "Stored history cursor must be a nonempty string or None",
        ),
    ],
)
def test_query_validation_and_precedence(encode, args, error, message):
    with pytest.raises(error) as caught:
        encode(*args)
    assert str(caught.value) == message
