from __future__ import annotations

from typing import Any, Literal

from openai import AsyncOpenAI

from .fake_id import is_local_response_item_id

OpenAIResponsesTransport = Literal["http", "websocket"]

_default_openai_key: str | None = None
_default_openai_client: AsyncOpenAI | None = None
_use_responses_by_default: bool = True
# Source of truth for the default Responses transport.
_default_openai_responses_transport: OpenAIResponsesTransport = "http"
# Backward-compatibility shim for internal code/tests that still mutate the legacy flag directly.
_use_responses_websocket_by_default: bool = False


def set_default_openai_key(key: str) -> None:
    global _default_openai_key
    _default_openai_key = key


def get_default_openai_key() -> str | None:
    return _default_openai_key


def set_default_openai_client(client: AsyncOpenAI) -> None:
    global _default_openai_client
    _default_openai_client = client


def get_default_openai_client() -> AsyncOpenAI | None:
    return _default_openai_client


def set_use_responses_by_default(use_responses: bool) -> None:
    global _use_responses_by_default
    _use_responses_by_default = use_responses


def get_use_responses_by_default() -> bool:
    return _use_responses_by_default


def set_use_responses_websocket_by_default(use_responses_websocket: bool) -> None:
    set_default_openai_responses_transport("websocket" if use_responses_websocket else "http")


def get_use_responses_websocket_by_default() -> bool:
    return get_default_openai_responses_transport() == "websocket"


def set_default_openai_responses_transport(transport: OpenAIResponsesTransport) -> None:
    global _default_openai_responses_transport
    global _use_responses_websocket_by_default
    _default_openai_responses_transport = transport
    _use_responses_websocket_by_default = transport == "websocket"


def get_default_openai_responses_transport() -> OpenAIResponsesTransport:
    global _default_openai_responses_transport
    # Respect direct writes to the legacy private flag (used in tests) by syncing on read.
    legacy_transport: OpenAIResponsesTransport = (
        "websocket" if _use_responses_websocket_by_default else "http"
    )
    if _default_openai_responses_transport != legacy_transport:
        _default_openai_responses_transport = legacy_transport
    return _default_openai_responses_transport


def clean_responses_input_item(item: Any) -> Any | None:
    """Prepare a by-value item for Responses generation or compaction."""
    if not isinstance(item, dict):
        return item
    if item.get("type") == "reasoning" and item.get("provider_data"):
        return None
    strip_id = is_local_response_item_id(item)
    if not strip_id and "provider_data" not in item and "codex_reasoning_summary" not in item:
        return item
    cleaned = item.copy()
    if strip_id:
        cleaned.pop("id")
    cleaned.pop("provider_data", None)
    cleaned.pop("codex_reasoning_summary", None)
    return cleaned


def merge_openai_extra_args(
    kwargs: dict[str, Any],
    extra_args: dict[str, Any],
    *,
    method: str,
    omitted_types: type | tuple[type, ...],
    error_type: type[Exception] = TypeError,
) -> None:
    """Allow raw overrides only where the adapter omitted its own argument."""
    duplicates = sorted(
        key for key in extra_args if key in kwargs and not isinstance(kwargs[key], omitted_types)
    )
    if duplicates:
        noun = "argument" if len(duplicates) == 1 else "arguments"
        keys = ", ".join(repr(key) for key in duplicates)
        raise error_type(f"{method}() got multiple values for keyword {noun} {keys}")
    kwargs.update(extra_args)
