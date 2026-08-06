from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

from openai import AsyncOpenAI
from openai.types.chat.chat_completion_token_logprob import ChatCompletionTokenLogprob
from openai.types.responses.response_output_text import (
    Annotation as ResponseOutputTextAnnotation,
    AnnotationURLCitation,
    Logprob,
    LogprobTopLogprob,
)
from openai.types.responses.response_text_delta_event import (
    Logprob as DeltaLogprob,
    LogprobTopLogprob as DeltaTopLogprob,
)
from pydantic import ValidationError

from ..logger import log_model_action_debug, logger
from ..model_settings import ModelSettings
from ..version import __version__
from .openai_client_utils import is_official_openai_client


def _mapping_or_attr(source: Any, name: str) -> Any:
    """Read a field from a value that is either a typed object or a plain mapping."""
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


_USER_AGENT = f"Agents/Python {__version__}"
HEADERS = {"User-Agent": _USER_AGENT}

HEADERS_OVERRIDE: ContextVar[dict[str, str] | None] = ContextVar(
    "openai_chatcompletions_headers_override", default=None
)


class ChatCmplHelpers:
    @classmethod
    def is_openai(cls, client: AsyncOpenAI) -> bool:
        return is_official_openai_client(client)

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

    @classmethod
    def convert_url_citations(cls, raw_annotations: Any) -> list[ResponseOutputTextAnnotation]:
        """Convert Chat Completions url citations into output text annotations."""
        # Providers report annotations as typed objects or as raw payloads, so validate
        # rather than assume the declared shape.
        if not isinstance(raw_annotations, list | tuple):
            return []

        annotations: list[ResponseOutputTextAnnotation] = []
        for annotation in raw_annotations:
            url_citation = _mapping_or_attr(annotation, "url_citation")
            if _mapping_or_attr(annotation, "type") != "url_citation" or url_citation is None:
                continue
            try:
                annotations.append(
                    AnnotationURLCitation.model_validate(
                        {
                            "type": "url_citation",
                            "start_index": _mapping_or_attr(url_citation, "start_index"),
                            "end_index": _mapping_or_attr(url_citation, "end_index"),
                            "url": _mapping_or_attr(url_citation, "url"),
                            "title": _mapping_or_attr(url_citation, "title"),
                        }
                    )
                )
            except ValidationError as exc:
                # A provider that reports an incomplete citation should not fail the turn.
                log_model_action_debug(logger, "Skipping malformed url citation", exc)
        return annotations

    @classmethod
    def clean_gemini_tool_call_id(cls, tool_call_id: str, model: str | None = None) -> str:
        """Clean up litellm's __thought__ suffix from Gemini tool call IDs.

        LiteLLM adds a "__thought__" suffix to Gemini tool call IDs to track thought
        signatures. This suffix is redundant since we can get thought_signature from
        provider_specific_fields, and this hack causes validation errors when cross-model
        passing to other models.

        See: https://github.com/BerriAI/litellm/pull/16895

        Args:
            tool_call_id: The tool call ID to clean.
            model: The model name (used to check if it's a Gemini model).

        Returns:
            The cleaned tool call ID with "__thought__" suffix removed if present.
        """
        if model and "gemini" in model.lower() and "__thought__" in tool_call_id:
            return tool_call_id.split("__thought__")[0]
        return tool_call_id
