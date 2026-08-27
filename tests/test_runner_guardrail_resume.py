from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest
from openai.types.responses import ResponseFunctionToolCall

import agents.run as run_module
from agents import (
    Agent,
    Runner,
    ToolExecutionConfig,
    ToolInputGuardrailData,
    ToolOutputGuardrailData,
    function_tool,
)
from agents.guardrail import GuardrailFunctionOutput, InputGuardrail, InputGuardrailResult
from agents.items import ModelResponse, ToolApprovalItem, TResponseInputItem
from agents.lifecycle import RunHooks
from agents.memory import Session
from agents.run import RunConfig
from agents.run_context import RunContextWrapper
from agents.run_internal.run_steps import (
    NextStepFinalOutput,
    NextStepInterruption,
    SingleStepResult,
)
from agents.run_state import RunState
from agents.testing import ScriptedModel
from agents.tool import Tool
from agents.tool_guardrails import (
    AllowBehavior,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrail,
    ToolInputGuardrailResult,
    ToolOutputGuardrail,
    ToolOutputGuardrailResult,
    tool_input_guardrail,
    tool_output_guardrail,
)
from agents.usage import Usage
from tests.test_responses import get_function_tool_call, get_text_message
from tests.utils.simple_session import SimpleListSession


class _ResumeWriteFailureSession(SimpleListSession):
    """Fail one resumed append either before or after the batch reaches the Session."""

    def __init__(self) -> None:
        super().__init__()
        self.failure: Literal["before", "after"] | None = None
        self.error = RuntimeError("session append failed")

    async def add_items(self, items: list[TResponseInputItem]) -> None:
        failure, self.failure = self.failure, None
        if failure == "before":
            raise self.error
        await super().add_items(items)
        if failure == "after":
            raise self.error


class _CountingToolHooks(RunHooks[Any]):
    def __init__(self) -> None:
        self.tool_starts = 0
        self.tool_ends = 0

    async def on_tool_start(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        tool: Tool,
    ) -> None:
        self.tool_starts += 1

    async def on_tool_end(
        self,
        context: RunContextWrapper[Any],
        agent: Agent[Any],
        tool: Tool,
        result: object,
    ) -> None:
        self.tool_ends += 1


async def _run_with_session(
    agent: Agent[Any],
    value: str | RunState[Any],
    session: Session,
    hooks: RunHooks[Any],
    *,
    streamed: bool,
    pre_approval: bool,
) -> Any:
    run_config = RunConfig(
        tracing_disabled=True,
        tool_execution=(
            ToolExecutionConfig(pre_approval_tool_input_guardrails=True) if pre_approval else None
        ),
    )
    if not streamed:
        return await Runner.run(
            agent,
            value,
            session=session,
            hooks=hooks,
            run_config=run_config,
        )
    result = Runner.run_streamed(
        agent,
        value,
        session=session,
        hooks=hooks,
        run_config=run_config,
    )
    async for _ in result.stream_events():
        pass
    return result


def _assert_guardrail_results(value: Any, *, input_count: int) -> None:
    input_results = (
        value._tool_input_guardrail_results
        if isinstance(value, RunState)
        else value.tool_input_guardrail_results
    )
    output_results = (
        value._tool_output_guardrail_results
        if isinstance(value, RunState)
        else value.tool_output_guardrail_results
    )
    assert [result.output.output_info for result in input_results] == [
        "input-checked"
    ] * input_count
    assert [result.output.output_info for result in output_results] == ["output-checked"]


