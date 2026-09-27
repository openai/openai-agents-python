from __future__ import annotations

from typing import Any

import pytest
from openai.types.responses import ResponseOutputMessage, ResponseOutputRefusal

from agents import (
    Agent,
    GuardrailFunctionOutput,
    ModelRefusalError,
    RunConfig,
    RunContextWrapper,
    Runner,
    RunState,
    ToolCallOutputItem,
)
from agents.decorators import output_guardrail, tool
from agents.testing import ScriptedModel
from tests.test_responses import get_function_tool_call, get_text_message


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    ("guardrail_owner", "fallback_source", "completion", "custom"),
    [
        ("agent", "tool", "empty", False),
        ("config", "message", "empty", False),
        ("handoff", "tool", "empty", False),
        ("agent", "tool", "none", False),
        ("agent", "tool", "nonempty", False),
        ("none", "tool", "empty", False),
        ("none", "message", "empty", False),
        ("agent", "tool", "refusal", False),
        ("agent", "tool", "empty", True),
    ],
)
async def test_default_agent_tool_exports_guarded_final_output(
    streamed: bool, guardrail_owner: str, fallback_source: str, completion: str, custom: bool
) -> None:
    checked: list[tuple[str, Any]] = []

    @output_guardrail
    def check_output(
        context: RunContextWrapper[Any], agent: Agent[Any], output: Any
    ) -> GuardrailFunctionOutput:
        checked.append((agent.name, output))
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=output == "internal")

    @tool
    def lookup() -> str:
        return "internal" if fallback_source == "tool" else ""

    first_turn = [get_function_tool_call("lookup", "{}")]
    if fallback_source == "message":
        first_turn.insert(0, get_text_message("internal"))
    final_message = get_text_message("done" if completion == "nonempty" else "")
    if completion == "none":
        final_message = get_text_message('{"response":null}')
    if completion == "refusal":
        final_message = ResponseOutputMessage(
            id="refusal",
            type="message",
            role="assistant",
            status="completed",
            content=[ResponseOutputRefusal(type="refusal", refusal="Cannot answer")],
        )
    final_agent = Agent(
        name="worker",
        model=ScriptedModel([first_turn, [final_message]]),
        tools=[lookup],
        output_type=type(None) if completion == "none" else None,
        output_guardrails=[check_output] if guardrail_owner in ("agent", "handoff") else [],
    )
    nested = final_agent
    if guardrail_owner == "handoff":
        nested = Agent(
            name="delegator",
            handoffs=[final_agent],
            model=ScriptedModel(
                [[get_function_tool_call("transfer_to_worker", "{}", call_id="handoff")]]
            ),
        )

    async def extract(result: Any) -> str:
        return "custom output"

    nested_tool = nested.as_tool(
        "delegate",
        "Delegate to a worker",
        on_stream=(lambda event: None) if streamed else None,
        run_config=RunConfig(output_guardrails=[check_output])
        if guardrail_owner == "config"
        else None,
        custom_output_extractor=extract if custom else None,
        failure_error_function=None,
    )
    parent_model = ScriptedModel(
        [
            [get_function_tool_call("delegate", '{"input":"work"}')],
            [get_text_message("parent done")],
        ]
    )
    parent = Agent(name="parent", model=parent_model, tools=[nested_tool])
    if completion == "refusal":
        with pytest.raises(ModelRefusalError):
            await Runner.run(parent, "go")
        assert checked == []
        assert len(parent_model.calls) == 1
        return
    result = await Runner.run(parent, "go")
    expected_final = None if completion == "none" else "done" if completion == "nonempty" else ""
    expected_export = (
        "custom output" if custom else "internal" if guardrail_owner == "none" else expected_final
    )
    assert checked == ([] if guardrail_owner == "none" else [("worker", expected_final)])
    outputs = [item.output for item in result.new_items if isinstance(item, ToolCallOutputItem)]
    assert outputs == [expected_export]
    parent_input = parent_model.calls[1].input
    assert isinstance(parent_input, list)
    forwarded = [item for item in parent_input if item.get("type") == "function_call_output"]
    assert [item["output"] for item in forwarded] == [str(expected_export)]


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_guarded_empty_agent_tool_output_after_approval_resume(streamed: bool) -> None:
    checked: list[Any] = []

    @output_guardrail
    def check_output(
        context: RunContextWrapper[Any], agent: Agent[Any], output: Any
    ) -> GuardrailFunctionOutput:
        checked.append(output)
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=output == "internal")

    @tool(needs_approval=True)
    def lookup() -> str:
        return "internal"

    nested = Agent(
        name="worker",
        tools=[lookup],
        output_guardrails=[check_output],
        model=ScriptedModel(
            [
                [get_function_tool_call("lookup", "{}")],
                [get_text_message("")],
            ]
        ),
    )
    nested_tool = nested.as_tool(
        "delegate",
        "Delegate",
        on_stream=(lambda event: None) if streamed else None,
        failure_error_function=None,
    )
    parent_model = ScriptedModel(
        [
            [get_function_tool_call("delegate", '{"input":"work"}')],
            [get_text_message("done")],
        ]
    )
    parent = Agent(name="parent", model=parent_model, tools=[nested_tool])
    paused = await Runner.run(parent, "go")
    assert len(paused.interruptions) == 1
    assert checked == []
    assert not [item for item in paused.new_items if isinstance(item, ToolCallOutputItem)]
    state = await RunState.from_json(parent, paused.to_state().to_json())
    state.approve(state.get_interruptions()[0])
    resumed = await Runner.run(parent, state)
    assert checked == [""]
    assert [item.output for item in resumed.new_items if isinstance(item, ToolCallOutputItem)] == [
        ""
    ]
    parent_input = parent_model.calls[1].input
    assert isinstance(parent_input, list)
    assert [
        item["output"] for item in parent_input if item.get("type") == "function_call_output"
    ] == [""]
