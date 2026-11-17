from collections.abc import Mapping

FAKE_RESPONSES_ID = "__fake_id__"
"""This is a placeholder ID used to fill in the `id` field in Responses API related objects. It's
useful when you're creating Responses objects from non-Responses APIs, e.g. the OpenAI Chat
Completions API or other LLM providers.
"""


def is_local_response_item_id(item: Mapping[str, object]) -> bool:
    """Identify IDs that cannot name an item in the OpenAI Responses service."""
    item_id = item.get("id")
    if item_id == FAKE_RESPONSES_ID:
        return True
    # Stock Codex identifies SDK tool stream events by the call's correlation ID.
    # Replay still needs call_id to pair a result, but the service assigns its own item ID.
    return (
        item.get("type") in ("function_call", "shell_call", "apply_patch_call")
        and isinstance(item_id, str)
        and item_id.startswith("call_")
        and item_id == item.get("call_id")
    )
