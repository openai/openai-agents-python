from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import patch

import pytest

from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrail,
    InputGuardrailTripwireTriggered,
    OutputGuardrail,
    RunConfig,
    RunContextWrapper,
    Runner,
    TResponseInputItem,
    UserError,
    function_tool,
)
from agents.guardrail import input_guardrail, output_guardrail

from .fake_model import FakeModel
from .test_responses import get_function_tool_call, get_text_message


def get_sync_guardrail(triggers: bool, output_info: Any | None = None):
    def sync_guardrail(
        context: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ):
        return GuardrailFunctionOutput(
            output_info=output_info,
            tripwire_triggered=triggers,
        )

    return sync_guardrail


@pytest.mark.asyncio
async def test_sync_input_guardrail():
    guardrail = InputGuardrail(guardrail_function=get_sync_guardrail(triggers=False))
    result = await guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = InputGuardrail(guardrail_function=get_sync_guardrail(triggers=True))
    result = await guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = InputGuardrail(
        guardrail_function=get_sync_guardrail(triggers=True, output_info="test")
    )
    result = await guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info == "test"


def get_async_input_guardrail(triggers: bool, output_info: Any | None = None):
    async def async_guardrail(
        context: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ):
        return GuardrailFunctionOutput(
            output_info=output_info,
            tripwire_triggered=triggers,
        )

    return async_guardrail


@pytest.mark.asyncio
async def test_async_input_guardrail():
    guardrail = InputGuardrail(guardrail_function=get_async_input_guardrail(triggers=False))
    result = await guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = InputGuardrail(guardrail_function=get_async_input_guardrail(triggers=True))
    result = await guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = InputGuardrail(
        guardrail_function=get_async_input_guardrail(triggers=True, output_info="test")
    )
    result = await guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info == "test"


@pytest.mark.asyncio
async def test_invalid_input_guardrail_raises_user_error():
    with pytest.raises(UserError):
        # Purposely ignoring type error
        guardrail = InputGuardrail(guardrail_function="foo")  # type: ignore
        await guardrail.run(
            agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
        )


def get_sync_output_guardrail(triggers: bool, output_info: Any | None = None):
    def sync_guardrail(context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any):
        return GuardrailFunctionOutput(
            output_info=output_info,
            tripwire_triggered=triggers,
        )

    return sync_guardrail


