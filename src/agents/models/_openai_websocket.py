from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx2
from openai import AsyncOpenAI, NotGiven, Omit

from .._httpx_compat import is_legacy_httpx_instance
from ..exceptions import UserError


def _is_openai_omitted_value(value: Any) -> bool:
    return isinstance(value, Omit | NotGiven)


async def refresh_openai_client_api_key_if_supported(client: Any) -> None:
    """Refresh dynamic OpenAI client credentials before materializing handshake headers."""
    refresh_api_key = getattr(client, "_refresh_api_key", None)
    if callable(refresh_api_key):
        await refresh_api_key()


def _set_header(headers: dict[str, str], key: object, value: object) -> None:
    header_key = str(key)
    for existing_key in list(headers):
        if existing_key.lower() == header_key.lower():
            del headers[existing_key]
    headers[header_key] = str(value)


def merge_openai_client_websocket_headers(
    client: AsyncOpenAI,
    *,
    extra_headers: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Materialize OpenAI client auth/default headers for a WebSocket handshake."""
    headers: dict[str, str] = {}
    for source in (
        getattr(client, "auth_headers", {}),
        getattr(client, "default_headers", {}),
    ):
        for key, value in source.items():
            if _is_openai_omitted_value(value):
                continue
            _set_header(headers, key, value)

    for key, value in (extra_headers or {}).items():
        if isinstance(value, NotGiven):
            continue
        header_key = str(key)
        for existing_key in list(headers):
            if existing_key.lower() == header_key.lower():
                del headers[existing_key]
        if isinstance(value, Omit):
            continue
        headers[header_key] = str(value)

    return headers


def _merge_query_values(params: dict[str, Any], values: Mapping[str, Any]) -> None:
    for key, value in values.items():
        query_key = str(key)
        if isinstance(value, Omit):
            params.pop(query_key, None)
            continue
        if isinstance(value, NotGiven):
            continue
        params[query_key] = value


def prepare_openai_client_websocket_base_url(
    client: AsyncOpenAI,
    *,
    extra_query: Any = None,
    context: str,
) -> httpx2.URL:
    """Build the client-derived WebSocket base URL and normalized query parameters.

    Endpoint suffixes and transport-specific fixed query parameters are intentionally left to
    each caller.
    """
    websocket_base_url = getattr(client, "websocket_base_url", None)
    if websocket_base_url is not None:
        if is_legacy_httpx_instance(websocket_base_url, "URL"):
            websocket_base_url = str(websocket_base_url)
        base_url = httpx2.URL(websocket_base_url)
    else:
        client_base_url = client.base_url
        if is_legacy_httpx_instance(client_base_url, "URL"):
            client_base_url = str(client_base_url)
        base_url = httpx2.URL(client_base_url)

    ws_scheme = {"http": "ws", "https": "wss"}.get(base_url.scheme, base_url.scheme)
    base_url = base_url.copy_with(scheme=ws_scheme)
    params: dict[str, Any] = dict(base_url.params)

    default_query = getattr(client, "default_query", None)
    if default_query is not None and not _is_openai_omitted_value(default_query):
        if not isinstance(default_query, Mapping):
            raise UserError(f"{context} client default_query must be a mapping.")
        _merge_query_values(params, default_query)

    if extra_query is not None and not _is_openai_omitted_value(extra_query):
        if not isinstance(extra_query, Mapping):
            raise UserError(f"{context} extra_query must be a mapping.")
        _merge_query_values(params, extra_query)

    return base_url.copy_with(params=params)
