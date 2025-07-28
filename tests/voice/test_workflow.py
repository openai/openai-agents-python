from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from inline_snapshot import snapshot
from openai.types.responses import ResponseCompletedEvent
from openai.types.responses.response_text_delta_event import ResponseTextDeltaEvent

from agents import Agent, Model, ModelSettings, ModelTracing, Tool
from agents.agent_output import AgentOutputSchemaBase
from agents.handoffs import Handoff
from agents.items import (
    ModelResponse,
    TResponseInputItem,
    TResponseOutputItem,
    TResponseStreamEvent,
)

from ..fake_model import get_response_obj
from ..test_responses import get_function_tool, get_function_tool_call, get_text_message

try:
    from agents.voice import SingleAgentVoiceWorkflow

except ImportError:
    pass


class FakeStreamingModel(Model):
    def __init__(self):
        self.turn_outputs: list[list[TResponseOutputItem]] = []

    def set_next_output(self, output: list[TResponseOutputItem]):
        self.turn_outputs.append(output)

    def add_multiple_turn_outputs(self, outputs: list[list[TResponseOutputItem]]):
        self.turn_outputs.extend(outputs)

    def get_next_output(self) -> list[TResponseOutputItem]:
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
        prompt: Any | None,
    ) -> ModelResponse:
        raise NotImplementedError("Not implemented")

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
        prompt: Any | None,
    ) -> AsyncIterator[TResponseStreamEvent]:
        output = self.get_next_output()
        for item in output:
            if (
                item.type == "message"
                and len(item.content) == 1
                and item.content[0].type == "output_text"
            ):
                yield ResponseTextDeltaEvent(
                    content_index=0,
                    delta=item.content[0].text,
                    type="response.output_text.delta",
                    output_index=0,
                    item_id=item.id,
                    sequence_number=0,
                    logprobs=[],
                )

        yield ResponseCompletedEvent(
            type="response.completed",
            response=get_response_obj(output),
            sequence_number=1,
        )


@pytest.mark.asyncio
async def test_single_agent_workflow(monkeypatch) -> None:
    model = FakeStreamingModel()
    model.add_multiple_turn_outputs(
        [
            # First turn: a message and a tool call
            [
                get_function_tool_call("some_function", json.dumps({"a": "b"})),
                get_text_message("a_message"),
            ],
            # Second turn: text message
            [get_text_message("done")],
        ]
    )

    agent = Agent(
        "initial_agent",
        model=model,
        tools=[get_function_tool("some_function", "tool_result")],
    )

    workflow = SingleAgentVoiceWorkflow(agent)
    output = []
    async for chunk in workflow.run("transcription_1"):
        output.append(chunk)

    # Validate that the text yielded matches our fake events
    assert output == ["a_message", "done"]
    # Validate that internal state was updated
    assert workflow._input_history == snapshot(
        [
            {"content": "transcription_1", "role": "user"},
            {
                "arguments": '{"a": "b"}',
                "call_id": "2",
                "name": "some_function",
                "type": "function_call",
                "id": "1",
            },
            {
                "id": "1",
                "content": [{"annotations": [], "text": "a_message", "type": "output_text"}],
                "role": "assistant",
                "status": "completed",
                "type": "message",
            },
            {"call_id": "2", "output": "tool_result", "type": "function_call_output"},
            {
                "id": "1",
                "content": [{"annotations": [], "text": "done", "type": "output_text"}],
                "role": "assistant",
                "status": "completed",
                "type": "message",
            },
        ]
    )
    assert workflow._current_agent == agent

    model.set_next_output([get_text_message("done_2")])

    # Run it again with a new transcription to make sure the input history is updated
    output = []
    async for chunk in workflow.run("transcription_2"):
        output.append(chunk)

    assert workflow._input_history == snapshot(
        [
            {"role": "user", "content": "transcription_1"},
            {
                "arguments": '{"a": "b"}',
                "call_id": "2",
                "name": "some_function",
                "type": "function_call",
                "id": "1",
            },
            {
                "id": "1",
                "content": [{"annotations": [], "text": "a_message", "type": "output_text"}],
                "role": "assistant",
                "status": "completed",
                "type": "message",
            },
            {"call_id": "2", "output": "tool_result", "type": "function_call_output"},
            {
                "id": "1",
                "content": [{"annotations": [], "text": "done", "type": "output_text"}],
                "role": "assistant",
                "status": "completed",
                "type": "message",
            },
            {"role": "user", "content": "transcription_2"},
            {
                "id": "1",
                "content": [{"annotations": [], "text": "done_2", "type": "output_text"}],
                "role": "assistant",
                "status": "completed",
                "type": "message",
            },
        ]
    )
    assert workflow._current_agent == agent
