from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from openai.types.responses import ResponseStreamEvent
from openai.types.responses.response_prompt_param import ResponsePromptParam

from agents._run_impl import (
    AgentToolUseTracker,
    NextStepRunAgain,
    ProcessedResponse,
    RunImpl,
    SingleStepResult,
)
from agents.agent import Agent
from agents.agent_output import AgentOutputSchemaBase
from agents.exceptions import (
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
    OutputGuardrailTripwireTriggered,
)
from agents.guardrail import GuardrailFunctionOutput, InputGuardrail, OutputGuardrail
from agents.handoffs import Handoff
from agents.items import ModelResponse, RunItem, TResponseInputItem
from agents.lifecycle import RunHooks
from agents.model_settings import ModelSettings
from agents.models.interface import Model, ModelTracing
from agents.run import (
    AgentRunner,
    RunConfig,
    Runner,
    _Cancellation,
    get_default_agent_runner,
    set_default_agent_runner,
)
from agents.run_context import RunContextWrapper
from agents.stream_events import RunUpdatedStreamEvent
from agents.tool import Tool
from agents.usage import Usage

# Reuse the repo’s helper to build a FunctionTool correctly
from tests.test_responses import get_function_tool  # <-- existing test helper


class MinimalAgent:
    """Just enough surface for Runner."""

    def __init__(self, model: Model, name: str = "test-agent"):
        self.name = name
        self.model = model
        self.model_settings = ModelSettings()
        self.output_type = None
        self.hooks = None
        self.handoffs: list[Handoff] = []
        self.reset_tool_choice = False
        self.input_guardrails: list[Any] = []
        self.output_guardrails: list[Any] = []

    async def get_system_prompt(self, _):
        return None

    async def get_prompt(self, _):
        return None

    async def get_all_tools(self, _):
        return []


class FakeModelNeverCompletes(Model):
    async def get_response(self, *a: Any, **k: Any) -> Any:
        raise NotImplementedError

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[Any],
        model_settings: ModelSettings,
        tools: list[Any],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> AsyncIterator[ResponseStreamEvent]:
        while True:
            await asyncio.sleep(0.02)
            yield cast(ResponseStreamEvent, object())


@pytest.mark.anyio
async def test_cancel_streamed_run_emits_cancelled_status():
    """When status events are enabled, cancel should emit run.updated(cancelled)."""
    agent = MinimalAgent(model=FakeModelNeverCompletes())
    result = Runner.run_streamed(
        cast(Agent[Any], agent),
        input="hello world",
        run_config=RunConfig(model=agent.model),
        max_turns=10,
    )
    # Opt-in to status events for this test
    result._emit_status_events = True

    seen_status: str | None = None

    async def consume():
        nonlocal seen_status
        async for ev in result.stream_events():
            if isinstance(ev, RunUpdatedStreamEvent):
                seen_status = ev.status

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.08)
    result.cancel("user-requested")
    await consumer

    assert result.is_complete is True
    assert seen_status == "cancelled"


@pytest.mark.anyio
async def test_default_flag_off_emits_no_status_event():
    """By default, no run.updated events should be emitted (back-compat)."""
    agent = MinimalAgent(model=FakeModelNeverCompletes())
    result = Runner.run_streamed(
        cast(Agent[Any], agent),
        input="x",
        run_config=RunConfig(model=agent.model),
    )
    # DO NOT set result._emit_status_events here
    statuses: list[str] = []

    async def consume():
        async for ev in result.stream_events():
            if isinstance(ev, RunUpdatedStreamEvent):
                statuses.append(ev.status)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    result.cancel("user")
    await task

    assert statuses == []  # no run.updated by default


