from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from copy import deepcopy
from typing import Any, cast

from openai.types.responses.response_prompt_param import ResponsePromptParam

from agents.agent_output import AgentOutputSchemaBase
from agents.handoffs import Handoff
from agents.items import (
    ModelResponse,
    TResponseInputItem,
    TResponseOutputItem,
    TResponseStreamEvent,
)
from agents.model_settings import ModelSettings
from agents.models.interface import Model, ModelTracing
from agents.tool import Tool
from agents.usage import Usage


class QueuedFakeModel(Model):
    """Deterministic non-streaming model for installed-distribution contracts."""

    def __init__(self, turns: Sequence[Sequence[TResponseOutputItem]]) -> None:
        self._turns = [list(turn) for turn in turns]
        self.requests: list[dict[str, Any]] = []

    def _record_request(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> None:
        self.requests.append(
            {
                "system_instructions": system_instructions,
                "input": deepcopy(input),
                "model_settings": model_settings,
                "tools": list(tools),
                "output_schema": output_schema,
                "handoffs": list(handoffs),
                "tracing": tracing,
                "previous_response_id": previous_response_id,
                "conversation_id": conversation_id,
                "prompt": deepcopy(prompt),
            }
        )

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
        self._record_request(
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            previous_response_id,
            conversation_id,
            prompt,
        )
        if not self._turns:
            raise AssertionError("QueuedFakeModel received an unexpected model request")
        return ModelResponse(
            output=self._turns.pop(0),
            usage=Usage(requests=1),
            response_id="queued-fake-response",
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
        self._record_request(
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            previous_response_id,
            conversation_id,
            prompt,
        )
        if False:
            yield cast(TResponseStreamEvent, None)
        raise AssertionError("QueuedFakeModel does not support streaming")
