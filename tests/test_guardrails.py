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
    OutputGuardrailTripwireTriggered,
    RunConfig,
    RunContextWrapper,
    Runner,
    TResponseInputItem,
    UserError,
    function_tool,
)
from agents.guardrail import input_guardrail, output_guardrail
from agents.result import RunResultStreaming
from agents.run_internal.guardrails import run_input_guardrails, run_input_guardrails_with_queue

from .fake_model import FakeModel
from .test_responses import get_function_tool_call, get_text_message
from .testing_processor import fetch_events

SHORT_DELAY = 0.01
MEDIUM_DELAY = 0.03
LONG_DELAY = 0.05


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
async def test_run_input_guardrails_returns_empty_for_no_guardrails() -> None:
    result = await run_input_guardrails(
        agent=Agent(name="test"),
        guardrails=[],
        input="test",
        context=RunContextWrapper(context=None),
    )

    assert result == []


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
    assert guardrail.get_name() == "decorated_input_guardrail"

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
    assert guardrail.get_name() == "decorated_output_guardrail"

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
        await asyncio.sleep(MEDIUM_DELAY)
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
        await asyncio.sleep(SHORT_DELAY)
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
        await asyncio.sleep(MEDIUM_DELAY)
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
        await asyncio.sleep(MEDIUM_DELAY)
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
    loop = asyncio.get_running_loop()
    tool_executed = asyncio.Event()

    @function_tool
    def fast_tool() -> str:
        nonlocal tool_was_executed
        tool_was_executed = True
        # Sync tools run in a worker thread, so signal the loop-owned event safely.
        loop.call_soon_threadsafe(tool_executed.set)
        return "tool_executed"

    @input_guardrail(run_in_parallel=True)
    async def slow_parallel_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal guardrail_executed
        # Trip only after the tool ran. If parallel guardrails gated the turn, this
        # would deadlock instead of silently losing a wall-clock race.
        await asyncio.wait_for(tool_executed.wait(), timeout=5)
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
            await asyncio.sleep(0.02)
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
            await asyncio.sleep(0.02)
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
async def test_model_error_cancels_parallel_input_guardrail_task():
    """A non-tripwire model failure must cancel the still-running guardrail task.

    Without cancellation the guardrail task is orphaned and keeps running after
    ``Runner.run`` has already raised.
    """
    guardrail_started = asyncio.Event()
    guardrail_cancelled = asyncio.Event()
    guardrail_finished = asyncio.Event()

    @input_guardrail(run_in_parallel=True)
    async def slow_parallel_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        guardrail_started.set()
        try:
            # Never finishes on its own, so only cancellation can end this guardrail.
            await asyncio.Event().wait()
            guardrail_finished.set()
            return GuardrailFunctionOutput(
                output_info="parallel_ok",
                tripwire_triggered=False,
            )
        except asyncio.CancelledError:
            guardrail_cancelled.set()
            raise

    model = FakeModel()

    async def boom_get_response(*args, **kwargs):
        # Only blow up once the guardrail is genuinely mid-flight.
        await asyncio.wait_for(guardrail_started.wait(), timeout=1)
        raise RuntimeError("model boom")

    agent = Agent(
        name="model_error_agent",
        input_guardrails=[slow_parallel_check],
        model=model,
    )

    with patch.object(model, "get_response", side_effect=boom_get_response):
        with pytest.raises(RuntimeError, match="model boom"):
            await asyncio.wait_for(Runner.run(agent, "trigger guardrail"), timeout=5)

    # By the time Runner.run returns, the guardrail task must already be
    # cancelled rather than left running to completion in the background.
    assert guardrail_started.is_set() is True
    assert guardrail_cancelled.is_set() is True
    assert guardrail_finished.is_set() is False


@pytest.mark.asyncio
async def test_parallel_guardrail_non_tripwire_error_not_swallowed():
    """A non-tripwire error raised inside a parallel guardrail must propagate.

    It should also cancel the in-flight model task rather than leave it running.
    """
    model_started = asyncio.Event()
    model_cancelled = asyncio.Event()
    model_finished = asyncio.Event()

    @input_guardrail(run_in_parallel=True)
    async def raising_parallel_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        await asyncio.wait_for(model_started.wait(), timeout=1)
        raise ValueError("guardrail boom")

    model = FakeModel()
    original_get_response = model.get_response

    async def slow_get_response(*args, **kwargs):
        model_started.set()
        try:
            # Never finishes on its own, so only cancellation can end the model call.
            await asyncio.Event().wait()
            return await original_get_response(*args, **kwargs)
        except asyncio.CancelledError:
            model_cancelled.set()
            raise
        finally:
            model_finished.set()

    agent = Agent(
        name="guardrail_error_agent",
        input_guardrails=[raising_parallel_check],
        model=model,
    )
    model.set_next_output([get_text_message("should_not_finish")])

    with patch.object(model, "get_response", side_effect=slow_get_response):
        with pytest.raises(ValueError, match="guardrail boom"):
            await asyncio.wait_for(Runner.run(agent, "trigger guardrail"), timeout=5)

    await asyncio.wait_for(model_finished.wait(), timeout=1)
    assert model_started.is_set() is True
    assert model_cancelled.is_set() is True


