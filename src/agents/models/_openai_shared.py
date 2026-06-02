from __future__ import annotations

from typing import Literal

from openai import AsyncOpenAI

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
