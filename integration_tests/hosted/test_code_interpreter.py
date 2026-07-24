from __future__ import annotations

import pytest

from agents import Agent, CodeInterpreterTool, RunConfig, Runner
from agents.items import ToolCallItem

pytestmark = pytest.mark.hosted


async def test_code_interpreter_reasoning_items_survive_follow_up_replay(
    integration_model: str,
) -> None:
    agent = Agent(
        name="Packaged code interpreter agent",
        model=integration_model,
        instructions="Use code interpreter for calculations and answer with RESULT:<integer>.",
        tools=[
            CodeInterpreterTool(
                tool_config={"type": "code_interpreter", "container": {"type": "auto"}}
            )
        ],
        model_settings={"max_tokens": 1024},
    )
    first = await Runner.run(
        agent,
        "Use the code interpreter to calculate 273 * 312821 + 1782.",
        run_config=RunConfig(tracing_disabled=True),
    )
    expected = str(273 * 312821 + 1782)
    assert expected in str(first.final_output)
    assert any(
        isinstance(item, ToolCallItem)
        and getattr(item.raw_item, "type", None) == "code_interpreter_call"
        for item in first.new_items
    )

    follow_up = first.to_input_list()
    follow_up.append({"role": "user", "content": "Repeat the calculated result exactly."})
    second = await Runner.run(
        agent,
        follow_up,
        run_config=RunConfig(tracing_disabled=True),
    )

    assert expected in str(second.final_output)
