from __future__ import annotations

from typing import Any

import pytest

from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    OutputGuardrailTripwireTriggered,
    RunConfig,
    RunContextWrapper,
    Runner,
    ToolExecutionConfig,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
    ToolOutputGuardrailData,
)
from agents.decorators import (
    input_guardrail,
    output_guardrail,
    tool,
    tool_input_guardrail,
    tool_output_guardrail,
)

pytestmark = pytest.mark.core


@pytest.mark.parametrize("blocked", [False, True], ids=["accepted", "blocked"])
async def test_output_guardrails_validate_real_model_results(
    integration_model: str, blocked: bool
) -> None:
    inspected: list[str] = []

    @output_guardrail
    async def inspect_result(
        context: RunContextWrapper[Any], agent: Agent[Any], output: str
    ) -> GuardrailFunctionOutput:
        del context, agent
        inspected.append(output)
        return GuardrailFunctionOutput(
            output_info={"checked": True},
            tripwire_triggered=blocked,
        )

    agent = Agent(
        name="Packaged output guardrail agent",
        model=integration_model,
        instructions="Reply with exactly GUARDED_RESULT.",
        output_guardrails=[inspect_result],
        model_settings={"max_tokens": 256},
    )
    if blocked:
        with pytest.raises(OutputGuardrailTripwireTriggered):
            await Runner.run(
                agent,
                "Return the deterministic guarded result.",
                run_config=RunConfig(tracing_disabled=True),
            )
    else:
        result = await Runner.run(
            agent,
            "Return the deterministic guarded result.",
            run_config=RunConfig(tracing_disabled=True),
        )
        assert result.final_output == "GUARDED_RESULT"

    assert inspected == ["GUARDED_RESULT"]


@pytest.mark.parametrize("blocked", [False, True], ids=["accepted", "blocked"])
async def test_input_guardrails_validate_live_run_requests(
    integration_model: str, blocked: bool
) -> None:
    inspected: list[str] = []

    @input_guardrail
    async def inspect_input(
        context: RunContextWrapper[Any], agent: Agent[Any], input: str | list[Any]
    ) -> GuardrailFunctionOutput:
        del context, agent
        inspected.append(str(input))
        return GuardrailFunctionOutput(output_info={"checked": True}, tripwire_triggered=blocked)

    agent = Agent(
        name="Packaged input guardrail agent",
        model=integration_model,
        instructions="Reply with exactly INPUT_GUARDRAIL_READY.",
        input_guardrails=[inspect_input],
        model_settings={"max_tokens": 256},
    )
    if blocked:
        with pytest.raises(InputGuardrailTripwireTriggered):
            await Runner.run(
                agent,
                "Check the input guardrail.",
                run_config=RunConfig(tracing_disabled=True),
            )
    else:
        result = await Runner.run(
            agent,
            "Check the input guardrail.",
            run_config=RunConfig(tracing_disabled=True),
        )
        assert result.final_output == "INPUT_GUARDRAIL_READY"

    assert inspected == ["Check the input guardrail."]


async def test_tool_input_and_output_guardrails_preserve_live_execution_order(
    integration_model: str,
) -> None:
    observed: list[str] = []

    @tool_input_guardrail
    def inspect_input(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        observed.append(f"input:{data.context.tool_name}")
        return ToolGuardrailFunctionOutput.allow()

    @tool_output_guardrail
    def inspect_output(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        observed.append(f"output:{data.output}")
        return ToolGuardrailFunctionOutput.allow()

    @tool(
        tool_input_guardrails=[inspect_input],
        tool_output_guardrails=[inspect_output],
    )
    def guarded_lookup(value: int) -> str:
        """Look up a deterministic guarded value."""
        observed.append(f"tool:{value}")
        return "guarded-ready"

    agent = Agent(
        name="Packaged tool guardrail agent",
        model=integration_model,
        instructions="Call guarded_lookup with value 42 and then reply TOOL_GUARDRAILS_READY.",
        tools=[guarded_lookup],
        model_settings={"max_tokens": 384},
    )

    result = await Runner.run(
        agent,
        "Use the guarded lookup.",
        run_config=RunConfig(
            tracing_disabled=True,
            tool_execution=ToolExecutionConfig(pre_approval_tool_input_guardrails=True),
        ),
    )

    assert result.final_output == "TOOL_GUARDRAILS_READY"
    assert observed == ["input:guarded_lookup", "tool:42", "output:guarded-ready"]


@pytest.mark.parametrize("blocked", [False, True], ids=["accepted", "blocked"])
async def test_streaming_output_guardrails_validate_live_model_results(
    integration_model: str, blocked: bool
) -> None:
    inspected: list[str] = []

    @output_guardrail
    async def inspect_result(
        context: RunContextWrapper[Any], agent: Agent[Any], output: str
    ) -> GuardrailFunctionOutput:
        del context, agent
        inspected.append(output)
        return GuardrailFunctionOutput(output_info={"checked": True}, tripwire_triggered=blocked)

    agent = Agent(
        name="Packaged streamed output guardrail agent",
        model=integration_model,
        instructions="Reply with exactly STREAM_GUARDED_RESULT.",
        output_guardrails=[inspect_result],
        model_settings={"max_tokens": 256},
    )
    result = Runner.run_streamed(
        agent,
        "Return the deterministic streamed guarded result.",
        run_config=RunConfig(tracing_disabled=True),
    )
    if blocked:
        with pytest.raises(OutputGuardrailTripwireTriggered):
            async for _event in result.stream_events():
                pass
    else:
        async for _event in result.stream_events():
            pass
        assert result.final_output == "STREAM_GUARDED_RESULT"

    assert inspected == ["STREAM_GUARDED_RESULT"]
