from types import SimpleNamespace
from typing import Any, cast

import pytest
from openai.types.responses import ResponseFunctionToolCall

import agents.run as run_module
from agents import Agent, Runner
from agents.guardrail import GuardrailFunctionOutput, InputGuardrail, InputGuardrailResult
from agents.items import ModelResponse, ToolApprovalItem
from agents.run_context import RunContextWrapper
from agents.run_internal.run_steps import (
    NextStepFinalOutput,
    NextStepInterruption,
    SingleStepResult,
)
from agents.run_state import RunState
from agents.tool_guardrails import (
    AllowBehavior,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrail,
    ToolInputGuardrailResult,
    ToolOutputGuardrail,
    ToolOutputGuardrailResult,
)
from agents.usage import Usage
from tests.fake_model import FakeModel


@pytest.mark.asyncio
async def test_runner_resume_preserves_guardrail_results(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = Agent(name="agent", model=FakeModel())
    context_wrapper: RunContextWrapper[dict[str, Any]] = RunContextWrapper(context={})

    input_guardrail: InputGuardrail[Any] = InputGuardrail(
        guardrail_function=lambda ctx, ag, inp: GuardrailFunctionOutput(
            output_info={"source": "state"},
            tripwire_triggered=False,
        ),
        name="state_input_guardrail",
    )
    initial_input_result = InputGuardrailResult(
        guardrail=input_guardrail,
        output=GuardrailFunctionOutput(
            output_info={"source": "state"},
            tripwire_triggered=False,
        ),
    )

    tool_input_guardrail: ToolInputGuardrail[Any] = ToolInputGuardrail(
        guardrail_function=lambda data: ToolGuardrailFunctionOutput(
            output_info={"source": "state"},
            behavior=AllowBehavior(type="allow"),
        ),
        name="state_tool_input_guardrail",
    )
    tool_output_guardrail: ToolOutputGuardrail[Any] = ToolOutputGuardrail(
        guardrail_function=lambda data: ToolGuardrailFunctionOutput(
            output_info={"source": "state"},
            behavior=AllowBehavior(type="allow"),
        ),
        name="state_tool_output_guardrail",
    )
    initial_tool_input_result = ToolInputGuardrailResult(
        guardrail=tool_input_guardrail,
        output=ToolGuardrailFunctionOutput(
            output_info={"source": "state"},
            behavior=AllowBehavior(type="allow"),
        ),
    )
    initial_tool_output_result = ToolOutputGuardrailResult(
        guardrail=tool_output_guardrail,
        output=ToolGuardrailFunctionOutput(
            output_info={"source": "state"},
            behavior=AllowBehavior(type="allow"),
        ),
    )

    run_state = RunState(
        context=context_wrapper,
        original_input="hello",
        starting_agent=agent,
        max_turns=3,
    )
    run_state._input_guardrail_results = [initial_input_result]
    run_state._tool_input_guardrail_results = [initial_tool_input_result]
    run_state._tool_output_guardrail_results = [initial_tool_output_result]

    model_response = ModelResponse(output=[], usage=Usage(), response_id="resp-final")

    new_tool_input_result = ToolInputGuardrailResult(
        guardrail=ToolInputGuardrail(
            guardrail_function=lambda data: ToolGuardrailFunctionOutput(
                output_info={"source": "new"},
                behavior=AllowBehavior(type="allow"),
            ),
            name="new_tool_input_guardrail",
        ),
        output=ToolGuardrailFunctionOutput(
            output_info={"source": "new"},
            behavior=AllowBehavior(type="allow"),
        ),
    )
    new_tool_output_result = ToolOutputGuardrailResult(
        guardrail=ToolOutputGuardrail(
            guardrail_function=lambda data: ToolGuardrailFunctionOutput(
                output_info={"source": "new"},
                behavior=AllowBehavior(type="allow"),
            ),
            name="new_tool_output_guardrail",
        ),
        output=ToolGuardrailFunctionOutput(
            output_info={"source": "new"},
            behavior=AllowBehavior(type="allow"),
        ),
    )

    async def fake_run_single_turn(**_: object) -> SingleStepResult:
        return SingleStepResult(
            original_input="hello",
            model_response=model_response,
            pre_step_items=[],
            new_step_items=[],
            next_step=NextStepFinalOutput(output="done"),
            tool_input_guardrail_results=[new_tool_input_result],
            tool_output_guardrail_results=[new_tool_output_result],
        )

    async def fake_run_output_guardrails(*_: object, **__: object) -> list[object]:
        return []

    async def fake_get_all_tools(*_: object, **__: object) -> list[object]:
        return []

    async def fake_initialize_computer_tools(
        *args: object, tools: list[object], **kwargs: object
    ) -> list[object]:
        return tools

    monkeypatch.setattr(run_module, "run_single_turn", fake_run_single_turn)
    monkeypatch.setattr(run_module, "run_output_guardrails", fake_run_output_guardrails)
    monkeypatch.setattr(run_module, "get_all_tools", fake_get_all_tools)
    monkeypatch.setattr(run_module, "initialize_computer_tools", fake_initialize_computer_tools)

    result = await Runner.run(agent, run_state)

    assert result.final_output == "done"
    assert [res.guardrail.get_name() for res in result.input_guardrail_results] == [
        "state_input_guardrail"
    ]
    assert [res.guardrail.get_name() for res in result.tool_input_guardrail_results] == [
        "state_tool_input_guardrail",
        "new_tool_input_guardrail",
    ]
    assert [res.guardrail.get_name() for res in result.tool_output_guardrail_results] == [
        "state_tool_output_guardrail",
        "new_tool_output_guardrail",
    ]


@pytest.mark.asyncio
async def test_runner_resume_preserves_guardrail_results_on_reinterruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed run that interrupts again must keep the tool guardrail results it carried in."""
    agent = Agent(name="agent", model=FakeModel())
    context_wrapper: RunContextWrapper[dict[str, Any]] = RunContextWrapper(context={})

    tool_input_guardrail: ToolInputGuardrail[Any] = ToolInputGuardrail(
        guardrail_function=lambda data: ToolGuardrailFunctionOutput(
            output_info={"source": "state"},
            behavior=AllowBehavior(type="allow"),
        ),
        name="state_tool_input_guardrail",
    )
    tool_output_guardrail: ToolOutputGuardrail[Any] = ToolOutputGuardrail(
        guardrail_function=lambda data: ToolGuardrailFunctionOutput(
            output_info={"source": "state"},
            behavior=AllowBehavior(type="allow"),
        ),
        name="state_tool_output_guardrail",
    )
    initial_tool_input_result = ToolInputGuardrailResult(
        guardrail=tool_input_guardrail,
        output=ToolGuardrailFunctionOutput(
            output_info={"source": "state"},
            behavior=AllowBehavior(type="allow"),
        ),
    )
    initial_tool_output_result = ToolOutputGuardrailResult(
        guardrail=tool_output_guardrail,
        output=ToolGuardrailFunctionOutput(
            output_info={"source": "state"},
            behavior=AllowBehavior(type="allow"),
        ),
    )

    model_response = ModelResponse(output=[], usage=Usage(), response_id="resp-interrupted")
    processed_response = cast(Any, SimpleNamespace(tools_used=[], new_items=[]))

    run_state = RunState(
        context=context_wrapper,
        original_input="hello",
        starting_agent=agent,
        max_turns=3,
    )
    run_state._tool_input_guardrail_results = [initial_tool_input_result]
    run_state._tool_output_guardrail_results = [initial_tool_output_result]
    run_state._model_responses = [model_response]
    run_state._last_processed_response = processed_response

    pending_approval = ToolApprovalItem(
        agent=agent,
        raw_item=ResponseFunctionToolCall(
            id="call-pending",
            call_id="call-pending",
            name="pending_tool",
            arguments="{}",
            type="function_call",
        ),
    )
    run_state._current_step = NextStepInterruption(interruptions=[pending_approval])

    new_tool_input_result = ToolInputGuardrailResult(
        guardrail=ToolInputGuardrail(
            guardrail_function=lambda data: ToolGuardrailFunctionOutput(
                output_info={"source": "new"},
                behavior=AllowBehavior(type="allow"),
            ),
            name="new_tool_input_guardrail",
        ),
        output=ToolGuardrailFunctionOutput(
            output_info={"source": "new"},
            behavior=AllowBehavior(type="allow"),
        ),
    )
    new_tool_output_result = ToolOutputGuardrailResult(
        guardrail=ToolOutputGuardrail(
            guardrail_function=lambda data: ToolGuardrailFunctionOutput(
                output_info={"source": "new"},
                behavior=AllowBehavior(type="allow"),
            ),
            name="new_tool_output_guardrail",
        ),
        output=ToolGuardrailFunctionOutput(
            output_info={"source": "new"},
            behavior=AllowBehavior(type="allow"),
        ),
    )

    async def fake_resolve_interrupted_turn(**_: object) -> SingleStepResult:
        return SingleStepResult(
            original_input="hello",
            model_response=model_response,
            pre_step_items=[],
            new_step_items=[],
            next_step=NextStepInterruption(interruptions=[pending_approval]),
            tool_input_guardrail_results=[new_tool_input_result],
            tool_output_guardrail_results=[new_tool_output_result],
        )

    async def fake_get_all_tools(*_: object, **__: object) -> list[object]:
        return []

    async def fake_initialize_computer_tools(
        *args: object, tools: list[object], **kwargs: object
    ) -> list[object]:
        return tools

    monkeypatch.setattr(run_module, "resolve_interrupted_turn", fake_resolve_interrupted_turn)
    monkeypatch.setattr(run_module, "get_all_tools", fake_get_all_tools)
    monkeypatch.setattr(run_module, "initialize_computer_tools", fake_initialize_computer_tools)

    result = await Runner.run(agent, run_state)

    assert result.interruptions
    assert [res.guardrail.get_name() for res in result.tool_input_guardrail_results] == [
        "state_tool_input_guardrail",
        "new_tool_input_guardrail",
    ]
    assert [res.guardrail.get_name() for res in result.tool_output_guardrail_results] == [
        "state_tool_output_guardrail",
        "new_tool_output_guardrail",
    ]