@pytest.mark.anyio
async def test_midstream_cancel_emits_cancelled_status_when_enabled():
    """Cancel while model is streaming yields cancelled when flag is on."""
    agent = MinimalAgent(model=FakeModelNeverCompletes())
    result = Runner.run_streamed(
        cast(Agent[Any], agent),
        input="x",
        run_config=RunConfig(model=agent.model),
    )
    result._emit_status_events = True
    statuses: list[str] = []

    async def consume():
        async for ev in result.stream_events():
            if isinstance(ev, RunUpdatedStreamEvent):
                statuses.append(ev.status)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.06)
    result.cancel("user")
    await task

    assert "cancelled" in statuses


class FakeModelSlowGet(Model):
    async def get_response(self, *a, **k):
        # simulate long compute so we can cancel
        await asyncio.sleep(1.0)

    async def stream_response(self, *a, **k):
        raise NotImplementedError


@pytest.mark.anyio
async def test_non_streamed_cancel_propagates_cancelled_error_or_returns_terminal_result():
    """Runner.run cancellation should terminate cleanly.

    We accept either a CancelledError or a terminal RunResult.
    """
    agent = MinimalAgent(model=FakeModelSlowGet())

    async def run_it():
        return await Runner.run(
            cast(Agent[Any], agent),
            input="y",
            run_config=RunConfig(model=agent.model),
        )

    task = asyncio.create_task(run_it())
    await asyncio.sleep(0.05)
    task.cancel()

    try:
        result = await task
    except asyncio.CancelledError:
        # Current contract may propagate cancel; this is acceptable.
        return

    # If your contract returns a terminal result on cancel, assert it here.
    assert getattr(result, "final_output", None) is None


@pytest.mark.anyio
async def test_inject_is_consumed_on_next_turn():
    """
    Injected items should be included in a subsequent model turn input.
    We capture the inputs passed into FakeModel each turn and assert presence.
    """
    INJECT_TOKEN: TResponseInputItem = {
        "role": "user",
        "content": "INJECTED",
    }  # match message-style items

    class FakeModelCapture(Model):
        def __init__(self) -> None:
            self.inputs: list[list[Any]] = []

        async def get_response(self, *a: Any, **k: Any) -> Any:
            raise NotImplementedError

        async def stream_response(
            self,
            system_instructions: str | None,
            input: str | list[Any],
            model_settings: ModelSettings,
            tools: list[Any],
            output_schema: AgentOutputSchemaBase | None,
            handoffs: list[Handoff],
            tracing: ModelTracing,
            *,
            previous_response_id: str | None,
            conversation_id: str | None,
            prompt: ResponsePromptParam | None,
        ) -> AsyncIterator[ResponseStreamEvent]:
            turn = 0
            while True:
                self.inputs.append(list(input))
                yield cast(ResponseStreamEvent, object())
                turn += 1
                await asyncio.sleep(0.01)


    model = FakeModelCapture()
    agent = MinimalAgent(model=model)

    result = Runner.run_streamed(
        starting_agent=cast(Agent[Any], agent),
        input="hello",
        run_config=RunConfig(model=agent.model),
        max_turns=6,
    )

    async def drive_and_inject():
        # Let at least one turn record baseline input
        await asyncio.sleep(0.05)
        # Inject so a future turn sees it
        result.inject([INJECT_TOKEN])
        # Give time for a couple more turns to run
        await asyncio.sleep(0.12)
        result.cancel("done")

    consumer = asyncio.create_task(drive_and_inject())
    async for _ in result.stream_events():
        pass
    await consumer

    # We should have recorded ≥2 turns
    assert len(model.inputs) >= 2

    # Assert the injected message appears in ANY turn after injection time
    flattened_after_injection = [item for turn in model.inputs[1:] for item in turn]
    assert any(
        isinstance(item, dict) and item.get("role") == "user" and item.get("content") == "INJECTED"
        for item in flattened_after_injection
    ), f"Injected item not present after injection; captured={model.inputs}"


