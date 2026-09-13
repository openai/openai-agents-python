from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from ..model_settings import ModelSettings
from ..usage import Usage, _requests_for_response_without_usage, model_usage_to_span_usage

if TYPE_CHECKING:
    from openai.types.responses import Response

    from ..tracing.span_data import GenerationSpanData
    from ..tracing.spans import Span
    from .interface import ModelTracing


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


def populate_generation_span(
    span_generation: Span[GenerationSpanData],
    final_response: Response,
    tracing: ModelTracing,
) -> None:
    """Populate generation trace data from a Chat Completions adapter response."""
    if tracing.include_data():
        span_generation.span_data.output = [final_response.model_dump()]

    if final_response.usage is not None:
        span_generation.span_data.usage = {
            "requests": 1,
            "input_tokens": final_response.usage.input_tokens,
            "output_tokens": final_response.usage.output_tokens,
            "total_tokens": final_response.usage.total_tokens,
            "input_tokens_details": (
                final_response.usage.input_tokens_details.model_dump()
                if final_response.usage.input_tokens_details is not None
                else {"cached_tokens": 0, "cache_write_tokens": 0}
            ),
            "output_tokens_details": (
                final_response.usage.output_tokens_details.model_dump()
                if final_response.usage.output_tokens_details is not None
                else {"reasoning_tokens": 0}
            ),
        }
    elif _requests_for_response_without_usage(final_response):
        # Keep streamed tracing aligned with the non-streaming path, which records the
        # request even when the provider reports no usage.
        span_generation.span_data.usage = model_usage_to_span_usage(Usage(requests=1))
