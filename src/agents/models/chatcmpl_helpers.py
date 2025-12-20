from __future__ import annotations

from contextvars import ContextVar

from openai import AsyncOpenAI
from openai.types.chat.chat_completion_token_logprob import ChatCompletionTokenLogprob
from openai.types.responses.response_output_text import Logprob, LogprobTopLogprob
from openai.types.responses.response_text_delta_event import (
    Logprob as DeltaLogprob,
    LogprobTopLogprob as DeltaTopLogprob,
)

from ..model_settings import ModelSettings
from ..version import __version__

_USER_AGENT = f"Agents/Python {__version__}"
HEADERS = {"User-Agent": _USER_AGENT}

HEADERS_OVERRIDE: ContextVar[dict[str, str] | None] = ContextVar(
    "openai_chatcompletions_headers_override", default=None
)


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

    @classmethod
    def convert_logprobs_for_output_text(
        cls, logprobs: list[ChatCompletionTokenLogprob] | None
    ) -> list[Logprob] | None:
        if not logprobs:
            return None

        converted: list[Logprob] = []
        for token_logprob in logprobs:
            converted.append(
                Logprob(
                    token=token_logprob.token,
                    logprob=token_logprob.logprob,
                    bytes=token_logprob.bytes or [],
                    top_logprobs=[
                        LogprobTopLogprob(
                            token=top_logprob.token,
                            logprob=top_logprob.logprob,
                            bytes=top_logprob.bytes or [],
                        )
                        for top_logprob in token_logprob.top_logprobs
                    ],
                )
            )
        return converted

    @classmethod
    def convert_logprobs_for_text_delta(
        cls, logprobs: list[ChatCompletionTokenLogprob] | None
    ) -> list[DeltaLogprob] | None:
        if not logprobs:
            return None

        converted: list[DeltaLogprob] = []
        for token_logprob in logprobs:
            converted.append(
                DeltaLogprob(
                    token=token_logprob.token,
                    logprob=token_logprob.logprob,
                    top_logprobs=[
                        DeltaTopLogprob(
                            token=top_logprob.token,
                            logprob=top_logprob.logprob,
                        )
                        for top_logprob in token_logprob.top_logprobs
                    ]
                    or None,
                )
            )
        return converted