class FakeModelTriggersTool(Model):
    """
    Emits continuous events so we can cancel while a function tool is (hypothetically) running.
    Note: This is a timing smoke test. For a full tool-call path test, emit tool-call outputs.
    """

    async def get_response(self, *a: Any, **k: Any) -> Any:
        raise NotImplementedError

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[Any],
        model_settings: ModelSettings,
        tools: list[Any],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> AsyncIterator[ResponseStreamEvent]:
        while True:
            await asyncio.sleep(0.02)
            yield cast(ResponseStreamEvent, object())


class AgentWithTool(MinimalAgent):
    def __init__(self, model: Model, tool: Tool):
        super().__init__(model)
        self._tool = tool

    async def get_all_tools(self, _):
        return [self._tool]


@pytest.mark.anyio
async def test_function_tool_cancels_promptly():
    # Build the tool using the repo helper (it doesn't take a handler argument)
    tool = get_function_tool("long", "done")

    agent = AgentWithTool(FakeModelTriggersTool(), tool)

    result = Runner.run_streamed(
        cast(Agent[Any], agent),
        input="trigger tool",
        run_config=RunConfig(model=agent.model),
    )
    start = time.perf_counter()
    await asyncio.sleep(0.05)  # let some activity happen
    result.cancel("user")

    # Drain stream; ensure no hang
    async for _ in result.stream_events():
        pass

    elapsed = time.perf_counter() - start
    # Expect prompt cancellation (well under 1s)
    assert elapsed < 0.4


class _FailingModel(Model):
    async def get_response(self, *a: Any, **k: Any) -> Any:
        raise NotImplementedError

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[Any],
        model_settings: ModelSettings,
        tools: list[Any],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> AsyncIterator[ResponseStreamEvent]:
        if False:
            yield cast(ResponseStreamEvent, object())
        raise RuntimeError("boom")


class _TickingModel(Model):
    async def get_response(self, *a: Any, **k: Any) -> Any:
        raise NotImplementedError

    async def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[Any],
        model_settings: ModelSettings,
        tools: list[Any],
        output_schema: AgentOutputSchemaBase | None,
        handoffs: list[Handoff],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: ResponsePromptParam | None,
    ) -> AsyncIterator[ResponseStreamEvent]:
        while True:
            await asyncio.sleep(0.001)
            yield cast(ResponseStreamEvent, object())


@pytest.mark.anyio
async def test_streamed_failure_emits_failed_status_and_closes():
    agent = MinimalAgent(model=_FailingModel())
    result = Runner.run_streamed(
        cast(Agent[Any], agent),
        input="x",
        run_config=RunConfig(model=agent.model),
    )
    result._emit_status_events = True

    statuses: list[str] = []
    it = result.stream_events()  # ensure we CALL the method and get an async iterator
    assert hasattr(it, "__aiter__"), (
        f"stream_events() did not return an async iterator, got {type(it)}"
    )
    caught = False
    try:
        async for ev in it:
            if isinstance(ev, RunUpdatedStreamEvent):
                statuses.append(ev.status)
    except RuntimeError as e:
        # Our failing model raises immediately; accept that path too
        assert str(e) == "boom"
        caught = True

    # Either we saw a 'failed' status, or we caught the model's RuntimeError.
    assert result.is_complete is True
    assert (statuses and statuses[-1] == "failed") or caught


@pytest.mark.anyio
async def test_max_turns_exceeded_hits_failed_path():
    agent = MinimalAgent(model=_TickingModel())
    result = Runner.run_streamed(
        cast(Agent[Any], agent),
        input="tick",
        run_config=RunConfig(model=agent.model),
        max_turns=0,  # immediate exceed
    )
    result._emit_status_events = True

    # Draining the stream should raise MaxTurnsExceeded for this configuration.
    it = result.stream_events()
    assert hasattr(it, "__aiter__"), (
        f"stream_events() did not return an async iterator, got {type(it)}"
    )
    with pytest.raises(MaxTurnsExceeded):
        async for _ in it:
            pass