@pytest.mark.asyncio
async def test_sync_output_guardrail():
    guardrail = OutputGuardrail(guardrail_function=get_sync_output_guardrail(triggers=False))
    result = await guardrail.run(
        agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = OutputGuardrail(guardrail_function=get_sync_output_guardrail(triggers=True))
    result = await guardrail.run(
        agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = OutputGuardrail(
        guardrail_function=get_sync_output_guardrail(triggers=True, output_info="test")
    )
    result = await guardrail.run(
        agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info == "test"


def get_async_output_guardrail(triggers: bool, output_info: Any | None = None):
    async def async_guardrail(
        context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any
    ):
        return GuardrailFunctionOutput(
            output_info=output_info,
            tripwire_triggered=triggers,
        )

    return async_guardrail


@pytest.mark.asyncio
async def test_async_output_guardrail():
    guardrail = OutputGuardrail(guardrail_function=get_async_output_guardrail(triggers=False))
    result = await guardrail.run(
        agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = OutputGuardrail(guardrail_function=get_async_output_guardrail(triggers=True))
    result = await guardrail.run(
        agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info is None

    guardrail = OutputGuardrail(
        guardrail_function=get_async_output_guardrail(triggers=True, output_info="test")
    )
    result = await guardrail.run(
        agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
    )
    assert result.output.tripwire_triggered
    assert result.output.output_info == "test"


@pytest.mark.asyncio
async def test_invalid_output_guardrail_raises_user_error():
    with pytest.raises(UserError):
        # Purposely ignoring type error
        guardrail = OutputGuardrail(guardrail_function="foo")  # type: ignore
        await guardrail.run(
            agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
        )


@input_guardrail
def decorated_input_guardrail(
    context: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
) -> GuardrailFunctionOutput:
    return GuardrailFunctionOutput(
        output_info="test_1",
        tripwire_triggered=False,
    )


@input_guardrail(name="Custom name")
def decorated_named_input_guardrail(
    context: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
) -> GuardrailFunctionOutput:
    return GuardrailFunctionOutput(
        output_info="test_2",
        tripwire_triggered=False,
    )


@pytest.mark.asyncio
async def test_input_guardrail_decorators():
    guardrail = decorated_input_guardrail
    result = await guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info == "test_1"

    guardrail = decorated_named_input_guardrail
    result = await guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info == "test_2"
    assert guardrail.get_name() == "Custom name"


@output_guardrail
def decorated_output_guardrail(
    context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any
) -> GuardrailFunctionOutput:
    return GuardrailFunctionOutput(
        output_info="test_3",
        tripwire_triggered=False,
    )


@output_guardrail(name="Custom name")
def decorated_named_output_guardrail(
    context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any
) -> GuardrailFunctionOutput:
    return GuardrailFunctionOutput(
        output_info="test_4",
        tripwire_triggered=False,
    )


@pytest.mark.asyncio
async def test_output_guardrail_decorators():
    guardrail = decorated_output_guardrail
    result = await guardrail.run(
        agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info == "test_3"

    guardrail = decorated_named_output_guardrail
    result = await guardrail.run(
        agent=Agent(name="test"), agent_output="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info == "test_4"
    assert guardrail.get_name() == "Custom name"


@pytest.mark.asyncio
async def test_input_guardrail_run_in_parallel_default():
    guardrail = InputGuardrail(
        guardrail_function=lambda ctx, agent, input: GuardrailFunctionOutput(
            output_info=None, tripwire_triggered=False
        )
    )
    assert guardrail.run_in_parallel is True


@pytest.mark.asyncio
async def test_input_guardrail_run_in_parallel_false():
    guardrail = InputGuardrail(
        guardrail_function=lambda ctx, agent, input: GuardrailFunctionOutput(
            output_info=None, tripwire_triggered=False
        ),
        run_in_parallel=False,
    )
    assert guardrail.run_in_parallel is False


@pytest.mark.asyncio
async def test_input_guardrail_decorator_with_run_in_parallel():
    @input_guardrail(run_in_parallel=False)
    def blocking_guardrail(
        context: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(
            output_info="blocking",
            tripwire_triggered=False,
        )

    assert blocking_guardrail.run_in_parallel is False
    result = await blocking_guardrail.run(
        agent=Agent(name="test"), input="test", context=RunContextWrapper(context=None)
    )
    assert not result.output.tripwire_triggered
    assert result.output.output_info == "blocking"


@pytest.mark.asyncio
async def test_input_guardrail_decorator_with_name_and_run_in_parallel():
    @input_guardrail(name="custom_name", run_in_parallel=False)
    def named_blocking_guardrail(
        context: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        return GuardrailFunctionOutput(
            output_info="named_blocking",
            tripwire_triggered=False,
        )

    assert named_blocking_guardrail.get_name() == "custom_name"
    assert named_blocking_guardrail.run_in_parallel is False


@pytest.mark.asyncio
async def test_parallel_guardrail_runs_concurrently_with_agent():
    guardrail_executed = False

    @input_guardrail(run_in_parallel=True)
    async def parallel_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal guardrail_executed
        await asyncio.sleep(0.3)
        guardrail_executed = True
        return GuardrailFunctionOutput(
            output_info="parallel_ok",
            tripwire_triggered=False,
        )

    model = FakeModel()
    agent = Agent(
        name="test_agent",
        instructions="Reply with 'hello'",
        input_guardrails=[parallel_check],
        model=model,
    )
    model.set_next_output([get_text_message("hello")])

    result = await Runner.run(agent, "test input")

    assert guardrail_executed is True
    assert result.final_output is not None
    assert len(result.input_guardrail_results) == 1
    assert result.input_guardrail_results[0].output.output_info == "parallel_ok"
    assert model.first_turn_args is not None, "Model should have been called in parallel mode"


@pytest.mark.asyncio
async def test_parallel_guardrail_runs_concurrently_with_agent_streaming():
    guardrail_executed = False

    @input_guardrail(run_in_parallel=True)
    async def parallel_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal guardrail_executed
        await asyncio.sleep(0.1)
        guardrail_executed = True
        return GuardrailFunctionOutput(
            output_info="parallel_streaming_ok",
            tripwire_triggered=False,
        )

    model = FakeModel()
    agent = Agent(
        name="streaming_agent",
        instructions="Reply with 'hello'",
        input_guardrails=[parallel_check],
        model=model,
    )
    model.set_next_output([get_text_message("hello from stream")])

    result = Runner.run_streamed(agent, "test input")

    received_events = False
    async for _event in result.stream_events():
        received_events = True

    assert guardrail_executed is True
    assert received_events is True
    assert model.first_turn_args is not None, "Model should have been called in parallel mode"


@pytest.mark.asyncio
async def test_blocking_guardrail_prevents_agent_execution():
    guardrail_executed = False

    @input_guardrail(run_in_parallel=False)
    async def blocking_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal guardrail_executed
        guardrail_executed = True
        await asyncio.sleep(0.3)
        return GuardrailFunctionOutput(
            output_info="security_violation",
            tripwire_triggered=True,
        )

    model = FakeModel()
    agent = Agent(
        name="test_agent",
        instructions="Reply with 'hello'",
        input_guardrails=[blocking_check],
        model=model,
    )
    model.set_next_output([get_text_message("hello")])

    with pytest.raises(InputGuardrailTripwireTriggered) as exc_info:
        await Runner.run(agent, "test input")

    assert guardrail_executed is True
    assert exc_info.value.guardrail_result.output.output_info == "security_violation"
    assert model.first_turn_args is None, "Model should not have been called"


@pytest.mark.asyncio
async def test_blocking_guardrail_prevents_agent_execution_streaming():
    guardrail_executed = False

    @input_guardrail(run_in_parallel=False)
    async def blocking_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal guardrail_executed
        guardrail_executed = True
        await asyncio.sleep(0.3)
        return GuardrailFunctionOutput(
            output_info="blocked_streaming",
            tripwire_triggered=True,
        )

    model = FakeModel()
    agent = Agent(
        name="streaming_agent",
        instructions="Reply with a long message",
        input_guardrails=[blocking_check],
        model=model,
    )
    model.set_next_output([get_text_message("hello")])

    result = Runner.run_streamed(agent, "test input")

    with pytest.raises(InputGuardrailTripwireTriggered):
        async for _event in result.stream_events():
            pass

    assert guardrail_executed is True
    assert model.first_turn_args is None, "Model should not have been called"


@pytest.mark.asyncio
async def test_parallel_guardrail_may_not_prevent_tool_execution():
    tool_was_executed = False
    guardrail_executed = False

    @function_tool
    def fast_tool() -> str:
        nonlocal tool_was_executed
        tool_was_executed = True
        return "tool_executed"

    @input_guardrail(run_in_parallel=True)
    async def slow_parallel_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal guardrail_executed
        await asyncio.sleep(0.5)
        guardrail_executed = True
        return GuardrailFunctionOutput(
            output_info="slow_parallel_triggered",
            tripwire_triggered=True,
        )

    model = FakeModel()
    agent = Agent(
        name="agent_with_tools",
        instructions="Call the fast_tool immediately",
        tools=[fast_tool],
        input_guardrails=[slow_parallel_check],
        model=model,
    )
    model.set_next_output([get_function_tool_call("fast_tool", arguments="{}")])
    model.set_next_output([get_text_message("done")])

    with pytest.raises(InputGuardrailTripwireTriggered):
        await Runner.run(agent, "trigger guardrail")

    assert guardrail_executed is True
    assert tool_was_executed is True, (
        "Expected tool to execute before slow parallel guardrail triggered"
    )
    assert model.first_turn_args is not None, "Model should have been called in parallel mode"


@pytest.mark.asyncio
async def test_parallel_guardrail_trip_cancels_model_task():
    model_started = asyncio.Event()
    model_cancelled = asyncio.Event()
    model_finished = asyncio.Event()

    @input_guardrail(run_in_parallel=True)
    async def tripwire_after_model_starts(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        await asyncio.wait_for(model_started.wait(), timeout=1)
        return GuardrailFunctionOutput(
            output_info="parallel_tripwire",
            tripwire_triggered=True,
        )

    model = FakeModel()
    original_get_response = model.get_response

    async def slow_get_response(*args, **kwargs):
        model_started.set()
        try:
            await asyncio.sleep(0.2)
            return await original_get_response(*args, **kwargs)
        except asyncio.CancelledError:
            model_cancelled.set()
            raise
        finally:
            model_finished.set()

    agent = Agent(
        name="parallel_tripwire_agent",
        instructions="Reply with 'hello'",
        input_guardrails=[tripwire_after_model_starts],
        model=model,
    )
    model.set_next_output([get_text_message("should_not_finish")])

    with patch.object(model, "get_response", side_effect=slow_get_response):
        with pytest.raises(InputGuardrailTripwireTriggered):
            await Runner.run(agent, "trigger guardrail")

    await asyncio.wait_for(model_finished.wait(), timeout=1)
    assert model_started.is_set() is True
    assert model_cancelled.is_set() is True


@pytest.mark.asyncio
async def test_parallel_guardrail_trip_compat_mode_does_not_cancel_model_task():
    model_started = asyncio.Event()
    model_cancelled = asyncio.Event()
    model_finished = asyncio.Event()

    @input_guardrail(run_in_parallel=True)
    async def tripwire_after_model_starts(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        await asyncio.wait_for(model_started.wait(), timeout=1)
        return GuardrailFunctionOutput(
            output_info="parallel_tripwire",
            tripwire_triggered=True,
        )

    model = FakeModel()
    original_get_response = model.get_response

    async def slow_get_response(*args, **kwargs):
        model_started.set()
        try:
            await asyncio.sleep(0.2)
            return await original_get_response(*args, **kwargs)
        except asyncio.CancelledError:
            model_cancelled.set()
            raise
        finally:
            model_finished.set()

    agent = Agent(
        name="parallel_tripwire_agent",
        instructions="Reply with 'hello'",
        input_guardrails=[tripwire_after_model_starts],
        model=model,
    )
    model.set_next_output([get_text_message("should_finish_without_cancel")])

    with patch.object(model, "get_response", side_effect=slow_get_response):
        with patch(
            "agents.run.should_cancel_parallel_model_task_on_input_guardrail_trip",
            return_value=False,
        ):
            with pytest.raises(InputGuardrailTripwireTriggered):
                await Runner.run(agent, "trigger guardrail")

    await asyncio.wait_for(model_finished.wait(), timeout=1)
    assert model_started.is_set() is True
    assert model_cancelled.is_set() is False


@pytest.mark.asyncio
async def test_parallel_guardrail_may_not_prevent_tool_execution_streaming():
    tool_was_executed = False
    guardrail_executed = False

    @function_tool
    def fast_tool() -> str:
        nonlocal tool_was_executed
        tool_was_executed = True
        return "tool_executed"

    @input_guardrail(run_in_parallel=True)
    async def slow_parallel_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal guardrail_executed
        await asyncio.sleep(0.5)
        guardrail_executed = True
        return GuardrailFunctionOutput(
            output_info="slow_parallel_triggered_streaming",
            tripwire_triggered=True,
        )

    model = FakeModel()
    agent = Agent(
        name="agent_with_tools",
        instructions="Call the fast_tool immediately",
        tools=[fast_tool],
        input_guardrails=[slow_parallel_check],
        model=model,
    )
    model.set_next_output([get_function_tool_call("fast_tool", arguments="{}")])
    model.set_next_output([get_text_message("done")])

    result = Runner.run_streamed(agent, "trigger guardrail")

    with pytest.raises(InputGuardrailTripwireTriggered):
        async for _event in result.stream_events():
            pass

    assert guardrail_executed is True
    assert tool_was_executed is True, (
        "Expected tool to execute before slow parallel guardrail triggered"
    )
    assert model.first_turn_args is not None, "Model should have been called in parallel mode"


@pytest.mark.asyncio
async def test_blocking_guardrail_prevents_tool_execution():
    tool_was_executed = False
    guardrail_executed = False

    @function_tool
    def dangerous_tool() -> str:
        nonlocal tool_was_executed
        tool_was_executed = True
        return "tool_executed"

    @input_guardrail(run_in_parallel=False)
    async def security_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal guardrail_executed
        await asyncio.sleep(0.3)
        guardrail_executed = True
        return GuardrailFunctionOutput(
            output_info="blocked_dangerous_input",
            tripwire_triggered=True,
        )

    model = FakeModel()
    agent = Agent(
        name="agent_with_tools",
        instructions="Call the dangerous_tool immediately",
        tools=[dangerous_tool],
        input_guardrails=[security_check],
        model=model,
    )
    model.set_next_output([get_function_tool_call("dangerous_tool", arguments="{}")])

    with pytest.raises(InputGuardrailTripwireTriggered):
        await Runner.run(agent, "trigger guardrail")

    assert guardrail_executed is True
    assert tool_was_executed is False
    assert model.first_turn_args is None, "Model should not have been called"


@pytest.mark.asyncio
async def test_blocking_guardrail_prevents_tool_execution_streaming():
    tool_was_executed = False
    guardrail_executed = False

    @function_tool
    def dangerous_tool() -> str:
        nonlocal tool_was_executed
        tool_was_executed = True
        return "tool_executed"

    @input_guardrail(run_in_parallel=False)
    async def security_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal guardrail_executed
        await asyncio.sleep(0.3)
        guardrail_executed = True
        return GuardrailFunctionOutput(
            output_info="blocked_dangerous_input_streaming",
            tripwire_triggered=True,
        )

    model = FakeModel()
    agent = Agent(
        name="agent_with_tools",
        instructions="Call the dangerous_tool immediately",
        tools=[dangerous_tool],
        input_guardrails=[security_check],
        model=model,
    )
    model.set_next_output([get_function_tool_call("dangerous_tool", arguments="{}")])

    result = Runner.run_streamed(agent, "trigger guardrail")

    with pytest.raises(InputGuardrailTripwireTriggered):
        async for _event in result.stream_events():
            pass

    assert guardrail_executed is True
    assert tool_was_executed is False
    assert model.first_turn_args is None, "Model should not have been called"


@pytest.mark.asyncio
async def test_parallel_guardrail_passes_agent_continues():
    guardrail_executed = False

    @input_guardrail(run_in_parallel=True)
    async def parallel_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal guardrail_executed
        await asyncio.sleep(0.1)
        guardrail_executed = True
        return GuardrailFunctionOutput(
            output_info="parallel_passed",
            tripwire_triggered=False,
        )

    model = FakeModel()
    agent = Agent(
        name="test_agent",
        instructions="Reply with 'success'",
        input_guardrails=[parallel_check],
        model=model,
    )
    model.set_next_output([get_text_message("success")])

    result = await Runner.run(agent, "test input")

    assert guardrail_executed is True
    assert result.final_output is not None
    assert model.first_turn_args is not None, "Model should have been called"


@pytest.mark.asyncio
async def test_parallel_guardrail_passes_agent_continues_streaming():
    guardrail_executed = False

    @input_guardrail(run_in_parallel=True)
    async def parallel_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal guardrail_executed
        await asyncio.sleep(0.1)
        guardrail_executed = True
        return GuardrailFunctionOutput(
            output_info="parallel_passed_streaming",
            tripwire_triggered=False,
        )

    model = FakeModel()
    agent = Agent(
        name="test_agent",
        instructions="Reply with 'success'",
        input_guardrails=[parallel_check],
        model=model,
    )
    model.set_next_output([get_text_message("success")])

    result = Runner.run_streamed(agent, "test input")

    received_events = False
    async for _event in result.stream_events():
        received_events = True

    assert guardrail_executed is True
    assert received_events is True
    assert model.first_turn_args is not None, "Model should have been called"


@pytest.mark.asyncio
async def test_blocking_guardrail_passes_agent_continues():
    guardrail_executed = False

    @input_guardrail(run_in_parallel=False)
    async def blocking_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal guardrail_executed
        await asyncio.sleep(0.3)
        guardrail_executed = True
        return GuardrailFunctionOutput(
            output_info="blocking_passed",
            tripwire_triggered=False,
        )

    model = FakeModel()
    agent = Agent(
        name="test_agent",
        instructions="Reply with 'success'",
        input_guardrails=[blocking_check],
        model=model,
    )
    model.set_next_output([get_text_message("success")])

    result = await Runner.run(agent, "test input")

    assert guardrail_executed is True
    assert result.final_output is not None
    assert model.first_turn_args is not None, "Model should have been called after guardrail passed"


@pytest.mark.asyncio
async def test_blocking_guardrail_passes_agent_continues_streaming():
    guardrail_executed = False

    @input_guardrail(run_in_parallel=False)
    async def blocking_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal guardrail_executed
        await asyncio.sleep(0.3)
        guardrail_executed = True
        return GuardrailFunctionOutput(
            output_info="blocking_passed_streaming",
            tripwire_triggered=False,
        )

    model = FakeModel()
    agent = Agent(
        name="test_agent",
        instructions="Reply with 'success'",
        input_guardrails=[blocking_check],
        model=model,
    )
    model.set_next_output([get_text_message("success")])

    result = Runner.run_streamed(agent, "test input")

    received_events = False
    async for _event in result.stream_events():
        received_events = True

    assert guardrail_executed is True
    assert received_events is True
    assert model.first_turn_args is not None, "Model should have been called after guardrail passed"


@pytest.mark.asyncio
async def test_mixed_blocking_and_parallel_guardrails():
    timestamps = {}

    @input_guardrail(run_in_parallel=False)
    async def blocking_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        timestamps["blocking_start"] = time.time()
        await asyncio.sleep(0.3)
        timestamps["blocking_end"] = time.time()
        return GuardrailFunctionOutput(
            output_info="blocking_passed",
            tripwire_triggered=False,
        )

    @input_guardrail(run_in_parallel=True)
    async def parallel_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        timestamps["parallel_start"] = time.time()
        await asyncio.sleep(0.3)
        timestamps["parallel_end"] = time.time()
        return GuardrailFunctionOutput(
            output_info="parallel_passed",
            tripwire_triggered=False,
        )

    model = FakeModel()

    original_get_response = model.get_response

    async def tracked_get_response(*args, **kwargs):
        timestamps["model_called"] = time.time()
        return await original_get_response(*args, **kwargs)

    agent = Agent(
        name="mixed_agent",
        instructions="Reply with 'hello'",
        input_guardrails=[blocking_check, parallel_check],
        model=model,
    )
    model.set_next_output([get_text_message("hello")])

    with patch.object(model, "get_response", side_effect=tracked_get_response):
        result = await Runner.run(agent, "test input")

    assert result.final_output is not None
    assert len(result.input_guardrail_results) == 2

    assert "blocking_start" in timestamps
    assert "blocking_end" in timestamps
    assert "parallel_start" in timestamps
    assert "parallel_end" in timestamps
    assert "model_called" in timestamps

    assert timestamps["blocking_end"] <= timestamps["parallel_start"], (
        "Blocking must complete before parallel starts"
    )
    assert timestamps["blocking_end"] <= timestamps["model_called"], (
        "Blocking must complete before model is called"
    )
    assert timestamps["model_called"] <= timestamps["parallel_end"], (
        "Model called while parallel guardrail still running"
    )
    assert model.first_turn_args is not None, (
        "Model should have been called after blocking guardrails passed"
    )


@pytest.mark.asyncio
async def test_mixed_blocking_and_parallel_guardrails_streaming():
    timestamps = {}

    @input_guardrail(run_in_parallel=False)
    async def blocking_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        timestamps["blocking_start"] = time.time()
        await asyncio.sleep(0.3)
        timestamps["blocking_end"] = time.time()
        return GuardrailFunctionOutput(
            output_info="blocking_passed",
            tripwire_triggered=False,
        )

    @input_guardrail(run_in_parallel=True)
    async def parallel_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        timestamps["parallel_start"] = time.time()
        await asyncio.sleep(0.3)
        timestamps["parallel_end"] = time.time()
        return GuardrailFunctionOutput(
            output_info="parallel_passed",
            tripwire_triggered=False,
        )

    model = FakeModel()

    original_stream_response = model.stream_response

    async def tracked_stream_response(*args, **kwargs):
        timestamps["model_called"] = time.time()
        async for event in original_stream_response(*args, **kwargs):
            yield event

    agent = Agent(
        name="mixed_agent",
        instructions="Reply with 'hello'",
        input_guardrails=[blocking_check, parallel_check],
        model=model,
    )
    model.set_next_output([get_text_message("hello")])

    with patch.object(model, "stream_response", side_effect=tracked_stream_response):
        result = Runner.run_streamed(agent, "test input")

        received_events = False
        async for _event in result.stream_events():
            received_events = True

    assert received_events is True
    assert "blocking_start" in timestamps
    assert "blocking_end" in timestamps
    assert "parallel_start" in timestamps
    assert "parallel_end" in timestamps
    assert "model_called" in timestamps

    assert timestamps["blocking_end"] <= timestamps["parallel_start"], (
        "Blocking must complete before parallel starts"
    )
    assert timestamps["blocking_end"] <= timestamps["model_called"], (
        "Blocking must complete before model is called"
    )
    assert timestamps["model_called"] <= timestamps["parallel_end"], (
        "Model called while parallel guardrail still running"
    )
    assert model.first_turn_args is not None, (
        "Model should have been called after blocking guardrails passed"
    )


@pytest.mark.asyncio
async def test_multiple_blocking_guardrails_complete_before_agent():
    timestamps = {}

    @input_guardrail(run_in_parallel=False)
    async def first_blocking_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        timestamps["first_blocking_start"] = time.time()
        await asyncio.sleep(0.3)
        timestamps["first_blocking_end"] = time.time()
        return GuardrailFunctionOutput(
            output_info="first_passed",
            tripwire_triggered=False,
        )

    @input_guardrail(run_in_parallel=False)
    async def second_blocking_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        timestamps["second_blocking_start"] = time.time()
        await asyncio.sleep(0.3)
        timestamps["second_blocking_end"] = time.time()
        return GuardrailFunctionOutput(
            output_info="second_passed",
            tripwire_triggered=False,
        )

    model = FakeModel()

    original_get_response = model.get_response

    async def tracked_get_response(*args, **kwargs):
        timestamps["model_called"] = time.time()
        return await original_get_response(*args, **kwargs)

    agent = Agent(
        name="multi_blocking_agent",
        instructions="Reply with 'hello'",
        input_guardrails=[first_blocking_check, second_blocking_check],
        model=model,
    )
    model.set_next_output([get_text_message("hello")])

    with patch.object(model, "get_response", side_effect=tracked_get_response):
        result = await Runner.run(agent, "test input")

    assert result.final_output is not None
    assert len(result.input_guardrail_results) == 2

    assert "first_blocking_start" in timestamps
    assert "first_blocking_end" in timestamps
    assert "second_blocking_start" in timestamps
    assert "second_blocking_end" in timestamps
    assert "model_called" in timestamps

    assert timestamps["first_blocking_end"] <= timestamps["model_called"], (
        "First blocking guardrail must complete before model is called"
    )
    assert timestamps["second_blocking_end"] <= timestamps["model_called"], (
        "Second blocking guardrail must complete before model is called"
    )
    assert model.first_turn_args is not None, (
        "Model should have been called after all blocking guardrails passed"
    )


@pytest.mark.asyncio
async def test_multiple_blocking_guardrails_complete_before_agent_streaming():
    timestamps = {}

    @input_guardrail(run_in_parallel=False)
    async def first_blocking_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        timestamps["first_blocking_start"] = time.time()
        await asyncio.sleep(0.3)
        timestamps["first_blocking_end"] = time.time()
        return GuardrailFunctionOutput(
            output_info="first_passed",
            tripwire_triggered=False,
        )

    @input_guardrail(run_in_parallel=False)
    async def second_blocking_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        timestamps["second_blocking_start"] = time.time()
        await asyncio.sleep(0.3)
        timestamps["second_blocking_end"] = time.time()
        return GuardrailFunctionOutput(
            output_info="second_passed",
            tripwire_triggered=False,
        )

    model = FakeModel()

    original_stream_response = model.stream_response

    async def tracked_stream_response(*args, **kwargs):
        timestamps["model_called"] = time.time()
        async for event in original_stream_response(*args, **kwargs):
            yield event

    agent = Agent(
        name="multi_blocking_agent",
        instructions="Reply with 'hello'",
        input_guardrails=[first_blocking_check, second_blocking_check],
        model=model,
    )
    model.set_next_output([get_text_message("hello")])

    with patch.object(model, "stream_response", side_effect=tracked_stream_response):
        result = Runner.run_streamed(agent, "test input")

        received_events = False
        async for _event in result.stream_events():
            received_events = True

    assert received_events is True
    assert "first_blocking_start" in timestamps
    assert "first_blocking_end" in timestamps
    assert "second_blocking_start" in timestamps
    assert "second_blocking_end" in timestamps
    assert "model_called" in timestamps

    assert timestamps["first_blocking_end"] <= timestamps["model_called"], (
        "First blocking guardrail must complete before model is called"
    )
    assert timestamps["second_blocking_end"] <= timestamps["model_called"], (
        "Second blocking guardrail must complete before model is called"
    )
    assert model.first_turn_args is not None, (
        "Model should have been called after all blocking guardrails passed"
    )


@pytest.mark.asyncio
async def test_multiple_blocking_guardrails_one_triggers():
    timestamps = {}
    first_guardrail_executed = False
    second_guardrail_executed = False

    @input_guardrail(run_in_parallel=False)
    async def first_blocking_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal first_guardrail_executed
        timestamps["first_blocking_start"] = time.time()
        await asyncio.sleep(0.3)
        first_guardrail_executed = True
        timestamps["first_blocking_end"] = time.time()
        return GuardrailFunctionOutput(
            output_info="first_passed",
            tripwire_triggered=False,
        )

    @input_guardrail(run_in_parallel=False)
    async def second_blocking_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal second_guardrail_executed
        timestamps["second_blocking_start"] = time.time()
        await asyncio.sleep(0.3)
        second_guardrail_executed = True
        timestamps["second_blocking_end"] = time.time()
        return GuardrailFunctionOutput(
            output_info="second_triggered",
            tripwire_triggered=True,
        )

    model = FakeModel()
    agent = Agent(
        name="multi_blocking_agent",
        instructions="Reply with 'hello'",
        input_guardrails=[first_blocking_check, second_blocking_check],
        model=model,
    )
    model.set_next_output([get_text_message("hello")])

    with pytest.raises(InputGuardrailTripwireTriggered):
        await Runner.run(agent, "test input")

    assert first_guardrail_executed is True
    assert second_guardrail_executed is True
    assert "first_blocking_start" in timestamps
    assert "first_blocking_end" in timestamps
    assert "second_blocking_start" in timestamps
    assert "second_blocking_end" in timestamps
    assert model.first_turn_args is None, (
        "Model should not have been called when guardrail triggered"
    )


@pytest.mark.asyncio
async def test_multiple_blocking_guardrails_one_triggers_streaming():
    timestamps = {}
    first_guardrail_executed = False
    second_guardrail_executed = False

    @input_guardrail(run_in_parallel=False)
    async def first_blocking_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal first_guardrail_executed
        timestamps["first_blocking_start"] = time.time()
        await asyncio.sleep(0.3)
        first_guardrail_executed = True
        timestamps["first_blocking_end"] = time.time()
        return GuardrailFunctionOutput(
            output_info="first_passed",
            tripwire_triggered=False,
        )

    @input_guardrail(run_in_parallel=False)
    async def second_blocking_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal second_guardrail_executed
        timestamps["second_blocking_start"] = time.time()
        await asyncio.sleep(0.3)
        second_guardrail_executed = True
        timestamps["second_blocking_end"] = time.time()
        return GuardrailFunctionOutput(
            output_info="second_triggered",
            tripwire_triggered=True,
        )

    model = FakeModel()
    agent = Agent(
        name="multi_blocking_agent",
        instructions="Reply with 'hello'",
        input_guardrails=[first_blocking_check, second_blocking_check],
        model=model,
    )
    model.set_next_output([get_text_message("hello")])

    result = Runner.run_streamed(agent, "test input")

    with pytest.raises(InputGuardrailTripwireTriggered):
        async for _event in result.stream_events():
            pass

    assert first_guardrail_executed is True
    assert second_guardrail_executed is True
    assert "first_blocking_start" in timestamps
    assert "first_blocking_end" in timestamps
    assert "second_blocking_start" in timestamps
    assert "second_blocking_end" in timestamps
    assert model.first_turn_args is None, (
        "Model should not have been called when guardrail triggered"
    )


@pytest.mark.asyncio
async def test_guardrail_via_agent_and_run_config_equivalent():
    agent_guardrail_executed = False
    config_guardrail_executed = False

    @input_guardrail(run_in_parallel=False)
    async def agent_level_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal agent_guardrail_executed
        agent_guardrail_executed = True
        return GuardrailFunctionOutput(
            output_info="agent_level_passed",
            tripwire_triggered=False,
        )

    @input_guardrail(run_in_parallel=False)
    async def config_level_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal config_guardrail_executed
        config_guardrail_executed = True
        return GuardrailFunctionOutput(
            output_info="config_level_passed",
            tripwire_triggered=False,
        )

    model1 = FakeModel()
    agent_with_guardrail = Agent(
        name="test_agent",
        instructions="Reply with 'hello'",
        input_guardrails=[agent_level_check],
        model=model1,
    )
    model1.set_next_output([get_text_message("hello")])

    model2 = FakeModel()
    agent_without_guardrail = Agent(
        name="test_agent",
        instructions="Reply with 'hello'",
        model=model2,
    )
    model2.set_next_output([get_text_message("hello")])
    run_config = RunConfig(input_guardrails=[config_level_check])

    result1 = await Runner.run(agent_with_guardrail, "test input")
    result2 = await Runner.run(agent_without_guardrail, "test input", run_config=run_config)

    assert agent_guardrail_executed is True
    assert config_guardrail_executed is True
    assert len(result1.input_guardrail_results) == 1
    assert len(result2.input_guardrail_results) == 1
    assert result1.input_guardrail_results[0].output.output_info == "agent_level_passed"
    assert result2.input_guardrail_results[0].output.output_info == "config_level_passed"
    assert result1.final_output is not None
    assert result2.final_output is not None
    assert model1.first_turn_args is not None
    assert model2.first_turn_args is not None


@pytest.mark.asyncio
async def test_blocking_guardrail_cancels_remaining_on_trigger():
    """
    Test that when one blocking guardrail triggers, remaining guardrails
    are cancelled (non-streaming).
    """
    fast_guardrail_executed = False
    slow_guardrail_executed = False
    slow_guardrail_cancelled = False
    timestamps = {}

    @input_guardrail(run_in_parallel=False)
    async def fast_guardrail_that_triggers(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal fast_guardrail_executed
        timestamps["fast_start"] = time.time()
        await asyncio.sleep(0.1)
        fast_guardrail_executed = True
        timestamps["fast_end"] = time.time()
        return GuardrailFunctionOutput(
            output_info="fast_triggered",
            tripwire_triggered=True,
        )

    @input_guardrail(run_in_parallel=False)
    async def slow_guardrail_that_should_be_cancelled(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal slow_guardrail_executed, slow_guardrail_cancelled
        timestamps["slow_start"] = time.time()
        try:
            await asyncio.sleep(0.3)
            slow_guardrail_executed = True
            timestamps["slow_end"] = time.time()
            return GuardrailFunctionOutput(
                output_info="slow_completed",
                tripwire_triggered=False,
            )
        except asyncio.CancelledError:
            slow_guardrail_cancelled = True
            timestamps["slow_cancelled"] = time.time()
            raise

    model = FakeModel()
    agent = Agent(
        name="test_agent",
        instructions="Reply with 'hello'",
        input_guardrails=[fast_guardrail_that_triggers, slow_guardrail_that_should_be_cancelled],
        model=model,
    )
    model.set_next_output([get_text_message("hello")])

    with pytest.raises(InputGuardrailTripwireTriggered):
        await Runner.run(agent, "test input")

    # Verify the fast guardrail executed
    assert fast_guardrail_executed is True, "Fast guardrail should have executed"

    # Verify the slow guardrail was cancelled, not completed
    assert slow_guardrail_cancelled is True, "Slow guardrail should have been cancelled"
    assert slow_guardrail_executed is False, "Slow guardrail should NOT have completed execution"

    # Verify timing: cancellation happened shortly after fast guardrail triggered
    assert "fast_end" in timestamps
    assert "slow_cancelled" in timestamps
    cancellation_delay = timestamps["slow_cancelled"] - timestamps["fast_end"]
    assert cancellation_delay >= 0, (
        f"Slow guardrail should be cancelled after fast one completes, "
        f"but was {cancellation_delay:.2f}s"
    )
    assert cancellation_delay < 0.2, (
        f"Cancellation should happen before the slow guardrail completes, "
        f"but took {cancellation_delay:.2f}s"
    )

    # Verify agent never started
    assert model.first_turn_args is None, (
        "Model should not have been called when guardrail triggered"
    )


@pytest.mark.asyncio
async def test_blocking_guardrail_cancels_remaining_on_trigger_streaming():
    """
    Test that when one blocking guardrail triggers, remaining guardrails
    are cancelled (streaming).
    """
    fast_guardrail_executed = False
    slow_guardrail_executed = False
    slow_guardrail_cancelled = False
    timestamps = {}

    @input_guardrail(run_in_parallel=False)
    async def fast_guardrail_that_triggers(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal fast_guardrail_executed
        timestamps["fast_start"] = time.time()
        await asyncio.sleep(0.1)
        fast_guardrail_executed = True
        timestamps["fast_end"] = time.time()
        return GuardrailFunctionOutput(
            output_info="fast_triggered",
            tripwire_triggered=True,
        )

    @input_guardrail(run_in_parallel=False)
    async def slow_guardrail_that_should_be_cancelled(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal slow_guardrail_executed, slow_guardrail_cancelled
        timestamps["slow_start"] = time.time()
        try:
            await asyncio.sleep(0.3)
            slow_guardrail_executed = True
            timestamps["slow_end"] = time.time()
            return GuardrailFunctionOutput(
                output_info="slow_completed",
                tripwire_triggered=False,
            )
        except asyncio.CancelledError:
            slow_guardrail_cancelled = True
            timestamps["slow_cancelled"] = time.time()
            raise

    model = FakeModel()
    agent = Agent(
        name="test_agent",
        instructions="Reply with 'hello'",
        input_guardrails=[fast_guardrail_that_triggers, slow_guardrail_that_should_be_cancelled],
        model=model,
    )
    model.set_next_output([get_text_message("hello")])

    result = Runner.run_streamed(agent, "test input")

    with pytest.raises(InputGuardrailTripwireTriggered):
        async for _event in result.stream_events():
            pass

    # Verify the fast guardrail executed
    assert fast_guardrail_executed is True, "Fast guardrail should have executed"

    # Verify the slow guardrail was cancelled, not completed
    assert slow_guardrail_cancelled is True, "Slow guardrail should have been cancelled"
    assert slow_guardrail_executed is False, "Slow guardrail should NOT have completed execution"

    # Verify timing: cancellation happened shortly after fast guardrail triggered
    assert "fast_end" in timestamps
    assert "slow_cancelled" in timestamps
    cancellation_delay = timestamps["slow_cancelled"] - timestamps["fast_end"]
    assert cancellation_delay >= 0, (
        f"Slow guardrail should be cancelled after fast one completes, "
        f"but was {cancellation_delay:.2f}s"
    )
    assert cancellation_delay < 0.2, (
        f"Cancellation should happen before the slow guardrail completes, "
        f"but took {cancellation_delay:.2f}s"
    )

    # Verify agent never started
    assert model.first_turn_args is None, (
        "Model should not have been called when guardrail triggered"
    )
