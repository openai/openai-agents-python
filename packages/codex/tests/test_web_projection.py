import pytest

from agents._web_projection import project_web_search_item


@pytest.mark.parametrize(
    ("native_type", "action", "expected"),
    [
        (
            {"type": "WebSearch"},
            {"type": "search", "query": "python", "queries": ["python", "rust"]},
            {"type": "search", "query": "python", "queries": ["python", "rust"]},
        ),
        (
            {"type": "WebSearch"},
            {"type": "open_page", "url": "https://example.com"},
            {"type": "open_page", "url": "https://example.com"},
        ),
        (
            {"type": "Extension", "kind": "web.search"},
            {"type": "openPage", "url": "https://example.com"},
            {"type": "open_page", "url": "https://example.com"},
        ),
        (
            {"type": "Extension", "kind": "web.search"},
            {"type": "findInPage", "url": "https://example.com", "pattern": "text"},
            {"type": "find_in_page", "url": "https://example.com", "pattern": "text"},
        ),
        (
            {"type": "WebSearch"},
            {"type": "find_in_page", "pattern": "text"},
            {"type": "find_in_page", "url": None, "pattern": "text"},
        ),
        (
            {"type": "WebSearch"},
            {"type": "other"},
            {"type": "other"},
        ),
    ],
)
def test_web_search_preserves_action_and_lifecycle(native_type, action, expected):
    native = {**native_type, "id": "search-1", "query": "display", "action": action}

    started = project_web_search_item(native, False, "turn-1")
    completed = project_web_search_item(native, True, "turn-1")

    assert started == {
        "type": "web_search_call",
        "id": "search-1",
        "turn_id": "turn-1",
        "status": "in_progress",
        "action": expected,
    }
    assert completed == {**started, "status": "completed"}


def test_web_search_null_action_uses_native_query():
    item = project_web_search_item(
        {
            "type": "Extension",
            "kind": "web.search",
            "id": "search-1",
            "query": "native query",
            "action": None,
        },
        False,
        "turn-1",
    )

    assert item is not None
    assert item["action"] == {"type": "search", "query": "native query", "queries": None}


@pytest.mark.parametrize(
    "native",
    [
        {"type": "Extension", "kind": "clock.sleep", "id": "sleep-1"},
        {"type": "WebSearch", "query": "native query"},
    ],
)
def test_web_search_requires_supported_kind_and_native_id(native):
    assert project_web_search_item(native, True, "turn-1") is None
