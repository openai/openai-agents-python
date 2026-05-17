from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any

from ..exceptions import UserError
from ..handoffs import Handoff
from ..tool import FunctionTool, Tool


def validate_realtime_tool_names(
    tools: Iterable[Tool],
    handoffs: Iterable[Handoff[Any, Any]],
) -> None:
    """Ensure all model-visible Realtime tool names are unambiguous."""
    sources_by_name: dict[str, list[str]] = {}

    for tool in tools:
        if isinstance(tool, FunctionTool):
            sources_by_name.setdefault(tool.name, []).append("function tool")

    for handoff in handoffs:
        sources_by_name.setdefault(handoff.tool_name, []).append("handoff")

    duplicate_descriptions = [
        f"{name!r} ({_format_sources(sources)})"
        for name, sources in sorted(sources_by_name.items())
        if len(sources) > 1
    ]
    if not duplicate_descriptions:
        return

    plural = "name" if len(duplicate_descriptions) == 1 else "names"
    raise UserError(
        f"Duplicate Realtime tool {plural} found: {', '.join(duplicate_descriptions)}. "
        "Realtime function tool and handoff names must be unique. Rename one of them "
        "before starting the session."
    )


def _format_sources(sources: list[str]) -> str:
    parts = [_format_source_count(source, count) for source, count in Counter(sources).items()]
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return f"{', '.join(parts[:-1])}, and {parts[-1]}"


def _format_source_count(source: str, count: int) -> str:
    if count == 1:
        return source
    return f"{count} {source}s"