@pytest.mark.asyncio
async def test_parallel_guardrail_error_cancels_streaming_model():
    model_started = asyncio.Event()
    model_cancelled = asyncio.Event()
    model_finished = asyncio.Event()

    @input_guardrail(run_in_parallel=True)
    async def raising_parallel_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        await asyncio.wait_for(model_started.wait(), timeout=1)
        raise ValueError("guardrail boom")

    model = FakeModel()

    async def blocking_stream_response(*args, **kwargs):
        model_started.set()
        try:
            await asyncio.Event().wait()
            yield
        except asyncio.CancelledError:
            model_cancelled.set()
            raise
        finally:
            model_finished.set()

    agent = Agent(
        name="streaming_guardrail_error_agent",
        input_guardrails=[raising_parallel_check],
        model=model,
    )

    async def consume_stream() -> None:
        async for _event in result.stream_events():
            pass

    with patch.object(model, "stream_response", side_effect=blocking_stream_response):
        result = Runner.run_streamed(agent, "trigger guardrail")
        with pytest.raises(ValueError, match="guardrail boom"):
            await asyncio.wait_for(consume_stream(), timeout=1)

    await asyncio.wait_for(model_finished.wait(), timeout=1)
    assert model_started.is_set() is True
    assert model_cancelled.is_set() is True


@pytest.mark.asyncio
async def test_model_error_before_guardrail_error_preserves_stream_finalization():
    model_cleanup_started = asyncio.Event()
    allow_model_cleanup = asyncio.Event()
    model_cleanup_finished = asyncio.Event()
    model_cleanup_cancelled = asyncio.Event()
    raise_guardrail_error = asyncio.Event()

    @input_guardrail(run_in_parallel=True)
    async def raising_parallel_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        await asyncio.wait_for(model_cleanup_started.wait(), timeout=1)
        await asyncio.wait_for(raise_guardrail_error.wait(), timeout=1)
        raise ValueError("guardrail boom")

    class BlockingCleanupFakeModel(FakeModel):
        async def _cleanup_on_run_end(self, owner: object) -> None:
            model_cleanup_started.set()
            try:
                await allow_model_cleanup.wait()
                model_cleanup_finished.set()
            except asyncio.CancelledError:
                model_cleanup_cancelled.set()
                raise

    model = BlockingCleanupFakeModel(tracing_enabled=True)
    model.set_next_output(RuntimeError("model boom"))

    agent = Agent(
        name="streaming_model_error_agent",
        input_guardrails=[raising_parallel_check],
        model=model,
    )

    result = Runner.run_streamed(agent, "trigger model error")

    await asyncio.wait_for(model_cleanup_started.wait(), timeout=1)
    assert result.is_complete is True
    raise_guardrail_error.set()

    guardrail_task = result._input_guardrails_task
    assert guardrail_task is not None

    async def wait_until_guardrail_task_finishes() -> None:
        while not guardrail_task.done():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_until_guardrail_task_finishes(), timeout=1)
    allow_model_cleanup.set()

    with pytest.raises(ValueError, match="guardrail boom"):
        async for _event in result.stream_events():
            pass

    assert model_cleanup_finished.is_set() is True
    assert model_cleanup_cancelled.is_set() is False
    assert result.run_loop_task is not None
    assert result.run_loop_task.done() is True
    assert result.run_loop_task.cancelled() is False
    assert isinstance(result.run_loop_exception, RuntimeError)
    assert str(result.run_loop_exception) == "model boom"
    assert fetch_events()[-1] == "trace_end"


