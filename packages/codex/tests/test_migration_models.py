"""REQ68 model selection and stream admission through the real native loop."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextvars import ContextVar
from typing import Any, Literal

import pytest
from openai.types.responses import (
    ResponseCompletedEvent,
    ResponseOutputItemDoneEvent,
    ResponseTextDeltaEvent,
)

from agents.classic import Agent, Model, ModelProvider, ModelSettings, function_tool
from agents.classic.exceptions import ModelBehaviorError, UserError
from agents.classic.items import TResponseStreamEvent
from agents.classic.models.interface import ModelTracing
from agents.classic.testing import ModelStep, ScriptedModel, assistant_message, function_call
from agents.classic.usage import Usage
from agents.migration import SDKRunner
from tests._asyncio import timeout
from tests.native_fixtures import responses as responses


@pytest.fixture(autouse=True)
def local_transport_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")


class BorrowedModel(ScriptedModel):
    closed = False

    async def close(self) -> None:
        self.closed = True

    async def _cleanup_on_run_end(self, owner: object) -> None:
        raise AssertionError("The runner must borrow the model")


class SelectedProvider(ModelProvider):
    def __init__(self, model: Model) -> None:
        self.model = model
        self.names: list[str | None] = []
        self.closed = False

    def get_model(self, model_name: str | None) -> Model:
        self.names.append(model_name)
        return self.model

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("invocation", ["response", "stream"])
@pytest.mark.parametrize("buffered", [False, True])
async def test_selected_provider_overrides_tools_ids_and_borrowing(
    invocation: Literal["response", "stream"], buffered: bool
) -> None:
    effects: list[str] = []

    @function_tool
    def greet(name: str) -> str:
        effects.append(name)
        return f"Hello {name}"

    usage = Usage(requests=2, input_tokens=11, output_tokens=3, total_tokens=14)
    selected = BorrowedModel(
        [
            ModelStep(
                output=[function_call("greet", {"name": "Ada"}, call_id="original-call")],
                response_id="original-tool-response",
                request_id="original-request",
                raw_usage={"provider_units": 19},
                usage=usage,
            ),
            ModelStep(output=[assistant_message("Hello Ada")], response_id="original-answer"),
        ]
    )
    unused = ScriptedModel([[assistant_message("wrong model")]])
    provider = SelectedProvider(selected)
    agent_settings = ModelSettings(temperature=0.8, max_tokens=37)
    override = ModelSettings(temperature=0.2, preserve_raw_usage=True)
    agent = Agent("Greeter", model=unused, model_settings=agent_settings, tools=[greet])
    events: list[TResponseStreamEvent] = []
    kwargs: dict[str, Any] = {
        "model": "selected",
        "model_provider": provider,
        "model_settings": override,
        "model_invocation": invocation,
        "on_model_event": events.append if invocation == "stream" else None,
    }
    if buffered:
        running = SDKRunner.run_streamed(agent, "Greet Ada", **kwargs)
        result = await running.get_result()
    else:
        result = await SDKRunner.run(agent, "Greet Ada", **kwargs)
    assert result.final_output == "Hello Ada"
    assert effects == ["Ada"]
    assert provider.names == ["selected"]
    assert not provider.closed and not selected.closed
    assert unused.calls == ()
    selected.assert_complete()
    assert result.last_response_id == "original-answer"
    assert result.raw_responses[0].response_id == "original-tool-response"
    assert result.raw_responses[0].usage == usage
    assert result.raw_responses[0].request_id == "original-request"
    assert result.raw_responses[0].raw_usage == {"provider_units": 19}
    assert result.new_items[0].raw_item.call_id == "original-call"
    assert selected.calls[1].input[-1]["call_id"] == "original-call"
    assert "Hello Ada" in str(selected.calls[1].input[-1]["output"])
    for request in selected.calls:
        assert request.streamed == (invocation == "stream")
        assert request.model_settings.temperature == 0.2
        assert request.model_settings.max_tokens == 37
        assert request.tracing is ModelTracing.DISABLED
        assert request.handoffs == [] and request.output_schema is None
        assert request.previous_response_id is None and request.conversation_id is None
        assert request.prompt is None
    assert agent.model is unused and agent.model_settings.temperature == 0.8
    assert override.max_tokens is None
    assert bool(events) == (invocation == "stream")


@pytest.mark.parametrize(
    "options, error",
    [
        ({"model_invocation": "invalid"}, ValueError),
        ({"model_settings": "invalid"}, TypeError),
        ({"model_settings": ModelSettings(extra_body={})}, UserError),
        ({"model_settings": ModelSettings(tool_choice="required")}, UserError),
        ({"model_invocation": "stream", "on_model_event": 3}, TypeError),
        ({"on_model_event": lambda event: None}, ValueError),
    ],
)
async def test_invalid_options_fail_before_provider_lookup(
    options: dict[str, Any], error: type
) -> None:
    selected = BorrowedModel([[assistant_message("unused")]])
    provider = SelectedProvider(selected)
    with pytest.raises(error):
        await SDKRunner.run(
            Agent("Invalid", model="selected"), "hello", model_provider=provider, **options
        )
    assert provider.names == [] and selected.calls == () and not provider.closed


async def test_object_override_preserves_provider_rejection_and_borrows_model() -> None:
    selected = BorrowedModel([[assistant_message("chosen")]])
    provider = SelectedProvider(selected)
    agent = Agent("Override", model="unused-name")
    with pytest.raises(UserError, match="only to a model name"):
        await SDKRunner.run(agent, "hello", model=selected, model_provider=provider)
    assert provider.names == []
    result = await SDKRunner.run(agent, "hello", model=selected)
    assert result.final_output == "chosen" and not selected.closed


async def test_provider_error_never_falls_back_to_agent_model() -> None:
    unused = ScriptedModel([[assistant_message("wrong")]])

    class FailingProvider(SelectedProvider):
        def get_model(self, model_name: str | None) -> Model:
            self.names.append(model_name)
            raise RuntimeError("provider lookup failed")

    provider = FailingProvider(unused)
    with pytest.raises(RuntimeError, match="provider lookup failed"):
        await SDKRunner.run(
            Agent("No fallback", model=unused), "hello", model="selected", model_provider=provider
        )
    assert provider.names == ["selected"] and unused.calls == () and not provider.closed


async def test_async_observers_fail_before_provider_lookup() -> None:
    async def observe(event: TResponseStreamEvent) -> None:
        pass

    class AsyncObserver:
        async def __call__(self, event: TResponseStreamEvent) -> None:
            pass

    selected = ScriptedModel([[assistant_message("unused")]])
    provider = SelectedProvider(selected)
    for observer in (observe, AsyncObserver()):
        with pytest.raises(TypeError, match="synchronous"):
            await SDKRunner.run(
                Agent("Invalid observer", model="selected"),
                "hello",
                model_provider=provider,
                model_invocation="stream",
                on_model_event=observer,  # type: ignore[arg-type]
            )
    assert provider.names == [] and selected.calls == ()


async def terminal(*, tools: bool = False) -> ResponseCompletedEvent:
    output = (
        [function_call("effect", {}, call_id="never-run")] if tools else [assistant_message("done")]
    )
    source = ScriptedModel([output])
    completed: ResponseCompletedEvent | None = None
    async for event in source.stream_response(
        system_instructions=None,
        input="hello",
        model_settings=ModelSettings(),
        tools=[],
        output_schema=None,
        handoffs=[],
        tracing=ModelTracing.DISABLED,
        previous_response_id=None,
        conversation_id=None,
        prompt=None,
    ):
        if isinstance(event, ResponseCompletedEvent):
            completed = event
    assert completed is not None, "ScriptedModel must complete"
    return completed


def delta(text: str = "live") -> ResponseTextDeltaEvent:
    return ResponseTextDeltaEvent(
        type="response.output_text.delta",
        delta=text,
        item_id="message",
        output_index=0,
        content_index=0,
        sequence_number=0,
        logprobs=[],
    )


class GatedModel(ScriptedModel):
    def __init__(self, events: list[TResponseStreamEvent], *, hold: str = "none") -> None:
        super().__init__()
        self.events = events
        self.hold = hold
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.closing = asyncio.Event()
        self.closed = asyncio.Event()
        self.resource_closed = False

    async def close(self) -> None:
        self.resource_closed = True

    def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[TResponseStreamEvent]:
        owner = self

        class Stream:
            def __init__(self) -> None:
                self.events = iter(owner.events)

            def __aiter__(self) -> Stream:
                return self

            async def __anext__(self) -> TResponseStreamEvent:
                event = next(self.events, None)
                if event is not None:
                    return event
                owner.entered.set()
                if owner.hold == "next":
                    try:
                        await asyncio.Future()
                    except asyncio.CancelledError:
                        # A producer may suppress cancellation and yield a terminal.
                        await owner.release.wait()
                        return await terminal()
                raise StopAsyncIteration

            async def aclose(self) -> None:
                owner.closing.set()
                if owner.hold == "close":
                    await owner.release.wait()
                owner.closed.set()
                if owner.hold == "close_error":
                    raise RuntimeError("close failure")

        return Stream()


async def test_item_done_supplies_output_when_terminal_output_is_empty() -> None:
    effects: list[str] = []

    @function_tool
    def effect() -> str:
        effects.append("ran")
        return "ran"

    final = await terminal(tools=True)
    item = ResponseOutputItemDoneEvent(
        type="response.output_item.done",
        item=final.response.output[0],
        output_index=0,
        sequence_number=0,
    )
    final.response.output = []
    selected = ScriptedModel(
        [
            ModelStep.stream([item, final]),
            [assistant_message("effect complete")],
        ]
    )
    result = await SDKRunner.run(
        Agent("Items", model=selected, tools=[effect]), "hello", model_invocation="stream"
    )
    assert result.final_output == "effect complete" and effects == ["ran"]
    assert result.raw_responses[0].output[0].call_id == "never-run"
    assert selected.calls[1].input[-1]["call_id"] == "never-run"
    assert final.response.output == []
    selected.assert_complete()


async def test_live_delta_is_detached_and_precedes_terminal_and_buffered_result() -> None:
    final = await terminal()
    raw_delta = delta()
    observed: list[str] = []
    gate = asyncio.Event()

    async def produce(request: Any) -> AsyncIterator[TResponseStreamEvent]:
        yield raw_delta
        await gate.wait()
        yield final

    model = ScriptedModel([ModelStep.stream(produce)])

    def observe(event: TResponseStreamEvent) -> None:
        observed.append(event.type)
        if isinstance(event, ResponseTextDeltaEvent):
            event.delta = "observer edit"
        if isinstance(event, ResponseCompletedEvent):
            event.response.output.clear()

    running = SDKRunner.run_streamed(
        Agent("Live", model=model),
        "hello",
        model_invocation="stream",
        on_model_event=observe,
    )
    task = asyncio.create_task(running.get_result())
    semantic_events: list[Any] = []

    async def read_events() -> None:
        async for event in running.stream_events():
            semantic_events.append(event)

    reader = asyncio.create_task(read_events())
    try:
        async with timeout(15):
            while not observed:
                await asyncio.sleep(0.01)
        assert observed == ["response.output_text.delta"] and not task.done()
        assert semantic_events == [] and not running.is_complete
        assert raw_delta.delta == "live"
        gate.set()
        result = await asyncio.wait_for(task, 15)
        assert result.final_output == "done"
        await asyncio.wait_for(reader, 15)
        assert [event.type for event in semantic_events] == [
            "agent_updated_stream_event",
            "run_item_stream_event",
        ]
        assert final.response.output
    finally:
        gate.set()
        if not task.done():
            task.cancel()
        if not reader.done():
            reader.cancel()
        await asyncio.gather(task, reader, return_exceptions=True)


@pytest.mark.parametrize(
    "bad",
    [
        "missing",
        "duplicate",
        "after",
        "failed",
        "incomplete",
        "error",
        "status",
        "eof_error",
        "close_error",
        "unsupported",
        "oversized",
    ],
)
async def test_invalid_stream_never_dispatches_terminal_tools(bad: str) -> None:
    effects: list[str] = []

    @function_tool
    def effect() -> str:
        effects.append("ran")
        return "ran"

    final = await terminal(tools=True)
    events: list[Any] = [final]
    if bad == "missing":
        events = [delta()]
    elif bad == "duplicate":
        events.append(final)
    elif bad == "after":
        events.append(delta())
    elif bad in {"failed", "incomplete", "error"}:
        events = [
            final.model_copy(update={"type": "error" if bad == "error" else f"response.{bad}"})
        ]
    elif bad == "status":
        final.response.status = "incomplete"
    elif bad == "unsupported":
        final.response.output = [assistant_message("no")]
        final.response.output[0] = final.response.output[0].model_copy(
            update={"status": "in_progress"}
        )
    elif bad == "oversized":
        events = [delta("x" * (2 * 1024 * 1024)), delta("y" * (2 * 1024 * 1024)), final]

    class InvalidStream(GatedModel):
        async def stream_response(
            self, *args: Any, **kwargs: Any
        ) -> AsyncIterator[TResponseStreamEvent]:
            try:
                for event in self.events:
                    yield event
                if bad == "eof_error":
                    raise RuntimeError("EOF failure")
            finally:
                self.closed.set()

    model = (
        GatedModel(events, hold="close_error") if bad == "close_error" else InvalidStream(events)
    )
    with pytest.raises((ModelBehaviorError, UserError, RuntimeError)):
        await SDKRunner.run(
            Agent("Invalid", model=model, tools=[effect]), "hello", model_invocation="stream"
        )
    assert effects == [] and model.closed.is_set() and not model.resource_closed


@pytest.mark.parametrize("hold", ["next", "close"])
async def test_cancellation_joins_next_and_close_and_discards_late_events(hold: str) -> None:
    model = GatedModel([delta()] if hold == "next" else [await terminal()], hold=hold)
    observed: list[TResponseStreamEvent] = []
    task = asyncio.create_task(
        SDKRunner.run(
            Agent("Held", model=model),
            "hello",
            model_invocation="stream",
            on_model_event=observed.append,
        )
    )
    try:
        await asyncio.wait_for(model.entered.wait(), 15)
        task.cancel()
        if hold == "close":
            await asyncio.wait_for(model.closing.wait(), 15)
        await asyncio.sleep(0.05)
        task.cancel()
        assert not task.done() and not model.closed.is_set()
        observed_count = len(observed)
        model.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 15)
        assert (
            model.closed.is_set() and len(observed) == observed_count and not model.resource_closed
        )
    finally:
        model.release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_observer_failure_closes_stream_before_tool_dispatch() -> None:
    effects: list[str] = []

    @function_tool
    def effect() -> str:
        effects.append("ran")
        return "ran"

    model = GatedModel([await terminal(tools=True)])

    def fail(event: TResponseStreamEvent) -> None:
        raise RuntimeError("observer failed")

    with pytest.raises(RuntimeError, match="observer failed"):
        await SDKRunner.run(
            Agent("Observer", model=model, tools=[effect]),
            "hello",
            model_invocation="stream",
            on_model_event=fail,
        )
    assert effects == [] and model.closed.is_set() and not model.resource_closed


@pytest.mark.parametrize(
    "failure, fail_cleanup", [("observer", False), ("admission", False), ("observer", True)]
)
async def test_context_bound_factory_and_generator_cleanup_on_error(
    failure: str, fail_cleanup: bool
) -> None:
    marker = ContextVar("stream-error-context", default="outside")
    tasks: list[Any] = []
    released: list[str] = []
    effects: list[str] = []
    observer_error = RuntimeError("observer failed")
    cleanup_error = RuntimeError("cleanup failed")
    final = await terminal(tools=True)

    @function_tool
    def effect() -> str:
        effects.append("ran")
        return "ran"

    class ContextBoundModel(BorrowedModel):
        def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[TResponseStreamEvent]:
            tasks.append(asyncio.current_task())
            acquired = marker.set("factory")

            async def produce() -> AsyncIterator[TResponseStreamEvent]:
                tasks.append(asyncio.current_task())
                token = marker.set("iterating")
                try:
                    yield final
                    yield delta()  # Invalid after the terminal, before native effects.
                finally:
                    tasks.append(asyncio.current_task())
                    marker.reset(token)
                    marker.reset(acquired)
                    released.append(marker.get())
                    if fail_cleanup:
                        raise cleanup_error

            return produce()

    def observe(event: TResponseStreamEvent) -> None:
        assert marker.get() == "iterating"
        if failure == "observer":
            raise observer_error

    model = ContextBoundModel()
    expected = RuntimeError if failure == "observer" else ModelBehaviorError
    with pytest.raises(expected) as raised:
        await SDKRunner.run(
            Agent("Context", model=model, tools=[effect]),
            "hello",
            model_invocation="stream",
            on_model_event=observe,
        )
    assert released == ["outside"] and marker.get() == "outside"
    assert len(tasks) == 3 and all(task is tasks[0] for task in tasks)
    assert effects == [] and not model.closed
    if failure == "observer":
        assert raised.value is observer_error
    else:
        assert "after response.completed" in str(raised.value)
    chain: list[BaseException] = []
    error: BaseException | None = raised.value
    while error is not None and error not in chain:
        chain.append(error)
        error = error.__context__
    assert not any(isinstance(error, ValueError) for error in chain)
    if fail_cleanup:
        assert cleanup_error in chain


@pytest.mark.parametrize("phase", ["next", "closing"])
async def test_context_bound_generator_cleanup_survives_repeated_cancellation(phase: str) -> None:
    marker = ContextVar("stream-cancel-context", default="outside")
    entered, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    released: list[str] = []
    tasks: list[Any] = []
    effects: list[str] = []
    final = await terminal(tools=True)

    @function_tool
    def effect() -> str:
        effects.append("ran")
        return "ran"

    class ContextBoundModel(BorrowedModel):
        async def stream_response(
            self, *args: Any, **kwargs: Any
        ) -> AsyncIterator[TResponseStreamEvent]:
            tasks.append(asyncio.current_task())
            token = marker.set("iterating")
            try:
                yield delta() if phase == "next" else final
                entered.set()
                await asyncio.Future()
            finally:
                tasks.append(asyncio.current_task())
                closing.set()
                await release.wait()
                marker.reset(token)
                released.append(marker.get())

    def observe(event: TResponseStreamEvent) -> None:
        if phase == "closing":
            raise RuntimeError("observer failed before cancellation")

    model = ContextBoundModel()
    task = asyncio.create_task(
        SDKRunner.run(
            Agent("Context cancellation", model=model, tools=[effect]),
            "hello",
            model_invocation="stream",
            on_model_event=observe,
        )
    )
    try:
        await asyncio.wait_for((entered if phase == "next" else closing).wait(), 15)
        task.cancel()
        await asyncio.wait_for(closing.wait(), 15)
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done() and released == []
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 15)
        assert released == ["outside"] and marker.get() == "outside"
        assert len(tasks) == 2 and tasks[0] is tasks[1]
        assert effects == [] and not model.closed
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancellation_joins_natural_finalization_after_completed_terminal() -> None:
    marker = ContextVar("stream-terminal-context", default="outside")
    closing, release = asyncio.Event(), asyncio.Event()
    released: list[str] = []
    tasks: list[Any] = []
    effects: list[str] = []
    final = await terminal(tools=True)

    @function_tool
    def effect() -> str:
        effects.append("ran")
        return "ran"

    class ContextBoundModel(BorrowedModel):
        async def stream_response(
            self, *args: Any, **kwargs: Any
        ) -> AsyncIterator[TResponseStreamEvent]:
            tasks.append(asyncio.current_task())
            token = marker.set("iterating")
            try:
                yield final
            finally:
                tasks.append(asyncio.current_task())
                closing.set()
                await release.wait()
                marker.reset(token)
                released.append(marker.get())

    model = ContextBoundModel()
    task = asyncio.create_task(
        SDKRunner.run(
            Agent("Terminal cancellation", model=model, tools=[effect]),
            "hello",
            model_invocation="stream",
        )
    )
    try:
        # The completed event was accepted. The next iterator step is now
        # awaiting natural generator finalization, before it can report EOF.
        await asyncio.wait_for(closing.wait(), 15)
        task.cancel("original cancellation")
        await asyncio.sleep(0.05)
        assert not task.done() and released == [] and effects == []
        task.cancel("repeated cancellation")
        await asyncio.sleep(0.05)
        assert not task.done() and released == [] and effects == []
        release.set()
        with pytest.raises(asyncio.CancelledError) as raised:
            await asyncio.wait_for(task, 15)
        # Python 3.11 added cancellation-message propagation to task awaiters.
        if sys.version_info >= (3, 11):
            assert raised.value.args == ("original cancellation",)
        assert released == ["outside"] and marker.get() == "outside"
        assert len(tasks) == 2 and tasks[0] is tasks[1]
        assert effects == [] and not model.closed
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_native_idle_resume_uses_current_override_and_settings(responses: Any) -> None:
    first = await SDKRunner.run(
        Agent("Resume", model=ScriptedModel([[assistant_message("first")]])),
        "one",
        return_state=True,
    )
    state = first.native_result.state
    assert state is not None
    current = ScriptedModel([[assistant_message("second")]])
    provider = SelectedProvider(current)
    result = await SDKRunner.run(
        Agent("Resume", model="old"),
        "two",
        state=state,
        model="current",
        model_provider=provider,
        model_invocation="stream",
        model_settings=ModelSettings(temperature=0.1),
    )
    assert result.final_output == "second" and provider.names == ["current"]
    assert current.calls[0].streamed and current.calls[0].model_settings.temperature == 0.1
    assert "first" in str(current.calls[0].input)
    assert responses.requests == []
