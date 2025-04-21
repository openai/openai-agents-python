from __future__ import annotations

from openai import AsyncOpenAI

from ..model_settings import ModelSettings
from ..version import __version__

_USER_AGENT = f"Agents/Python {__version__}"
HEADERS = {"User-Agent": _USER_AGENT}


class ChatCmplHelpers:
    @classmethod
    def is_openai(cls, client: AsyncOpenAI):
        return str(client.base_url).startswith("https://api.openai.com")

    @classmethod
    def get_store_param(cls, client: AsyncOpenAI, model_settings: ModelSettings) -> bool | None:
        # Match the behavior of Responses where store is True when not given
        default_store = True if cls.is_openai(client) else None
        return model_settings.store if model_settings.store is not None else default_store

    @classmethod
    def get_stream_options_param(
        cls, client: AsyncOpenAI, model_settings: ModelSettings, stream: bool
    ) -> dict[str, bool] | None:
        if not stream:
            return None

        default_include_usage = True if cls.is_openai(client) else None
        include_usage = (
            model_settings.include_usage
            if model_settings.include_usage is not None
            else default_include_usage
        )
        stream_options = {"include_usage": include_usage} if include_usage is not None else None
        return stream_options
