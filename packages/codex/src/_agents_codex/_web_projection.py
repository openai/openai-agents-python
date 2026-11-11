"""Project core and extension web search items into the public contract."""

from typing import Any as Any

from ._events import WebSearchAction as WebSearchAction, WebSearchActionSearch
from ._public import preserve_module_identity as _preserve_module_identity


def _action(native: dict[str, Any]) -> WebSearchAction | None:
    action = native.get("action")
    if action is None:
        query = native.get("query")
        return (
            {"type": "search", "query": query, "queries": None} if isinstance(query, str) else None
        )
    match action.get("type"):
        case "search":
            search: WebSearchActionSearch = {
                "type": "search",
                "query": action.get("query"),
                "queries": action.get("queries"),
            }
            if action.get("sources") is not None:
                search["sources"] = action["sources"]
            return search
        case "open_page" | "openPage":
            return {"type": "open_page", "url": action.get("url")}
        case "find_in_page" | "findInPage":
            return {
                "type": "find_in_page",
                "url": action.get("url"),
                "pattern": action.get("pattern"),
            }
        case _:
            return {"type": "other"}


def project_web_search_item(
    native: dict[str, Any], done: bool, turn_id: str | None
) -> dict[str, Any] | None:
    if native.get("type") != "WebSearch" and not (
        native.get("type") == "Extension" and native.get("kind") == "web.search"
    ):
        return None
    item_id = native.get("id")
    if not isinstance(item_id, str) or not item_id:
        return None
    return {
        "type": "web_search_call",
        "id": item_id,
        "turn_id": turn_id,
        "status": "completed" if done else "in_progress",
        "action": _action(native),
        **({"results": native["results"]} if native.get("results") is not None else {}),
    }


_preserve_module_identity(globals(), "agents._web_projection")
