import json
from typing import cast

import pytest
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage

import agents.run as run_module
from agents import Agent, Runner, function_tool
from agents.agent import ToolsToFinalOutputResult
from agents.items import MessageOutputItem, ModelResponse, ToolCallItem, ToolCallOutputItem
from agents.lifecycle import RunHooks
from agents.run import RunConfig
from agents.run_context import RunContextWrapper
from agents.run_internal import run_loop, turn_resolution
from agents.run_internal.run_loop import (
    NextStepFinalOutput,
    NextStepInterruption,
    NextStepRunAgain,
    ProcessedResponse,
    SingleStepResult,
)
from agents.run_state import RunState
from agents.usage import Usage
from tests.fake_model import FakeModel
from tests.test_responses import get_function_tool_call, get_text_message
from tests.utils.hitl import (
    make_agent,
    make_context_wrapper,
    make_model_and_agent,
    queue_function_call_and_text,
)
from tests.utils.simple_session import SimpleListSession


@pytest.mark.asyncio
async def test_resolve_interrupted_turn_final_output_short_circuit(monkeypatch) -> None:
    agent: Agent[dict[str, str]] = make_agent(model=FakeModel())
    context_wrapper = make_context_wrapper()

    async def fake_execute_tool_plan(*_: object, **__: object):
        return [], [], [], [], [], [], []

    async def fake_check_for_final_output_from_tools(*_: object, **__: object):
        return ToolsToFinalOutputResult(is_final_output=True, final_output="done")

    async def fake_execute_final_output(
        *,
        original_input,
        new_response,
        pre_step_items,
        new_step_items,
        final_output,
        tool_input_guardrail_results,
        tool_output_guardrail_results,
        **__: object,
    ) -> SingleStepResult:
        return SingleStepResult(
            original_input=original_input,
            model_response=new_response,
            pre_step_items=pre_step_items,
            new_step_items=new_step_items,
            next_step=NextStepFinalOutput(final_output),
            tool_input_guardrail_results=tool_input_guardrail_results,
            tool_output_guardrail_results=tool_output_guardrail_results,
        )

    monkeypatch.setattr(
        turn_resolution, "check_for_final_output_from_tools", fake_check_for_final_output_from_tools
    )
    monkeypatch.setattr(turn_resolution, "execute_final_output", fake_execute_final_output)
    monkeypatch.setattr(turn_resolution, "_execute_tool_plan", fake_execute_tool_plan)

    processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )

    result = await run_loop.resolve_interrupted_turn(
        agent=agent,
        original_input="input",
        original_pre_step_items=[],
        new_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        processed_response=processed_response,
        hooks=RunHooks(),
        context_wrapper=context_wrapper,
        run_config=RunConfig(),
        run_state=None,
    )

    assert isinstance(result, SingleStepResult)
    assert isinstance(result.next_step, NextStepFinalOutput)
    assert result.next_step.output == "done"


@pytest.mark.asyncio
async def test_resumed_session_persistence_uses_saved_count(monkeypatch) -> None:
    agent = Agent(name="resume-agent")
    context_wrapper: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
    state = RunState(
        context=context_wrapper,
        original_input="input",
        starting_agent=agent,
        max_turns=1,
    )
    session = SimpleListSession()

    raw_output = {"type": "function_call_output", "call_id": "call-1", "output": "ok"}
    item_1 = ToolCallOutputItem(agent=agent, raw_item=raw_output, output="ok")
    item_2 = ToolCallOutputItem(agent=agent, raw_item=dict(raw_output), output="ok")
    step = SingleStepResult(
        original_input="input",
        model_response=ModelResponse(output=[], usage=Usage(), response_id="resp"),
        pre_step_items=[],
        new_step_items=[item_1, item_2],
        next_step=NextStepFinalOutput("done"),
        tool_input_guardrail_results=[],
        tool_output_guardrail_results=[],
    )

    async def fake_run_single_turn(**_kwargs):
        return step

    monkeypatch.setattr(run_module, "run_single_turn", fake_run_single_turn)

    runner = run_module.AgentRunner()
    await runner.run(agent, state, session=session, run_config=RunConfig())

    assert state._current_turn_persisted_item_count == 1
    assert len(session.saved_items) == 1


