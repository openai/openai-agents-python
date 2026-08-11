from __future__ import annotations

import json

import pytest
from inline_snapshot import snapshot

from agents import Agent
from agents.testing import ScriptedModel

from ..test_responses import get_function_tool, get_function_tool_call, get_text_message

try:
    from agents.voice import SingleAgentVoiceWorkflow

except ImportError:
    pass


@pytest.mark.asyncio
async def test_single_agent_workflow(monkeypatch) -> None:
    model = ScriptedModel()
    model.extend(
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
                "content": [
                    {"annotations": [], "logprobs": [], "text": "a_message", "type": "output_text"}
                ],
                "role": "assistant",
                "status": "completed",
                "type": "message",
            },
            {
                "call_id": "2",
                "output": "tool_result",
                "type": "function_call_output",
            },
            {
                "id": "1",
                "content": [
                    {"annotations": [], "logprobs": [], "text": "done", "type": "output_text"}
                ],
                "role": "assistant",
                "status": "completed",
                "type": "message",
            },
        ]
    )
    assert workflow._current_agent == agent

    model.enqueue([get_text_message("done_2")])

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
                "content": [
                    {"annotations": [], "logprobs": [], "text": "a_message", "type": "output_text"}
                ],
                "role": "assistant",
                "status": "completed",
                "type": "message",
            },
            {
                "call_id": "2",
                "output": "tool_result",
                "type": "function_call_output",
            },
            {
                "id": "1",
                "content": [
                    {"annotations": [], "logprobs": [], "text": "done", "type": "output_text"}
                ],
                "role": "assistant",
                "status": "completed",
                "type": "message",
            },
            {"role": "user", "content": "transcription_2"},
            {
                "id": "1",
                "content": [
                    {"annotations": [], "logprobs": [], "text": "done_2", "type": "output_text"}
                ],
                "role": "assistant",
                "status": "completed",
                "type": "message",
            },
        ]
    )
    assert workflow._current_agent == agent
