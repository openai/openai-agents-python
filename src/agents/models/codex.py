from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from dataclasses import fields
from importlib import import_module
from pathlib import Path
from typing import TypedDict, cast

from openai.types.responses import (
    Response,
    ResponseCompletedEvent,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseTextDeltaEvent,
    ResponseUsage,
)
from openai.types.responses.response_prompt_param import ResponsePromptParam
from openai.types.responses.response_usage import OutputTokensDetails

from ..agent_output import AgentOutputSchemaBase
from ..exceptions import UserError
from ..handoffs import Handoff
from ..items import ItemHelpers, ModelResponse, TResponseInputItem, TResponseStreamEvent
from ..model_settings import ModelSettings
from ..tool import Tool
from ..usage import (
    Usage,
    _make_input_tokens_details,
    _mark_request_completed_without_usage,
    _response_usage_to_usage,
)
from .default_models import get_default_model_settings
from .interface import Model, ModelProvider, ModelTracing


class CodexProvider(ModelProvider):
    """Run text requests through the embedded Codex runtime.

    Install the optional native package from `native/codex` in the same Python environment.
    """

    def __init__(self, *, cwd: str | os.PathLike[str] | None = None) -> None:
        """Set the working directory for Codex. Defaults to the current directory."""
        self._cwd = str(Path(cwd).resolve() if cwd is not None else Path.cwd())

    def get_model(self, model_name: str | None) -> Model:
        """Resolve a Codex model name. With no name, Codex selects its default."""
        return _CodexModel(model_name, self._cwd)


class _CodexTurn(TypedDict):
    text: str
    model: str
    item_id: str
    turn_id: str
    input_tokens: int | None
    cached_input_tokens: int | None
    cache_write_input_tokens: int | None
    output_tokens: int | None
    reasoning_output_tokens: int | None
    total_tokens: int | None


def _text_messages(input: str | list[TResponseInputItem]) -> list[tuple[str, str]]:
    messages: list[tuple[str, str]] = []
    for item in ItemHelpers.input_to_new_input_list(input):
        role = item.get("role")
        if (
            item.get("type", "message") != "message"
            or not isinstance(role, str)
            or role not in ("user", "assistant")
        ):
            raise UserError("CodexProvider accepts only user and assistant text messages.")
        content = item.get("content")
        if isinstance(content, str):
            messages.append((role, content))
            continue
        text: list[str] = []
        if not isinstance(content, list):
            raise UserError("CodexProvider accepts only user and assistant text messages.")
        for part in content:
            if part.get("type") not in ("input_text", "output_text"):
                raise UserError("CodexProvider accepts only user and assistant text messages.")
            text.append(part["text"])
        messages.append((role, "\n".join(text)))
    if not messages or messages[-1][0] != "user" or not messages[-1][1]:
        raise UserError("CodexProvider requires a final user message with text.")
    return messages


class _CodexModel(Model):
    def __init__(self, name: str | None, cwd: str) -> None:
        self._name = name
        self._cwd = cwd

    def get_default_model_settings(self) -> ModelSettings:
        return ModelSettings()

    async def _run(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> Response:
        if tools or handoffs or output_schema is not None:
            raise UserError("CodexProvider supports text output without SDK tools or handoffs.")
        if previous_response_id is not None or conversation_id is not None or prompt is not None:
            raise UserError("CodexProvider does not support Responses API state or prompt IDs.")
        # The runner fills reasoning and verbosity defaults for unprefixed model names.
        defaults = get_default_model_settings(self._name) if self._name is not None else None
        for field in fields(model_settings):
            value = getattr(model_settings, field.name)
            if value is not None and field.name != "timeout":
                if defaults is not None and value == getattr(defaults, field.name):
                    continue
                raise UserError(f"CodexProvider does not support ModelSettings.{field.name}.")

        messages = _text_messages(input)
        try:
            native = import_module("_openai_agents_codex")
        except ModuleNotFoundError as exc:
            if exc.name != "_openai_agents_codex":
                raise
            raise UserError(
                "Install the Codex extension from ./native/codex to use CodexProvider."
            ) from None

        result = cast(
            _CodexTurn,
            await native.run_turn(
                messages[-1][1], system_instructions, self._name, self._cwd, messages[:-1]
            ),
        )
        usage = (
            ResponseUsage(
                input_tokens=result["input_tokens"] or 0,
                input_tokens_details=_make_input_tokens_details(
                    cached_tokens=result["cached_input_tokens"] or 0,
                    cache_write_tokens=result["cache_write_input_tokens"],
                ),
                output_tokens=result["output_tokens"] or 0,
                output_tokens_details=OutputTokensDetails(
                    reasoning_tokens=result["reasoning_output_tokens"] or 0,
                ),
                total_tokens=result["total_tokens"],
            )
            if result["total_tokens"] is not None
            else None
        )
        response = Response(
            id=result["turn_id"],
            created_at=time.time(),
            model=result["model"],
            object="response",
            output=[
                ResponseOutputMessage(
                    id=result["item_id"],
                    content=[
                        ResponseOutputText(type="output_text", text=result["text"], annotations=[])
                    ],
                    role="assistant",
                    status="completed",
                    type="message",
                )
            ],
            parallel_tool_calls=False,
            status="completed",
            tool_choice="none",
            tools=[],
            usage=usage,
        )
        if usage is None:
            _mark_request_completed_without_usage(response)
        return response

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> ModelResponse:
        response = await self._run(
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            previous_response_id,
            conversation_id,
            prompt,
        )
        return ModelResponse(
            output=response.output,
            usage=(
                _response_usage_to_usage(response.usage)
                if response.usage is not None
                else Usage(requests=1)
            ),
            response_id=response.id,
        )

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        response = await self._run(
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            previous_response_id,
            conversation_id,
            prompt,
        )
        message = cast(ResponseOutputMessage, response.output[0])
        text = cast(ResponseOutputText, message.content[0])
        yield ResponseTextDeltaEvent(
            content_index=0,
            delta=text.text,
            item_id=message.id,
            logprobs=[],
            output_index=0,
            sequence_number=0,
            type="response.output_text.delta",
        )
        yield ResponseCompletedEvent(
            response=response, sequence_number=1, type="response.completed"
        )
