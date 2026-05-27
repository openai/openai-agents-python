from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ..model_settings import ModelSettings


def sanitize_url_for_trace(url: object) -> str:
    """Return a URL safe for tracing by removing auth material and request parameters."""
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return ""

    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def model_config_for_trace(
    model_settings: ModelSettings,
    *,
    base_url: object | None = None,
    extra_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = model_settings.to_traceable_dict()
    if base_url is not None:
        config["base_url"] = sanitize_url_for_trace(base_url)
    if extra_config:
        config.update(extra_config)
    return config
