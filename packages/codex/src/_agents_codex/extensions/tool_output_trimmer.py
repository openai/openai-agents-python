"""Trim older tool outputs before native model requests."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from _agents_shared.tool_output_trimming import ToolOutputTrimming

from .._public import preserve_module_identity as _preserve_module_identity
from ..model_context import ModelRequestContext

logger = logging.getLogger(__name__)


def _trace_name(item: dict[str, Any]) -> str | None:
    name = item.get("name")
    if not isinstance(name, str) or not name:
        return None
    namespace = item.get("namespace")
    return f"{namespace}.{name}" if isinstance(namespace, str) and namespace else name


@dataclass
class ToolOutputTrimmer(ToolOutputTrimming):
    """Project older tool outputs with `Agent(transform_context=...)`.

    The last `recent_turns` user messages and their following items retain
    their full request context. Older function outputs over `max_output_chars`
    get a text preview of up to `preview_chars` characters. Tool search results
    retain their schemas with less prose. Use `trimmable_tools` to select bare
    names or qualified names such as `workspace.search`.

    This callback edits the request projection. The Session keeps its original
    history, and the native runtime may have capped large results before the
    callback receives them.
    """

    def __call__(self, request: ModelRequestContext[Any]) -> list[dict[str, Any]] | None:
        return self._trim_items(request.items, _trace_name, logger)


_preserve_module_identity(globals(), "agents.extensions.tool_output_trimmer")
