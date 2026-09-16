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
from openai.types.responses.response_prompt_param import ResponsePromptParam
from openai.types.responses.response_text_delta_event import (
    Logprob as DeltaLogprob,
    LogprobTopLogprob as DeltaTopLogprob,
)
from pydantic import ValidationError

from ..exceptions import UserError
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


class ChatCmplUnsupportedFeatures:
    """Warn about, or reject, Responses-only features a Chat Completions call cannot carry.

    Two adapters in this SDK speak Chat Completions: :class:`OpenAIChatCompletionsModel`
    and ``LitellmModel``. Both hit the same wall — the API has no server-managed
    conversation state, no reusable prompts, and only ``reasoning.effort`` — so the
    decision of what to say and whether to raise lives here once instead of in each
    adapter.

    Args:
        model_class_name: Name of the adapter, used in the message so a reader knows
            which model dropped the feature.
        strict: Whether to raise :class:`UserError` instead of warning once.
    """

    def __init__(self, model_class_name: str, strict: bool) -> None:
        self._model_class_name = model_class_name
        self._strict = strict
        self._warned_prompt = False
        self._warned_conversation_state = False
        self._warned_reasoning_settings = False

    def _raise_or_warn_once(self, message: str, hint: str, warned_attr: str) -> None:
        if self._strict:
            raise UserError(message)

        if not getattr(self, warned_attr):
            logger.warning("%s %s", message, hint)
            setattr(self, warned_attr, True)

    def check_prompt(self, prompt: ResponsePromptParam | None) -> None:
        """Handle a reusable prompt, which only the Responses API can resolve."""
        if prompt is None:
            return

        message = (
            "Reusable prompts are only supported by the Responses API. "
            f"{self._model_class_name} does not support `prompt`; use a Responses model "
            "instead."
        )
        self._raise_or_warn_once(
            message,
            "Ignoring `prompt`; enable strict feature validation to raise an error instead.",
            "_warned_prompt",
        )

    def check_server_managed_conversation_state(
        self,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
    ) -> None:
        """Handle ids that point at history living on the server, not in the request.

        The runner sends only the new items once either id is set, so an adapter that
        drops them silently sends a conversation with its earlier turns missing.
        """
        unsupported: list[str] = []
        if previous_response_id is not None:
            unsupported.append("previous_response_id")
        if conversation_id is not None:
            unsupported.append("conversation_id")
        if not unsupported:
            return

        unsupported_params = ", ".join(unsupported)
        message = (
            f"{self._model_class_name} does not support server-managed conversation state "
            f"({unsupported_params}). Chat Completions requires callers to pass the full "
            "conversation history; use a Responses API model for previous_response_id or a "
            "conversation-capable model for conversation_id."
        )
        self._raise_or_warn_once(
            message,
            "Ignoring unsupported server-managed conversation state; enable strict feature "
            "validation to raise an error instead.",
            "_warned_conversation_state",
        )

    def check_reasoning_settings(self, model_settings: ModelSettings) -> None:
        """Handle the reasoning settings that need the Responses API."""
        reasoning = model_settings.reasoning
        if reasoning is None:
            return

        unsupported = [
            name for name in ("mode", "context") if getattr(reasoning, name, None) is not None
        ]
        if not unsupported:
            return

        unsupported_params = ", ".join(f"reasoning.{name}" for name in unsupported)
        message = (
            f"{self._model_class_name} does not support {unsupported_params}. "
            "These reasoning settings require the Responses API; Chat Completions only "
            "uses reasoning.effort."
        )
        self._raise_or_warn_once(
            message,
            "Ignoring unsupported reasoning settings; enable strict feature validation "
            "to raise an error instead.",
            "_warned_reasoning_settings",
        )


def owns_chatcmpl_feature_validation(model: Any) -> bool:
    """Whether ``model`` is a Chat Completions adapter that handles the ids itself.

    Used by the runner to tell an adapter that warns about (or rejects)
    ``previous_response_id`` / ``conversation_id`` from one that really does keep the
    conversation on a server.
    """
    return isinstance(getattr(model, "_unsupported_features", None), ChatCmplUnsupportedFeatures)