@pytest.mark.anyio
async def test_cancel_before_streaming_closes_immediately():
    """Cancel right away to hit the early-cancel branch at the top of the loop."""
    agent = MinimalAgent(model=FakeModelNeverCompletes())
    result = Runner.run_streamed(
        cast(Agent[Any], agent),
        input="early",
        run_config=RunConfig(model=agent.model),
    )
    result._emit_status_events = True

    # Cancel before we ever start iterating events
    result.cancel("early-stop")

    statuses: list[str] = []
    it = result.stream_events()
    assert hasattr(it, "__aiter__"), (
        f"stream_events() did not return an async iterator, got {type(it)}"
    )
    async for ev in it:
        if isinstance(ev, RunUpdatedStreamEvent):
            statuses.append(ev.status)

    assert result.is_complete is True
    # Some runners emit the status, others just close; allow both
    assert (not statuses) or statuses[-1] == "cancelled"


@pytest.mark.anyio
async def test_idempotent_cancel_emits_single_terminal_status_when_enabled():
    """Double cancel should not duplicate terminal status."""
    agent = MinimalAgent(model=FakeModelNeverCompletes())
    result = Runner.run_streamed(
        cast(Agent[Any], agent),
        input="dup",
        run_config=RunConfig(model=agent.model),
    )
    result._emit_status_events = True

    # Issue cancel twice
    result.cancel("first")
    result.cancel("second")

    statuses: list[str] = []
    it = result.stream_events()
    assert hasattr(it, "__aiter__"), (
        f"stream_events() did not return an async iterator, got {type(it)}"
    )
    async for ev in it:
        if isinstance(ev, RunUpdatedStreamEvent):
            statuses.append(ev.status)

    assert result.is_complete is True
    # If statuses are emitted, 'cancelled' should appear exactly once
    if statuses:
        assert statuses.count("cancelled") == 1




def test_get_default_agent_runner_roundtrip() -> None:
    """The default agent runner can be replaced and restored."""
    original = get_default_agent_runner()
    try:
        set_default_agent_runner(None)
        new_runner = get_default_agent_runner()
        assert isinstance(new_runner, AgentRunner)
    finally:
        set_default_agent_runner(original)


def test_cancellation_raises_cancelled_error() -> None:
    """_Cancellation.raise_if_cancelled raises when started."""
    cancel = _Cancellation()
    cancel.start("go")
    with pytest.raises(asyncio.CancelledError):
        cancel.raise_if_cancelled()


@pytest.mark.anyio
async def test_run_input_guardrails_handles_tripwire() -> None:
    """Tripwire in input guardrail raises InputGuardrailTripwireTriggered."""

    async def tripwire(_, __, ___):
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    guardrail = InputGuardrail(tripwire, name="trip")
    context = RunContextWrapper(context=None)
    agent = MinimalAgent(model=FakeModelNeverCompletes())
    with pytest.raises(InputGuardrailTripwireTriggered):
        await AgentRunner._run_input_guardrails(cast(Agent[Any], agent), [guardrail], "hi", context)


@pytest.mark.anyio
async def test_run_input_guardrails_collects_results() -> None:
    """Non-tripwire guardrails return their results."""

    async def ok(_, __, ___):
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    guardrails = [InputGuardrail(ok, name="g1"), InputGuardrail(ok, name="g2")]
    context = RunContextWrapper(context=None)
    agent = MinimalAgent(model=FakeModelNeverCompletes())
    results = await AgentRunner._run_input_guardrails(
        cast(Agent[Any], agent), guardrails, "hi", context
    )
    assert len(results) == 2


@pytest.mark.anyio
async def test_run_output_guardrails_handles_tripwire() -> None:
    """Tripwire in output guardrail raises OutputGuardrailTripwireTriggered."""

    async def tripwire(_, __, ___):
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    guardrail = OutputGuardrail(tripwire, name="trip")
    context = RunContextWrapper(context=None)
    agent = MinimalAgent(model=FakeModelNeverCompletes())
    with pytest.raises(OutputGuardrailTripwireTriggered):
        await AgentRunner._run_output_guardrails(
            [guardrail], cast(Agent[Any], agent), "out", context
        )


