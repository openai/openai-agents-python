from __future__ import annotations

import pytest
from openai.types.responses import ResponseReasoningItem

from agents import Agent, CodeInterpreterTool, RunConfig, Runner
from agents.items import ToolCallItem

pytestmark = pytest.mark.hosted


async def test_code_interpreter_reasoning_items_survive_follow_up_replay(
    integration_model: str,
) -> None:
    agent = Agent(
        name="Packaged code interpreter agent",
        model=integration_model,
        instructions=(
            "Before using any tools, reason through conditional arithmetic to determine which "
            "calculation is required. Then use code interpreter for the calculation and "
            "answer with RESULT:<integer>."
        ),
        tools=[
            CodeInterpreterTool(
                tool_config={"type": "code_interpreter", "container": {"type": "auto"}}
            )
        ],
        model_settings={
            "max_tokens": 1024,
            "reasoning": {"effort": "medium", "summary": "auto"},
            "response_include": ["reasoning.encrypted_content"],
            "store": False,
        },
    )
    first = await Runner.run(
        agent,
        "First determine whether the remainder of 4837 multiplied by 8291 divided by 97 "
        "is odd. If it is odd, use the code interpreter to calculate 273 * 312821 + 1782; "
        "otherwise calculate 19 * 83. Respond only with RESULT:<answer>.",
        run_config=RunConfig(tracing_disabled=True, reasoning_item_id_policy="omit"),
    )
    expected = str(273 * 312821 + 1782)
    assert expected in str(first.final_output)
    assert any(
        isinstance(item, ToolCallItem)
        and getattr(item.raw_item, "type", None) == "code_interpreter_call"
        for item in first.new_items
    )

    reasoning_items = [
        output
        for response in first.raw_responses
        for output in response.output
        if isinstance(output, ResponseReasoningItem)
    ]
    assert reasoning_items, [
        getattr(output, "type", type(output).__name__)
        for response in first.raw_responses
        for output in response.output
    ]

    follow_up = first.to_input_list(mode="normalized")
    replayed_reasoning = [item for item in follow_up if item.get("type") == "reasoning"]
    assert len(replayed_reasoning) == len(reasoning_items)
    assert all(isinstance(item.get("encrypted_content"), str) for item in replayed_reasoning)
    follow_up.append({"role": "user", "content": "Repeat the calculated result exactly."})
    second = await Runner.run(
        agent,
        follow_up,
        run_config=RunConfig(tracing_disabled=True, reasoning_item_id_policy="omit"),
    )

    assert expected in str(second.final_output)
