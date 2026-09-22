from __future__ import annotations

from typing import Any, cast

import pytest

from agents import (
    Agent,
    AgentToolStreamEvent,
    GuardrailFunctionOutput,
    RunConfig,
    RunContextWrapper,
    UserError,
)
from agents.decorators import input_guardrail, tool
from agents.run_config import CallModelData, ModelInputData
from agents.testing import ScriptedModel
from agents.tool_context import ToolContext
from tests.test_responses import get_function_tool_call, get_text_message


def test_agent_as_tool_rejects_legacy_positional_run_config_before_execution() -> None:
    side_effects: list[str] = []

    @input_guardrail(run_in_parallel=False)
    def check_input(
        context: RunContextWrapper[Any], agent: Agent[Any], input: Any
    ) -> GuardrailFunctionOutput:
        side_effects.append("guardrail")
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    def filter_input(data: CallModelData[Any]) -> ModelInputData:
        side_effects.append("filter")
        return data.model_data

    @tool
    def perform_action() -> str:
        side_effects.append("tool")
        return "done"

    model = ScriptedModel([[get_function_tool_call("perform_action", "{}")]])
    agent = Agent(name="worker", model=model, tools=[perform_action])
    config = RunConfig(
        input_guardrails=[check_input],
        call_model_input_filter=filter_input,
        tracing_disabled=True,
        trace_include_sensitive_data=False,
    )

    with pytest.raises(UserError, match="on_stream must be callable or None.*run_config="):
        # Older callers passed RunConfig in the fifth positional slot.
        agent.as_tool("worker", "Run the worker", None, True, cast(Any, config))

    assert model.calls == ()
    assert side_effects == []


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_kind", ["none", "sync", "async", "instance"])
async def test_agent_as_tool_preserves_callbacks_and_keyword_run_config(handler_kind: str) -> None:
    events: list[AgentToolStreamEvent] = []
    filtered: list[str] = []
    checked: list[str] = []

    def sync_handler(event: AgentToolStreamEvent) -> None:
        events.append(event)

    async def async_handler(event: AgentToolStreamEvent) -> None:
        events.append(event)

    class Handler:
        def __call__(self, event: AgentToolStreamEvent) -> None:
            events.append(event)

    @input_guardrail(run_in_parallel=False)
    def check_input(
        context: RunContextWrapper[Any], agent: Agent[Any], input: Any
    ) -> GuardrailFunctionOutput:
        checked.append(agent.name)
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    def filter_input(data: CallModelData[Any]) -> ModelInputData:
        filtered.append(data.agent.name)
        return ModelInputData(input=[{"role": "user", "content": "filtered"}], instructions=None)

    handlers = {"none": None, "sync": sync_handler, "async": async_handler, "instance": Handler()}
    model = ScriptedModel([[get_text_message("done")]])
    agent = Agent(name="worker", model=model)
    nested_tool = agent.as_tool(
        "worker",
        "Run the worker",
        None,
        True,
        handlers[handler_kind],
        run_config=RunConfig(
            input_guardrails=[check_input],
            call_model_input_filter=filter_input,
            tracing_disabled=True,
            trace_include_sensitive_data=False,
        ),
    )

    context = ToolContext(
        context=None,
        tool_name="worker",
        tool_call_id="call_worker",
        tool_arguments='{"input":"original"}',
        run_config=RunConfig(trace_include_sensitive_data=True),
    )
    output = await nested_tool.on_invoke_tool(context, context.tool_arguments)

    assert output == "done"
    assert checked == ["worker"]
    assert filtered == ["worker"]
    assert len(model.calls) == 1
    assert model.calls[0].input == [{"role": "user", "content": "filtered"}]
    assert model.calls[0].tracing.is_disabled()
    assert bool(events) is (handler_kind != "none")
