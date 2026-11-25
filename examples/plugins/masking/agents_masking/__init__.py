import json
from collections.abc import Mapping, Sequence

from agents.plugins import Plugin, PluginHooks, ToolResult

_MARKER = "[REDACTED]"
_JSON_MARKER = json.dumps(_MARKER)


def _is_json(text: str) -> bool:
    try:
        json.loads(text, parse_int=str)
    except json.JSONDecodeError:
        return False
    return True


def masking(policy: Mapping[str, Sequence[str]]) -> Plugin:
    """Mask exact literals in successful string results by SDK function tool name."""
    if not isinstance(policy, Mapping) or not policy:
        raise ValueError("Provide a nonempty mapping of tool names to literals.")
    rules = {}
    for name, values in policy.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Provide a nonempty tool name.")
        if not isinstance(values, Sequence) or isinstance(values, str) or not values:
            raise ValueError("Provide a nonempty sequence of literals for each tool.")
        if any(not isinstance(v, str) or not v or v in _JSON_MARKER for v in values):
            raise ValueError("Use nonempty strings absent from the replacement marker.")
        rules[name] = sorted(values, key=len, reverse=True)

    def mask(result: ToolResult) -> str | None:
        values = rules.get(result.call.name)
        if values is None or not any(value in result.text for value in values):
            return None
        text = result.text
        for value in values:
            text = text.replace(value, _MARKER)
        # A replacement can form another match across the edge of the marker.
        if any(value in text for value in values):
            return _JSON_MARKER
        if _is_json(result.text) and not _is_json(text):
            return _JSON_MARKER
        return text

    return Plugin(name="masking", hooks=PluginHooks(filter_result=mask))
