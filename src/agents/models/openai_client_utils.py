from __future__ import annotations

from urllib.parse import urlsplit

from openai import AsyncOpenAI


def is_official_openai_base_url(base_url: object, *, websocket: bool = False) -> bool:
    parsed = urlsplit(str(base_url))
    try:
        port = parsed.port
    except ValueError:
        return False

    expected_scheme = "wss" if websocket else "https"
    return (
        parsed.scheme == expected_scheme
        and parsed.hostname == "api.openai.com"
        and port in (None, 443)
    )


def is_official_openai_client(client: AsyncOpenAI) -> bool:
    base_url = getattr(client, "base_url", None)
    if base_url is None:
        return False
    return is_official_openai_base_url(base_url)