def _tool_item_types(items: list[TResponseInputItem], call_id: str) -> list[str]:
    return [
        str(item.get("type"))
        for item in items
        if isinstance(item, dict) and item.get("call_id") == call_id
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "failing_streamed",
        "recovering_streamed",
        "round_trip",
        "failure",
        "pre_approval",
    ),
    [
        (False, False, False, "before", False),
        (False, True, True, "after", True),
        (True, False, False, "after", False),
        (True, True, True, "before", False),
    ],
    ids=[
        "run-to-run-live-before-commit",
        "run-to-streamed-json-pre-approval-commit-then-raise",
        "streamed-to-run-live-commit-then-raise",
        "streamed-to-streamed-json-before-commit",
    ],
)
async def test_resumed_session_failure_publishes_durable_tool_guardrails(
    failing_streamed: bool,
    recovering_streamed: bool,
    round_trip: bool,
    failure: Literal["before", "after"],
    pre_approval: bool,
) -> None:
    counters = {
        "effect": 0,
        "input_guardrail": 0,
        "output_guardrail": 0,
    }

    @tool_input_guardrail
    async def record_input(
        _data: ToolInputGuardrailData,
    ) -> ToolGuardrailFunctionOutput:
        counters["input_guardrail"] += 1
        return ToolGuardrailFunctionOutput.allow(output_info="input-checked")

    @tool_output_guardrail
    async def record_output(
        _data: ToolOutputGuardrailData,
    ) -> ToolGuardrailFunctionOutput:
        counters["output_guardrail"] += 1
        return ToolGuardrailFunctionOutput.allow(output_info="output-checked")

    @function_tool(
        needs_approval=True,
        tool_input_guardrails=[record_input],
        tool_output_guardrails=[record_output],
    )
    async def charge(amount: int) -> str:
        counters["effect"] += 1
        return f"receipt-{amount}"

    @function_tool(needs_approval=True)
    async def notify() -> str:
        raise AssertionError("the unresolved approval must not execute")

    model = ScriptedModel(
        [
            [
                get_function_tool_call("charge", '{"amount":7}', call_id="charge-1"),
                get_function_tool_call("notify", "{}", call_id="notify-1"),
            ],
            [get_text_message("done")],
        ]
    )
    agent = Agent(name="payment", model=model, tools=[charge, notify])
    session = _ResumeWriteFailureSession()
    hooks = _CountingToolHooks()
    input_guardrail_count = 2 if pre_approval else 1
    expected_counters = {
        "effect": 1,
        "input_guardrail": input_guardrail_count,
        "output_guardrail": 1,
    }

    paused = await _run_with_session(
        agent,
        "charge 7 and notify",
        session,
        hooks,
        streamed=failing_streamed,
        pre_approval=pre_approval,
    )
    state = paused.to_state()
    charge_approval = next(
        item for item in state.get_interruptions() if item.raw_item.call_id == "charge-1"
    )
    state.approve(charge_approval)

    session.failure = failure
    with pytest.raises(RuntimeError) as error:
        await _run_with_session(
            agent,
            state,
            session,
            hooks,
            streamed=failing_streamed,
            pre_approval=pre_approval,
        )
    assert error.value is session.error
    _assert_guardrail_results(state, input_count=input_guardrail_count)
    assert counters == expected_counters
    assert hooks.tool_starts == 1
    assert hooks.tool_ends == 1
    assert len(model.calls) == 1
    assert [item.raw_item.call_id for item in state.get_interruptions()] == ["notify-1"]
    assert _tool_item_types(await session.get_items(), "charge-1") == (
        ["function_call"] if failure == "before" else ["function_call", "function_call_output"]
    )

    if round_trip:
        state = await RunState.from_json(agent, state.to_json())
        _assert_guardrail_results(state, input_count=input_guardrail_count)

    pending = await _run_with_session(
        agent,
        state,
        session,
        hooks,
        streamed=recovering_streamed,
        pre_approval=pre_approval,
    )
    _assert_guardrail_results(pending, input_count=input_guardrail_count)
    pending_state = pending.to_state()
    _assert_guardrail_results(pending_state, input_count=input_guardrail_count)
    remaining = pending_state.get_interruptions()
    assert [item.raw_item.call_id for item in remaining] == ["notify-1"]
    assert _tool_item_types(await session.get_items(), "charge-1") == [
        "function_call",
        "function_call_output",
    ]
    assert counters == expected_counters
    assert hooks.tool_starts == 1
    assert hooks.tool_ends == 1
    assert len(model.calls) == 1

    pending_state.reject(remaining[0], rejection_message="declined")
    result = await _run_with_session(
        agent,
        pending_state,
        session,
        hooks,
        streamed=recovering_streamed,
        pre_approval=pre_approval,
    )
    assert result.final_output == "done"
    _assert_guardrail_results(result, input_count=input_guardrail_count)
    _assert_guardrail_results(result.to_state(), input_count=input_guardrail_count)
    session_items = await session.get_items()
    result_items = result.to_input_list()
    final_model_items = model.calls[-1].input
    for items in (session_items, result_items, final_model_items):
        assert _tool_item_types(items, "charge-1") == [
            "function_call",
            "function_call_output",
        ]
        assert _tool_item_types(items, "notify-1") == [
            "function_call",
            "function_call_output",
        ]
    assert counters == expected_counters
    assert hooks.tool_starts == 1
    assert hooks.tool_ends == 1
    assert len(model.calls) == 2


@pytest.mark.asyncio
async def test_runner_resume_preserves_guardrail_results(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = Agent(name="agent", model=ScriptedModel())
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
    agent = Agent(name="agent", model=ScriptedModel())
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