@pytest.mark.asyncio
async def test_parallel_guardrail_may_not_prevent_tool_execution_streaming():
    tool_was_executed = False
    guardrail_executed = False
    loop = asyncio.get_running_loop()
    tool_executed = asyncio.Event()

    @function_tool
    def fast_tool() -> str:
        nonlocal tool_was_executed
        tool_was_executed = True
        # Sync tools run in a worker thread, so signal the loop-owned event safely.
        loop.call_soon_threadsafe(tool_executed.set)
        return "tool_executed"

    @input_guardrail(run_in_parallel=True)
    async def slow_parallel_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal guardrail_executed
        # Trip only after the tool ran. If parallel guardrails gated the turn, this
        # would deadlock instead of silently losing a wall-clock race.
        await asyncio.wait_for(tool_executed.wait(), timeout=5)
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
async def test_parallel_guardrail_trip_before_tool_execution_stops_streaming_turn():
    tool_was_executed = False
    model_started = asyncio.Event()
    guardrail_tripped = asyncio.Event()

    @function_tool
    def dangerous_tool() -> str:
        nonlocal tool_was_executed
        tool_was_executed = True
        return "tool_executed"

    @input_guardrail(run_in_parallel=True)
    async def tripwire_before_tool_execution(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        await asyncio.wait_for(model_started.wait(), timeout=1)
        guardrail_tripped.set()
        return GuardrailFunctionOutput(
            output_info="parallel_trip_before_tool_execution",
            tripwire_triggered=True,
        )

    model = FakeModel()
    original_stream_response = model.stream_response

    async def delayed_stream_response(*args, **kwargs):
        model_started.set()
        await asyncio.wait_for(guardrail_tripped.wait(), timeout=1)
        await asyncio.sleep(SHORT_DELAY)
        async for event in original_stream_response(*args, **kwargs):
            yield event

    agent = Agent(
        name="streaming_guardrail_hardening_agent",
        instructions="Call the dangerous_tool immediately",
        tools=[dangerous_tool],
        input_guardrails=[tripwire_before_tool_execution],
        model=model,
    )
    model.set_next_output([get_function_tool_call("dangerous_tool", arguments="{}")])
    model.set_next_output([get_text_message("done")])

    with patch.object(model, "stream_response", side_effect=delayed_stream_response):
        result = Runner.run_streamed(agent, "trigger guardrail")

        with pytest.raises(InputGuardrailTripwireTriggered):
            async for _event in result.stream_events():
                pass

    assert model_started.is_set() is True
    assert guardrail_tripped.is_set() is True
    assert tool_was_executed is False
    assert model.first_turn_args is not None, "Model should have been called in parallel mode"


@pytest.mark.asyncio
async def test_parallel_guardrail_trip_with_slow_cancel_sibling_stops_streaming_turn():
    tool_was_executed = False
    model_started = asyncio.Event()
    guardrail_tripped = asyncio.Event()
    slow_cancel_started = asyncio.Event()
    slow_cancel_finished = asyncio.Event()

    @function_tool
    def dangerous_tool() -> str:
        nonlocal tool_was_executed
        tool_was_executed = True
        return "tool_executed"

    @input_guardrail(run_in_parallel=True)
    async def tripwire_before_tool_execution(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        await asyncio.wait_for(model_started.wait(), timeout=1)
        guardrail_tripped.set()
        return GuardrailFunctionOutput(
            output_info="parallel_trip_before_tool_execution_with_slow_cancel",
            tripwire_triggered=True,
        )

    @input_guardrail(run_in_parallel=True)
    async def slow_to_cancel_guardrail(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        try:
            await asyncio.Event().wait()
            return GuardrailFunctionOutput(
                output_info="slow_to_cancel_guardrail_completed",
                tripwire_triggered=False,
            )
        except asyncio.CancelledError:
            slow_cancel_started.set()
            await asyncio.sleep(SHORT_DELAY)
            slow_cancel_finished.set()
            raise

    model = FakeModel()
    original_stream_response = model.stream_response

    async def delayed_stream_response(*args, **kwargs):
        model_started.set()
        await asyncio.wait_for(guardrail_tripped.wait(), timeout=1)
        await asyncio.wait_for(slow_cancel_started.wait(), timeout=1)
        async for event in original_stream_response(*args, **kwargs):
            yield event

    agent = Agent(
        name="streaming_guardrail_slow_cancel_agent",
        instructions="Call the dangerous_tool immediately",
        tools=[dangerous_tool],
        input_guardrails=[tripwire_before_tool_execution, slow_to_cancel_guardrail],
        model=model,
    )
    model.set_next_output([get_function_tool_call("dangerous_tool", arguments="{}")])
    model.set_next_output([get_text_message("done")])

    with patch.object(model, "stream_response", side_effect=delayed_stream_response):
        result = Runner.run_streamed(agent, "trigger guardrail")

        with pytest.raises(InputGuardrailTripwireTriggered) as excinfo:
            async for _event in result.stream_events():
                pass

    exc = excinfo.value
    assert exc.run_data is not None
    assert [res.output.output_info for res in exc.run_data.input_guardrail_results] == [
        "parallel_trip_before_tool_execution_with_slow_cancel"
    ]
    assert model_started.is_set() is True
    assert guardrail_tripped.is_set() is True
    assert slow_cancel_started.is_set() is True
    assert slow_cancel_finished.is_set() is True
    assert tool_was_executed is False
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
        await asyncio.sleep(MEDIUM_DELAY)
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
        await asyncio.sleep(MEDIUM_DELAY)
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
        await asyncio.sleep(SHORT_DELAY)
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
        await asyncio.sleep(SHORT_DELAY)
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
        await asyncio.sleep(MEDIUM_DELAY)
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
        await asyncio.sleep(MEDIUM_DELAY)
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
    blocking_finished = asyncio.Event()
    parallel_started = asyncio.Event()
    model_called = asyncio.Event()
    parallel_finished = asyncio.Event()
    observed: dict[str, bool] = {}

    @input_guardrail(run_in_parallel=False)
    async def blocking_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        await asyncio.sleep(MEDIUM_DELAY)
        blocking_finished.set()
        return GuardrailFunctionOutput(
            output_info="blocking_passed",
            tripwire_triggered=False,
        )

    @input_guardrail(run_in_parallel=True)
    async def parallel_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        observed["blocking_finished_at_parallel_start"] = blocking_finished.is_set()
        parallel_started.set()
        await asyncio.wait_for(model_called.wait(), timeout=5)
        parallel_finished.set()
        return GuardrailFunctionOutput(
            output_info="parallel_passed",
            tripwire_triggered=False,
        )

    model = FakeModel()

    original_get_response = model.get_response

    async def tracked_get_response(*args, **kwargs):
        observed["blocking_finished_at_model_call"] = blocking_finished.is_set()
        await asyncio.wait_for(parallel_started.wait(), timeout=5)
        observed["parallel_finished_at_model_call"] = parallel_finished.is_set()
        model_called.set()
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

    assert observed["blocking_finished_at_parallel_start"] is True, (
        "Blocking must complete before parallel starts"
    )
    assert observed["blocking_finished_at_model_call"] is True, (
        "Blocking must complete before model is called"
    )
    assert observed["parallel_finished_at_model_call"] is False, (
        "Model called while parallel guardrail still running"
    )
    assert parallel_finished.is_set() is True, "Parallel guardrail should have completed"
    assert model.first_turn_args is not None, (
        "Model should have been called after blocking guardrails passed"
    )


@pytest.mark.asyncio
async def test_mixed_blocking_and_parallel_guardrails_streaming():
    blocking_finished = asyncio.Event()
    parallel_started = asyncio.Event()
    model_called = asyncio.Event()
    parallel_finished = asyncio.Event()
    observed: dict[str, bool] = {}

    @input_guardrail(run_in_parallel=False)
    async def blocking_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        await asyncio.sleep(MEDIUM_DELAY)
        blocking_finished.set()
        return GuardrailFunctionOutput(
            output_info="blocking_passed",
            tripwire_triggered=False,
        )

    @input_guardrail(run_in_parallel=True)
    async def parallel_check(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        observed["blocking_finished_at_parallel_start"] = blocking_finished.is_set()
        parallel_started.set()
        await asyncio.wait_for(model_called.wait(), timeout=5)
        parallel_finished.set()
        return GuardrailFunctionOutput(
            output_info="parallel_passed",
            tripwire_triggered=False,
        )

    model = FakeModel()

    original_stream_response = model.stream_response

    async def tracked_stream_response(*args, **kwargs):
        observed["blocking_finished_at_model_call"] = blocking_finished.is_set()
        await asyncio.wait_for(parallel_started.wait(), timeout=5)
        observed["parallel_finished_at_model_call"] = parallel_finished.is_set()
        model_called.set()
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
    assert observed["blocking_finished_at_parallel_start"] is True, (
        "Blocking must complete before parallel starts"
    )
    assert observed["blocking_finished_at_model_call"] is True, (
        "Blocking must complete before model is called"
    )
    assert observed["parallel_finished_at_model_call"] is False, (
        "Model called while parallel guardrail still running"
    )
    assert parallel_finished.is_set() is True, "Parallel guardrail should have completed"
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
        await asyncio.sleep(MEDIUM_DELAY)
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
        await asyncio.sleep(MEDIUM_DELAY)
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
        await asyncio.sleep(MEDIUM_DELAY)
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
        await asyncio.sleep(MEDIUM_DELAY)
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
        await asyncio.sleep(MEDIUM_DELAY)
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
        await asyncio.sleep(MEDIUM_DELAY)
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
        await asyncio.sleep(MEDIUM_DELAY)
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
        await asyncio.sleep(MEDIUM_DELAY)
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
    slow_guardrail_started = asyncio.Event()

    @input_guardrail(run_in_parallel=False)
    async def fast_guardrail_that_triggers(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal fast_guardrail_executed
        # Trip only once the sibling is provably in flight and therefore cancellable.
        await asyncio.wait_for(slow_guardrail_started.wait(), timeout=5)
        fast_guardrail_executed = True
        return GuardrailFunctionOutput(
            output_info="fast_triggered",
            tripwire_triggered=True,
        )

    @input_guardrail(run_in_parallel=False)
    async def slow_guardrail_that_should_be_cancelled(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal slow_guardrail_executed, slow_guardrail_cancelled
        slow_guardrail_started.set()
        try:
            # Never finishes on its own, so only cancellation can end this guardrail.
            await asyncio.Event().wait()
            slow_guardrail_executed = True
            return GuardrailFunctionOutput(
                output_info="slow_completed",
                tripwire_triggered=False,
            )
        except asyncio.CancelledError:
            slow_guardrail_cancelled = True
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
        await asyncio.wait_for(Runner.run(agent, "test input"), timeout=5)

    # Verify the fast guardrail executed
    assert fast_guardrail_executed is True, "Fast guardrail should have executed"

    # Verify the slow guardrail was cancelled, not completed
    assert slow_guardrail_cancelled is True, "Slow guardrail should have been cancelled"
    assert slow_guardrail_executed is False, "Slow guardrail should NOT have completed execution"

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
    slow_guardrail_started = asyncio.Event()

    @input_guardrail(run_in_parallel=False)
    async def fast_guardrail_that_triggers(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal fast_guardrail_executed
        # Trip only once the sibling is provably in flight and therefore cancellable.
        await asyncio.wait_for(slow_guardrail_started.wait(), timeout=5)
        fast_guardrail_executed = True
        return GuardrailFunctionOutput(
            output_info="fast_triggered",
            tripwire_triggered=True,
        )

    @input_guardrail(run_in_parallel=False)
    async def slow_guardrail_that_should_be_cancelled(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal slow_guardrail_executed, slow_guardrail_cancelled
        slow_guardrail_started.set()
        try:
            # Never finishes on its own, so only cancellation can end this guardrail.
            await asyncio.Event().wait()
            slow_guardrail_executed = True
            return GuardrailFunctionOutput(
                output_info="slow_completed",
                tripwire_triggered=False,
            )
        except asyncio.CancelledError:
            slow_guardrail_cancelled = True
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

    async def consume_stream() -> None:
        async for _event in result.stream_events():
            pass

    with pytest.raises(InputGuardrailTripwireTriggered):
        await asyncio.wait_for(consume_stream(), timeout=5)

    # Verify the fast guardrail executed
    assert fast_guardrail_executed is True, "Fast guardrail should have executed"

    # Verify the slow guardrail was cancelled, not completed
    assert slow_guardrail_cancelled is True, "Slow guardrail should have been cancelled"
    assert slow_guardrail_executed is False, "Slow guardrail should NOT have completed execution"

    # Verify agent never started
    assert model.first_turn_args is None, (
        "Model should not have been called when guardrail triggered"
    )


@pytest.mark.asyncio
async def test_streaming_input_guardrail_exception_awaits_cancelled_siblings():
    slow_started = asyncio.Event()
    slow_cleanup_finished = False

    async def slow_guardrail(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        nonlocal slow_cleanup_finished
        slow_started.set()
        try:
            await asyncio.sleep(LONG_DELAY)
            return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)
        except asyncio.CancelledError:
            await asyncio.sleep(SHORT_DELAY)
            slow_cleanup_finished = True
            raise

    async def raising_guardrail(
        ctx: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        await slow_started.wait()
        raise RuntimeError("guardrail failed")

    agent = Agent(name="test_agent", model=FakeModel())
    context = RunContextWrapper(context=None)
    streamed_result = RunResultStreaming(
        "test input",
        [],
        [],
        None,
        [],
        [],
        [],
        [],
        context,
        agent,
        0,
        None,
        None,
        None,
    )

    with pytest.raises(RuntimeError, match="guardrail failed"):
        await run_input_guardrails_with_queue(
            agent=agent,
            guardrails=[
                InputGuardrail(guardrail_function=slow_guardrail),
                InputGuardrail(guardrail_function=raising_guardrail),
            ],
            input="test input",
            context=context,
            streamed_result=streamed_result,
            parent_span=None,
        )

    assert slow_cleanup_finished is True


@pytest.mark.asyncio
async def test_input_guardrail_raise_cancels_siblings():
    """When one input guardrail raises a non-tripwire exception, sibling tasks
    must be cancelled and awaited so they don't keep running past the function's return."""
    from agents.run_internal.guardrails import run_input_guardrails

    sibling_started = asyncio.Event()
    sibling_completed = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    async def slow_sibling_started_first(ctx, agent, input):
        sibling_started.set()
        try:
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            sibling_cancelled.set()
            raise
        sibling_completed.set()
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    async def raise_after_sibling_starts(ctx, agent, input):
        await sibling_started.wait()
        raise RuntimeError("boom")

    g_slow = InputGuardrail(guardrail_function=slow_sibling_started_first)
    g_raise = InputGuardrail(guardrail_function=raise_after_sibling_starts)

    with pytest.raises(RuntimeError, match="boom"):
        await run_input_guardrails(
            Agent(name="t"), [g_slow, g_raise], "x", RunContextWrapper(context=None)
        )

    # By the time run_input_guardrails returns (via raise), the sibling must already
    # have been cancelled and awaited. No additional sleep should be needed.
    assert sibling_cancelled.is_set(), "Sibling task should have been cancelled"
    assert not sibling_completed.is_set(), "Sibling task should not have completed"


@pytest.mark.asyncio
async def test_output_guardrail_raise_cancels_siblings():
    """When one output guardrail raises a non-tripwire exception, sibling tasks
    must be cancelled and awaited so they don't keep running past the function's return."""
    from agents.run_internal.guardrails import run_output_guardrails

    sibling_started = asyncio.Event()
    sibling_completed = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    async def slow_sibling_started_first(ctx, agent, agent_output):
        sibling_started.set()
        try:
            await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            sibling_cancelled.set()
            raise
        sibling_completed.set()
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    async def raise_after_sibling_starts(ctx, agent, agent_output):
        await sibling_started.wait()
        raise RuntimeError("boom")

    g_slow = OutputGuardrail(guardrail_function=slow_sibling_started_first)
    g_raise = OutputGuardrail(guardrail_function=raise_after_sibling_starts)

    with pytest.raises(RuntimeError, match="boom"):
        await run_output_guardrails(
            [g_slow, g_raise], Agent(name="t"), "out", RunContextWrapper(context=None)
        )

    assert sibling_cancelled.is_set(), "Sibling task should have been cancelled"
    assert not sibling_completed.is_set(), "Sibling task should not have completed"


def _ordered_input_guardrails(
    *, second_triggers: bool, second_raises: bool = False, run_in_parallel: bool = False
) -> list[InputGuardrail[Any]]:
    """Build two guardrails whose completion order is fixed by an explicit barrier."""
    first_done = asyncio.Event()

    async def first_fn(
        context: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        first_done.set()
        return GuardrailFunctionOutput(output_info="passes", tripwire_triggered=False)

    async def second_fn(
        context: RunContextWrapper[Any], agent: Agent[Any], input: str | list[TResponseInputItem]
    ) -> GuardrailFunctionOutput:
        await first_done.wait()
        if second_raises:
            raise RuntimeError("guardrail exploded")
        return GuardrailFunctionOutput(output_info="second", tripwire_triggered=second_triggers)

    return [
        InputGuardrail(guardrail_function=first_fn, name="passes", run_in_parallel=run_in_parallel),
        InputGuardrail(
            guardrail_function=second_fn,
            name="raises" if second_raises else "trips",
            run_in_parallel=run_in_parallel,
        ),
    ]


def _tripwire_agent(model: FakeModel, *, run_in_parallel: bool) -> Agent[Any]:
    return Agent(
        name="guardrail_results_agent",
        model=model,
        input_guardrails=_ordered_input_guardrails(
            second_triggers=True, run_in_parallel=run_in_parallel
        ),
    )


def _result_names(results: list[Any]) -> list[str]:
    return [result.guardrail.get_name() for result in results]


@pytest.mark.asyncio
@pytest.mark.parametrize("run_in_parallel", [False, True])
async def test_input_guardrail_tripwire_reports_results(run_in_parallel: bool):
    """Runner.run() reports every completed guardrail result on the raised tripwire."""
    model = FakeModel()
    model.set_next_output([get_text_message("hello")])

    with pytest.raises(InputGuardrailTripwireTriggered) as exc_info:
        await Runner.run(_tripwire_agent(model, run_in_parallel=run_in_parallel), "test input")

    run_data = exc_info.value.run_data
    assert run_data is not None
    assert _result_names(run_data.input_guardrail_results) == ["passes", "trips"]
    assert exc_info.value.guardrail_result.guardrail.get_name() == "trips"


@pytest.mark.asyncio
@pytest.mark.parametrize("run_in_parallel", [False, True])
async def test_input_guardrail_tripwire_reports_results_streamed(run_in_parallel: bool):
    """The streamed path reports the same results, including on the streamed result object."""
    model = FakeModel()
    model.set_next_output([get_text_message("hello")])

    result = Runner.run_streamed(
        _tripwire_agent(model, run_in_parallel=run_in_parallel), "test input"
    )
    with pytest.raises(InputGuardrailTripwireTriggered) as exc_info:
        async for _ in result.stream_events():
            pass

    run_data = exc_info.value.run_data
    assert run_data is not None
    assert _result_names(run_data.input_guardrail_results) == ["passes", "trips"]
    assert _result_names(result.input_guardrail_results) == ["passes", "trips"]


def test_input_guardrail_tripwire_reports_results_sync():
    """Runner.run_sync() matches the async entry points."""
    model = FakeModel()
    model.set_next_output([get_text_message("hello")])

    with pytest.raises(InputGuardrailTripwireTriggered) as exc_info:
        Runner.run_sync(_tripwire_agent(model, run_in_parallel=False), "test input")

    run_data = exc_info.value.run_data
    assert run_data is not None
    assert _result_names(run_data.input_guardrail_results) == ["passes", "trips"]


@pytest.mark.asyncio
async def test_input_guardrail_results_reported_on_success():
    """Passing guardrails still land on the successful result exactly once."""
    model = FakeModel()
    model.set_next_output([get_text_message("hello")])
    agent = Agent(
        name="guardrail_results_agent",
        model=model,
        input_guardrails=[
            InputGuardrail(
                guardrail_function=get_sync_guardrail(triggers=False),
                name="blocking",
                run_in_parallel=False,
            ),
            InputGuardrail(
                guardrail_function=get_sync_guardrail(triggers=False),
                name="parallel",
                run_in_parallel=True,
            ),
        ],
    )

    result = await Runner.run(agent, "test input")

    assert _result_names(result.input_guardrail_results) == ["blocking", "parallel"]


@pytest.mark.asyncio
async def test_input_guardrail_exception_reports_completed_results():
    """A guardrail raising a non-tripwire error still preserves earlier results."""

    collected: list[Any] = []
    with pytest.raises(RuntimeError, match="guardrail exploded"):
        await run_input_guardrails(
            Agent(name="t"),
            _ordered_input_guardrails(second_triggers=False, second_raises=True),
            "test input",
            RunContextWrapper(context=None),
            collected,
        )

    assert _result_names(collected) == ["passes"]


@pytest.mark.asyncio
@pytest.mark.parametrize("run_in_parallel", [False, True])
async def test_input_guardrail_exception_reports_completed_results_streamed(
    run_in_parallel: bool,
):
    """A streamed guardrail raising a non-tripwire error still reports earlier results."""
    model = FakeModel()
    model.set_next_output([get_text_message("hello")])
    agent = Agent(
        name="guardrail_results_agent",
        model=model,
        input_guardrails=_ordered_input_guardrails(
            second_triggers=False,
            second_raises=True,
            run_in_parallel=run_in_parallel,
        ),
    )

    result = Runner.run_streamed(agent, "test input")
    with pytest.raises(RuntimeError, match="guardrail exploded"):
        async for _ in result.stream_events():
            pass

    assert _result_names(result.input_guardrail_results) == ["passes"]


def _ordered_output_guardrails(
    *, second_triggers: bool, second_raises: bool = False
) -> list[OutputGuardrail[Any]]:
    """Build two output guardrails whose completion order is fixed by an explicit barrier."""
    first_done = asyncio.Event()

    async def first_fn(
        context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any
    ) -> GuardrailFunctionOutput:
        first_done.set()
        return GuardrailFunctionOutput(output_info="passes", tripwire_triggered=False)

    async def second_fn(
        context: RunContextWrapper[Any], agent: Agent[Any], agent_output: Any
    ) -> GuardrailFunctionOutput:
        await first_done.wait()
        if second_raises:
            raise RuntimeError("guardrail exploded")
        return GuardrailFunctionOutput(output_info="second", tripwire_triggered=second_triggers)

    return [
        OutputGuardrail(guardrail_function=first_fn, name="passes"),
        OutputGuardrail(guardrail_function=second_fn, name="raises" if second_raises else "trips"),
    ]


def _output_tripwire_agent(model: FakeModel) -> Agent[Any]:
    return Agent(
        name="output_guardrail_results_agent",
        model=model,
        output_guardrails=_ordered_output_guardrails(second_triggers=True),
    )


@pytest.mark.asyncio
async def test_output_guardrail_tripwire_reports_results():
    """Runner.run() reports every completed output guardrail result on the raised tripwire."""
    model = FakeModel()
    model.set_next_output([get_text_message("hello")])

    with pytest.raises(OutputGuardrailTripwireTriggered) as exc_info:
        await Runner.run(_output_tripwire_agent(model), "test input")

    run_data = exc_info.value.run_data
    assert run_data is not None
    assert _result_names(run_data.output_guardrail_results) == ["passes", "trips"]
    assert exc_info.value.guardrail_result.guardrail.get_name() == "trips"


@pytest.mark.asyncio
async def test_output_guardrail_tripwire_reports_results_streamed():
    """The streamed path reports the same results, including on the streamed result object."""
    model = FakeModel()
    model.set_next_output([get_text_message("hello")])

    result = Runner.run_streamed(_output_tripwire_agent(model), "test input")
    with pytest.raises(OutputGuardrailTripwireTriggered) as exc_info:
        async for _ in result.stream_events():
            pass

    run_data = exc_info.value.run_data
    assert run_data is not None
    assert _result_names(run_data.output_guardrail_results) == ["passes", "trips"]
    assert _result_names(result.output_guardrail_results) == ["passes", "trips"]


def test_output_guardrail_tripwire_reports_results_sync():
    """Runner.run_sync() matches the async entry points."""
    model = FakeModel()
    model.set_next_output([get_text_message("hello")])

    with pytest.raises(OutputGuardrailTripwireTriggered) as exc_info:
        Runner.run_sync(_output_tripwire_agent(model), "test input")

    run_data = exc_info.value.run_data
    assert run_data is not None
    assert _result_names(run_data.output_guardrail_results) == ["passes", "trips"]


@pytest.mark.asyncio
async def test_output_guardrail_results_reported_on_success():
    """Passing output guardrails still land on the successful result exactly once."""
    model = FakeModel()
    model.set_next_output([get_text_message("hello")])
    agent = Agent(
        name="output_guardrail_results_agent",
        model=model,
        output_guardrails=_ordered_output_guardrails(second_triggers=False),
    )

    result = await Runner.run(agent, "test input")

    assert _result_names(result.output_guardrail_results) == ["passes", "trips"]


@pytest.mark.asyncio
async def test_output_guardrail_exception_reports_completed_results():
    """A guardrail raising a non-tripwire error still preserves earlier results."""
    from agents.run_internal.guardrails import run_output_guardrails

    collected: list[Any] = []
    with pytest.raises(RuntimeError, match="guardrail exploded"):
        await run_output_guardrails(
            _ordered_output_guardrails(second_triggers=False, second_raises=True),
            Agent(name="t"),
            "out",
            RunContextWrapper(context=None),
            collected,
        )

    assert _result_names(collected) == ["passes"]


@pytest.mark.asyncio
async def test_output_guardrail_exception_reports_completed_results_streamed():
    """A streamed output guardrail raising a non-tripwire error still reports earlier results."""
    model = FakeModel()
    model.set_next_output([get_text_message("hello")])
    agent = Agent(
        name="output_guardrail_results_agent",
        model=model,
        output_guardrails=_ordered_output_guardrails(second_triggers=False, second_raises=True),
    )

    result = Runner.run_streamed(agent, "test input")
    with pytest.raises(RuntimeError, match="guardrail exploded"):
        async for _ in result.stream_events():
            pass

    assert _result_names(result.output_guardrail_results) == ["passes"]