@pytest.mark.asyncio
async def test_resumed_run_again_resets_persisted_count(monkeypatch) -> None:
    agent = Agent(name="resume-agent")
    context_wrapper: RunContextWrapper[dict[str, str]] = RunContextWrapper(context={})
    state = RunState(
        context=context_wrapper,
        original_input="input",
        starting_agent=agent,
        max_turns=2,
    )
    session = SimpleListSession()

    state._current_step = NextStepInterruption(interruptions=[])
    state._model_responses = [
        ModelResponse(output=[], usage=Usage(), response_id="resp_1"),
    ]
    state._last_processed_response = ProcessedResponse(
        new_items=[],
        handoffs=[],
        functions=[],
        computer_actions=[],
        local_shell_calls=[],
        shell_calls=[],
        apply_patch_calls=[],
        tools_used=[],
        mcp_approval_requests=[],
        interruptions=[],
    )
    state._current_turn_persisted_item_count = 1

    async def fake_resolve_interrupted_turn(**_kwargs):
        return SingleStepResult(
            original_input="input",
            model_response=ModelResponse(output=[], usage=Usage(), response_id="resp_resume"),
            pre_step_items=[],
            new_step_items=[],
            next_step=NextStepRunAgain(),
            tool_input_guardrail_results=[],
            tool_output_guardrail_results=[],
        )

    async def fake_run_single_turn(**_kwargs):
        tool_call = cast(
            ResponseFunctionToolCall,
            get_function_tool_call("test_tool", "{}", call_id="call-1"),
        )
        tool_call_item = ToolCallItem(agent=agent, raw_item=tool_call)
        tool_output_item = ToolCallOutputItem(
            agent=agent,
            raw_item={
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "ok",
            },
            output="ok",
        )
        message_item = MessageOutputItem(
            agent=agent,
            raw_item=cast(ResponseOutputMessage, get_text_message("final")),
        )
        return SingleStepResult(
            original_input="input",
            model_response=ModelResponse(
                output=[get_text_message("final")],
                usage=Usage(),
                response_id="resp_final",
            ),
            pre_step_items=[],
            new_step_items=[tool_call_item, tool_output_item, message_item],
            next_step=NextStepFinalOutput("done"),
            tool_input_guardrail_results=[],
            tool_output_guardrail_results=[],
        )

    monkeypatch.setattr(run_module, "resolve_interrupted_turn", fake_resolve_interrupted_turn)
    monkeypatch.setattr(run_module, "run_single_turn", fake_run_single_turn)

    runner = run_module.AgentRunner()
    result = await runner.run(agent, state, session=session, run_config=RunConfig())

    assert result.final_output == "done"
    saved_types = [
        item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
        for item in session.saved_items
    ]
    assert "function_call" in saved_types


@pytest.mark.asyncio
async def test_resumed_approval_does_not_duplicate_session_items() -> None:
    async def test_tool() -> str:
        return "tool_result"

    tool = function_tool(test_tool, name_override="test_tool", needs_approval=True)
    model, agent = make_model_and_agent(name="test", tools=[tool])
    session = SimpleListSession()

    queue_function_call_and_text(
        model,
        get_function_tool_call("test_tool", json.dumps({}), call_id="call-resume"),
        followup=[get_text_message("done")],
    )

    first = await Runner.run(agent, input="Use test_tool", session=session)
    assert first.interruptions
    state = first.to_state()
    state.approve(first.interruptions[0])

    resumed = await Runner.run(agent, state, session=session)
    assert resumed.final_output == "done"

    saved_items = await session.get_items()
    call_count = sum(
        1
        for item in saved_items
        if isinstance(item, dict)
        and item.get("type") == "function_call"
        and item.get("call_id") == "call-resume"
    )
    output_count = sum(
        1
        for item in saved_items
        if isinstance(item, dict)
        and item.get("type") == "function_call_output"
        and item.get("call_id") == "call-resume"
    )

    assert call_count == 1
    assert output_count == 1