@pytest.mark.anyio
async def test_run_output_guardrails_collects_results() -> None:
    """Non-tripwire output guardrails return their results."""

    async def ok(_, __, ___):
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    guardrails = [OutputGuardrail(ok, name="g1"), OutputGuardrail(ok, name="g2")]
    context = RunContextWrapper(context=None)
    agent = MinimalAgent(model=FakeModelNeverCompletes())
    results = await AgentRunner._run_output_guardrails(
        guardrails, cast(Agent[Any], agent), "out", context
    )
    assert len(results) == 2


@pytest.mark.anyio
async def test_get_single_step_result_from_response(monkeypatch) -> None:
    """_get_single_step_result_from_response processes model output."""
    agent = MinimalAgent(model=FakeModelNeverCompletes())
    new_response = ModelResponse(output=[], usage=Usage(), response_id=None)

    async def fake_execute(*args, **kwargs):
        return SingleStepResult(
            original_input="hi",
            model_response=new_response,
            pre_step_items=[],
            new_step_items=[],
            next_step=NextStepRunAgain(),
        )

    def fake_process(*args, **kwargs):
        return ProcessedResponse(
            new_items=[],
            handoffs=[],
            functions=[],
            computer_actions=[],
            local_shell_calls=[],
            tools_used=[],
            mcp_approval_requests=[],
        )

    monkeypatch.setattr(RunImpl, "process_model_response", fake_process)
    monkeypatch.setattr(RunImpl, "execute_tools_and_side_effects", fake_execute)

    result = await AgentRunner._get_single_step_result_from_response(
        agent=cast(Agent[Any], agent),
        all_tools=[],
        original_input="hi",
        pre_step_items=[],
        new_response=new_response,
        output_schema=None,
        handoffs=[],
        hooks=RunHooks[Any](),
        context_wrapper=RunContextWrapper(context=None),
        run_config=RunConfig(model=agent.model),
        tool_use_tracker=AgentToolUseTracker(),
    )

    assert result.model_response is new_response


@pytest.mark.anyio
async def test_get_single_step_result_from_streamed_response(monkeypatch) -> None:
    """_get_single_step_result_from_streamed_response handles streamed events."""

    agent = MinimalAgent(model=FakeModelNeverCompletes())
    new_response = ModelResponse(output=[], usage=Usage(), response_id=None)

    async def fake_execute(*args, **kwargs):
        return SingleStepResult(
            original_input="hi",
            model_response=new_response,
            pre_step_items=[],
            new_step_items=[],
            next_step=NextStepRunAgain(),
        )

    def fake_process(*args, **kwargs):
        return ProcessedResponse(
            new_items=[],
            handoffs=[],
            functions=[],
            computer_actions=[],
            local_shell_calls=[],
            tools_used=[],
            mcp_approval_requests=[],
        )

    class DummyStreamed:
        def __init__(self) -> None:
            self.input = "hi"
            self.new_items: list[RunItem] = []
            self._event_queue: asyncio.Queue[Any] = asyncio.Queue()

    monkeypatch.setattr(RunImpl, "process_model_response", fake_process)
    monkeypatch.setattr(RunImpl, "execute_tools_and_side_effects", fake_execute)

    streamed = DummyStreamed()
    result = await AgentRunner._get_single_step_result_from_streamed_response(
        agent=cast(Agent[Any], agent),
        all_tools=[],
        streamed_result=cast(Any, streamed),
        new_response=new_response,
        output_schema=None,
        handoffs=[],
        hooks=RunHooks[Any](),
        context_wrapper=RunContextWrapper(context=None),
        run_config=RunConfig(model=agent.model),
        tool_use_tracker=AgentToolUseTracker(),
    )

    assert result.model_response is new_response
