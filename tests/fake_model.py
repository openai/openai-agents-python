from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from openai.types.responses import Response, ResponseCompletedEvent, ResponseUsage
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails

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
from agents.tracing import SpanError, generation_span
from agents.usage import Usage


class FakeModel(Model):
    def __init__(
        self,
        tracing_enabled: bool = False,
        initial_output: list[TResponseOutputItem] | Exception | None = None,
    ):
        if initial_output is None:
            initial_output = []
        self.turn_outputs: list[list[TResponseOutputItem] | Exception] = (
            [initial_output] if initial_output else []
        )
        self.tracing_enabled = tracing_enabled
        self.last_turn_args: dict[str, Any] = {}
        self.first_turn_args: dict[str, Any] | None = None
        self.hardcoded_usage: Usage | None = None

    def set_hardcoded_usage(self, usage: Usage):
        self.hardcoded_usage = usage

    def set_next_output(self, output: list[TResponseOutputItem] | Exception):
        self.turn_outputs.append(output)

    def add_multiple_turn_outputs(self, outputs: list[list[TResponseOutputItem] | Exception]):
        self.turn_outputs.extend(outputs)

    def get_next_output(self) -> list[TResponseOutputItem] | Exception:
        if not self.turn_outputs:
            return []
        return self.turn_outputs.pop(0)

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
        prompt: Any | None,
    ) -> ModelResponse:
        turn_args = {
            "system_instructions": system_instructions,
            "input": input,
            "model_settings": model_settings,
            "tools": tools,
            "output_schema": output_schema,
            "previous_response_id": previous_response_id,
            "conversation_id": conversation_id,
        }

        if self.first_turn_args is None:
            self.first_turn_args = turn_args.copy()

        self.last_turn_args = turn_args

        with generation_span(disabled=not self.tracing_enabled) as span:
            output = self.get_next_output()

            if isinstance(output, Exception):
                span.set_error(
                    SpanError(
                        message="Error",
                        data={
                            "name": output.__class__.__name__,
                            "message": str(output),
                        },
                    )
                )
                raise output

            return ModelResponse(
                output=output,
                usage=self.hardcoded_usage or Usage(),
                response_id="resp-789",
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
        previous_response_id: str | None = None,
        conversation_id: str | None = None,
        prompt: Any | None = None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        turn_args = {
            "system_instructions": system_instructions,
            "input": input,
            "model_settings": model_settings,
            "tools": tools,
            "output_schema": output_schema,
            "previous_response_id": previous_response_id,
            "conversation_id": conversation_id,
        }

        if self.first_turn_args is None:
            self.first_turn_args = turn_args.copy()

        self.last_turn_args = turn_args
        with generation_span(disabled=not self.tracing_enabled) as span:
            output = self.get_next_output()
            if isinstance(output, Exception):
                span.set_error(
                    SpanError(
                        message="Error",
                        data={
                            "name": output.__class__.__name__,
                            "message": str(output),
                        },
                    )
                )
                raise output

            yield ResponseCompletedEvent(
                type="response.completed",
                response=get_response_obj(output, usage=self.hardcoded_usage),
                sequence_number=0,
            )


def get_response_obj(
    output: list[TResponseOutputItem],
    response_id: str | None = None,
    usage: Usage | None = None,
) -> Response:
    return Response(
        id=response_id or "resp-789",
        created_at=123,
        model="test_model",
        object="response",
        output=output,
        tool_choice="none",
        tools=[],
        top_p=None,
        parallel_tool_calls=False,
        usage=ResponseUsage(
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            total_tokens=usage.total_tokens if usage else 0,
            input_tokens_details=InputTokensDetails(cached_tokens=0),
            output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        ),
    )
