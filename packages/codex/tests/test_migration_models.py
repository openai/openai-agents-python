"""REQ68 model selection and stream admission through the real native loop."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager
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
        "failed",
        "incomplete",
        "error",
        "status",
        "failed_status",
        "terminal_error",
        "terminal_incomplete_details",
        "preterminal_error",
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
    if bad in {"missing", "preterminal_error"}:
        events = [delta()]
    elif bad in {"failed", "incomplete", "error"}:
        events = [
            final.model_copy(update={"type": "error" if bad == "error" else f"response.{bad}"})
        ]
    elif bad == "status":
        final.response.status = "incomplete"
    elif bad == "failed_status":
        final.response.status = "failed"
    elif bad == "terminal_error":
        from openai.types.responses import ResponseError

        final.response.status = None
        final.response.error = ResponseError(code="server_error", message="provider failure")
    elif bad == "terminal_incomplete_details":
        from openai.types.responses.response import IncompleteDetails

        final.response.status = None
        final.response.incomplete_details = IncompleteDetails(reason="max_output_tokens")
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
                if bad == "preterminal_error":
                    raise RuntimeError("preterminal failure")
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
        await asyncio.wait_for((model.entered if hold == "next" else model.closing).wait(), 15)
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
                    yield final if failure == "observer" else delta("x" * (4 * 1024 * 1024))
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
    expected = RuntimeError if failure == "observer" else UserError
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
        assert "model stream exceeds 4 MiB" in str(raised.value)
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


async def test_cancellation_joins_explicit_close_after_completed_terminal() -> None:
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
        # The completed event was accepted. Direct aclose now awaits the
        # generator finalizer without reading any post-terminal event.
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


@pytest.mark.parametrize("selection", ["plain", "direct", "qualified", "explicit", "mutated"])
async def test_effective_defaults_follow_selection_and_preserve_explicit_settings(
    selection: str,
) -> None:
    class DefaultsModel(ScriptedModel):
        def get_default_model_settings(self) -> ModelSettings:
            assert selection == "qualified", "Only qualified selection consults model defaults"
            return ModelSettings(temperature=0.6, max_tokens=23)

    selected = DefaultsModel([[assistant_message("selected")]])
    provider = SelectedProvider(selected)
    agent = Agent("Defaults", model="gpt-5")
    if selection == "explicit":
        agent.model_settings = ModelSettings(temperature=0.7, max_tokens=19)
    elif selection == "mutated":
        agent.model_settings.max_tokens = 17
    options: dict[str, Any] = {
        "model": "qualified/model" if selection == "qualified" else "gpt-5-chat-latest",
        "model_provider": provider,
    }
    if selection == "direct":
        options = {"model": selected}
    if selection in {"explicit", "mutated", "qualified"}:
        options["model_settings"] = ModelSettings(temperature=0.2)
    result = await SDKRunner.run(agent, "hello", **options)
    assert result.final_output == "selected"
    settings = selected.calls[0].model_settings
    if selection in {"plain", "direct"}:
        assert settings.reasoning is None and settings.verbosity is None
    elif selection == "qualified":
        assert settings.max_tokens == 23 and settings.temperature == 0.2
    elif selection == "explicit":
        assert settings.max_tokens == 19 and settings.temperature == 0.2
    else:
        assert settings.max_tokens == 17 and settings.temperature == 0.2
        assert settings.reasoning is not None and settings.reasoning.effort == "low"


@pytest.mark.parametrize("buffered", [False, True])
async def test_dictionary_override_normalizes_and_preserves_agent_precedence(
    buffered: bool,
) -> None:
    selected = ScriptedModel([[assistant_message("dict")]])
    agent = Agent(
        "Dictionary", model=selected, model_settings=ModelSettings(max_tokens=29, temperature=0.8)
    )
    override = {"temperature": 0.2, "reasoning": {"effort": "low"}}
    if buffered:
        result = await SDKRunner.run_streamed(
            agent, "hello", model_settings=override, model_invocation="stream"
        ).get_result()
    else:
        result = await SDKRunner.run(agent, "hello", model_settings=override)
    assert result.final_output == "dict"
    settings = selected.calls[0].model_settings
    assert settings.temperature == 0.2 and settings.max_tokens == 29
    assert settings.reasoning is not None and settings.reasoning.effort == "low"
    assert agent.model_settings.temperature == 0.8 and override["temperature"] == 0.2


@pytest.mark.parametrize(
    "settings",
    [
        {"unknown": 1},
        {"temperature": "hot"},
        {"reasoning": {"effort": []}},
        {"extra_body": {"x": 1}},
    ],
)
async def test_invalid_dictionary_settings_reject_before_provider_lookup(
    settings: dict[str, Any],
) -> None:
    selected = ScriptedModel([[assistant_message("unused")]])
    provider = SelectedProvider(selected)
    with pytest.raises((TypeError, ValueError, UserError)):
        await SDKRunner.run(
            Agent("Invalid dictionary", model="gpt-5"),
            "hello",
            model_provider=provider,
            model_settings=settings,
        )
    assert provider.names == [] and selected.calls == ()


async def test_resolved_model_defaults_are_validated_before_execution() -> None:
    class InvalidDefaults(ScriptedModel):
        def get_default_model_settings(self) -> ModelSettings:
            return ModelSettings(extra_body={"unsupported": True})

    selected = InvalidDefaults([[assistant_message("unused")]])
    provider = SelectedProvider(selected)
    with pytest.raises(UserError, match="extra_body"):
        await SDKRunner.run(
            Agent("Invalid defaults", model="qualified/model"), "hello", model_provider=provider
        )
    assert provider.names == ["qualified/model"] and selected.calls == ()


@pytest.mark.parametrize("provenance", [False, True])
async def test_chat_handler_completed_without_status_uses_native_text_path(
    provenance: bool,
) -> None:
    from openai.types.chat import ChatCompletionChunk
    from openai.types.responses import Response

    from agents.classic.models.chatcmpl_stream_handler import ChatCmplStreamHandler

    template = Response(
        id="chat-normalized",
        created_at=0,
        model="",
        object="response",
        output=[],
        tool_choice="auto",
        tools=[],
        parallel_tool_calls=False,
    )

    async def chunks() -> AsyncIterator[ChatCompletionChunk]:
        # Empty optional provenance tests supported normalized output. The
        # populated variant retains provider_data rejection, without claiming
        # support for an AnyLLM/LiteLLM integration.
        yield ChatCompletionChunk.model_validate(
            {
                "id": "upstream-id" if provenance else "",
                "model": "upstream-model" if provenance else "",
                "created": 0,
                "object": "chat.completion.chunk",
                "choices": [
                    {"index": 0, "delta": {"content": "chat answer"}, "finish_reason": "stop"}
                ],
            }
        )

    class ChatModel(BorrowedModel):
        async def stream_response(
            self, *args: Any, **kwargs: Any
        ) -> AsyncIterator[TResponseStreamEvent]:
            async with aclosing(
                ChatCmplStreamHandler.handle_stream(template, chunks())  # type: ignore[arg-type]
            ) as stream:
                async for event in stream:
                    yield event

    events: list[TResponseStreamEvent] = []
    model = ChatModel()
    operation = SDKRunner.run(
        Agent("Chat normalized", model=model),
        "hello",
        model_invocation="stream",
        on_model_event=events.append,
    )
    if provenance:
        with pytest.raises(UserError, match="provider_data"):
            await operation
    else:
        result = await operation
        assert result.final_output == "chat answer" and result.last_response_id == "chat-normalized"
        assert result.raw_responses[0].usage.requests == 1
    assert isinstance(events[-1], ResponseCompletedEvent) and events[-1].response.status is None
    assert not model.closed


@pytest.mark.parametrize(
    "kind", ["response.failed", "response.incomplete", "error", "response.error"]
)
async def test_failure_event_is_observed_as_detached_snapshot_before_rejection(kind: str) -> None:
    from openai.types.responses import (
        ResponseErrorEvent,
        ResponseFailedEvent,
        ResponseIncompleteEvent,
    )

    final = await terminal(tools=True)
    if kind == "response.failed":
        event = ResponseFailedEvent(type=kind, response=final.response, sequence_number=0)
    elif kind == "response.incomplete":
        event = ResponseIncompleteEvent(type=kind, response=final.response, sequence_number=0)
    else:
        event = ResponseErrorEvent(
            type="error", message="provider failure", code=None, param=None, sequence_number=0
        )
        if kind == "response.error":
            event = event.model_copy(update={"type": kind})
    observed: list[TResponseStreamEvent] = []
    effects: list[str] = []

    @function_tool
    def effect() -> str:
        effects.append("ran")
        return "ran"

    def observe(copy: TResponseStreamEvent) -> None:
        assert copy.type == kind
        observed.append(copy)
        copy.type = "response.completed"
        if hasattr(copy, "response"):
            copy.response.output.clear()

    model = GatedModel([event])
    with pytest.raises(ModelBehaviorError, match="successful completed"):
        await SDKRunner.run(
            Agent("Failure events", model=model, tools=[effect]),
            "hello",
            model_invocation="stream",
            on_model_event=observe,
        )
    assert len(observed) == 1 and observed[0] is not event and event.type == kind
    assert (
        final.response.output
        and effects == []
        and model.closed.is_set()
        and not model.resource_closed
    )


@pytest.mark.parametrize("tail", ["duplicate", "delta", "normal_error"])
async def test_terminal_tail_is_not_consumed_and_close_still_runs(tail: str) -> None:
    final = await terminal()
    tail_reads: list[str] = []
    closed: list[bool] = []

    async def produce(request: Any) -> AsyncIterator[TResponseStreamEvent]:
        try:
            yield final
            tail_reads.append(tail)
            if tail == "normal_error":
                raise RuntimeError("unread tail failure")
            yield final if tail == "duplicate" else delta()
        finally:
            closed.append(True)

    model = ScriptedModel([ModelStep.stream(produce)])
    observed: list[TResponseStreamEvent] = []
    result = await SDKRunner.run(
        Agent("Unread tail", model=model),
        "hello",
        model_invocation="stream",
        on_model_event=observed.append,
    )
    assert result.final_output == "done" and closed == [True] and tail_reads == []
    assert len(observed) == 1 and isinstance(observed[0], ResponseCompletedEvent)


@pytest.mark.parametrize("outcome", ["success", "cancel", "close_error"])
async def test_terminal_close_never_advances_remote_tail_and_joins_same_context_cleanup(
    outcome: str,
) -> None:
    marker = ContextVar("terminal-close-context", default="outside")
    tail_entered, remote_tail = asyncio.Event(), asyncio.Event()
    closing, cleanup_release = asyncio.Event(), asyncio.Event()
    tasks: list[Any] = []
    released: list[str] = []
    effects: list[str] = []
    final = await terminal(tools=True)

    @function_tool
    def effect() -> str:
        effects.append("ran")
        return "ran"

    class TerminalModel(BorrowedModel):
        invocations = 0

        def stream_response(self, *args: Any, **kwargs: Any) -> AsyncIterator[TResponseStreamEvent]:
            self.invocations += 1
            if self.invocations > 1:
                return super().stream_response(*args, **kwargs)
            tasks.append(asyncio.current_task())
            factory_token = marker.set("factory")

            async def produce() -> AsyncIterator[TResponseStreamEvent]:
                tasks.append(asyncio.current_task())
                token = marker.set("iterating")
                try:
                    yield final
                    tail_entered.set()
                    await remote_tail.wait()  # This gate is never released, even in teardown.
                finally:
                    tasks.append(asyncio.current_task())
                    closing.set()
                    await cleanup_release.wait()
                    marker.reset(token)
                    marker.reset(factory_token)
                    released.append(marker.get())
                    if outcome == "close_error":
                        raise RuntimeError("terminal cleanup failed")

            return produce()

    model = TerminalModel([[assistant_message("finished")]])
    provider = SelectedProvider(model)
    task = asyncio.create_task(
        SDKRunner.run(
            Agent("Terminal close", model="selected", tools=[effect]),
            "hello",
            model_provider=provider,
            model_invocation="stream",
        )
    )
    try:
        await asyncio.wait_for(closing.wait(), 15)
        assert not tail_entered.is_set() and not remote_tail.is_set()
        assert not task.done() and effects == [] and released == []
        assert len(tasks) == 3 and all(owner is tasks[0] for owner in tasks)
        if outcome == "cancel":
            task.cancel("original cancellation")
            await asyncio.sleep(0.05)
            task.cancel("repeated cancellation")
            await asyncio.sleep(0.05)
            assert not task.done() and effects == [] and released == []
        cleanup_release.set()
        if outcome == "cancel":
            with pytest.raises(asyncio.CancelledError) as raised:
                await asyncio.wait_for(task, 15)
            if sys.version_info >= (3, 11):
                assert raised.value.args == ("original cancellation",)
            assert effects == []
        elif outcome == "close_error":
            with pytest.raises(RuntimeError, match="terminal cleanup failed"):
                await asyncio.wait_for(task, 15)
            assert effects == []
        else:
            result = await asyncio.wait_for(task, 15)
            assert result.final_output == "finished" and effects == ["ran"]
            model.assert_complete()
        assert released == ["outside"] and marker.get() == "outside"
        assert not tail_entered.is_set() and not model.closed and not provider.closed
    finally:
        cleanup_release.set()
        if not task.done():
            task.cancel()
            # Bound test teardown if a regression reintroduces a remote-tail
            # read. Cancel the test-owned producer, never release its tail gate.
            if tasks and not closing.is_set():
                tasks[0].cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 15)


@asynccontextmanager
async def _gated_builtin_responses_transport(
    *,
    fail_close: bool = False,
    read_end: Literal["blocked", "eof", "error"] = "blocked",
    completion: ResponseCompletedEvent | None = None,
) -> AsyncIterator[Any]:
    """Use the supported create-only client fallback with a real HTTP close gate.

    The custom client returns typed events and its iterator explicitly owns an
    in-memory httpx response. No with_streaming_response API or parser patch is
    present; this exercises _fetch_response's supported responses.create path.
    Ordinary installed-client SSE cancellation is not claimed to leak cleanup.
    """
    from types import SimpleNamespace

    import httpx2

    from agents.classic.models.openai_responses import OpenAIResponsesModel

    state = SimpleNamespace(
        reading=asyncio.Event(),
        close_started=asyncio.Event(),
        cleanup_release=asyncio.Event(),
        close_finished=asyncio.Event(),
        transport_closed=asyncio.Event(),
        close_calls=0,
        requests=0,
    )

    class Body(httpx2.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            raise AssertionError("The API boundary supplies parsed events")
            yield b""

        async def aclose(self) -> None:
            state.close_calls += 1
            state.close_started.set()
            await state.cleanup_release.wait()
            state.close_finished.set()
            if fail_close:
                try:
                    raise OSError("inner transport close diagnostic")
                except OSError as error:
                    raise RuntimeError("synthetic HTTP close failure") from error

    async def handle(request: httpx2.Request) -> httpx2.Response:
        state.requests += 1
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=Body())

    class Transport(httpx2.MockTransport):
        async def aclose(self) -> None:
            state.transport_closed.set()
            await super().aclose()

    http = httpx2.AsyncClient(transport=Transport(handle), trust_env=False)

    class TypedEvents:
        def __init__(self, response: httpx2.Response) -> None:
            self.response = response
            self.first = True

        def __aiter__(self) -> TypedEvents:
            return self

        async def __anext__(self) -> TResponseStreamEvent:
            if self.first:
                self.first = False
                return completion if completion is not None else delta()
            if completion is not None:
                raise StopAsyncIteration
            state.reading.set()
            if read_end == "eof":
                raise StopAsyncIteration
            if read_end == "error":
                raise ValueError("synthetic noncancel read failure")
            await asyncio.Future()
            raise AssertionError("A cancelled read must not produce another event")

        async def aclose(self) -> None:
            await self.response.aclose()

    async def create(**kwargs: Any) -> TypedEvents:
        assert kwargs["stream"] is True
        response = await http.send(
            http.build_request("POST", "https://synthetic.invalid/responses"), stream=True
        )
        return TypedEvents(response)

    client = SimpleNamespace(
        responses=SimpleNamespace(create=create),
        base_url="https://synthetic.invalid",
        is_closed=lambda: http.is_closed,
        close=http.aclose,
    )
    assert not hasattr(client.responses, "with_streaming_response")
    state.client = client
    state.model = OpenAIResponsesModel(model="gpt-5-chat-latest", openai_client=client)
    state.provider = SelectedProvider(state.model)
    try:
        yield state
    finally:
        state.cleanup_release.set()
        if state.close_started.is_set():
            await asyncio.wait_for(state.close_finished.wait(), 15)
        await client.close()


@pytest.mark.parametrize("fail_close", [False, True])
async def test_builtin_responses_cancellation_joins_http_close(fail_close: bool) -> None:
    async with _gated_builtin_responses_transport(fail_close=fail_close) as state:
        task = asyncio.create_task(
            SDKRunner.run(
                Agent("Built-in cancellation", model="selected"),
                "hello",
                model_provider=state.provider,
                model_invocation="stream",
            )
        )
        try:
            await asyncio.wait_for(state.reading.wait(), 15)
            task.cancel("original cancellation")
            await asyncio.wait_for(state.close_started.wait(), 15)
            await asyncio.sleep(0.05)
            task.cancel("repeated cancellation")
            await asyncio.sleep(0.05)
            assert not task.done(), "SDKRunner returned before its HTTP response finished closing"
            assert not state.close_finished.is_set()
            assert not state.client.is_closed() and not state.provider.closed
            assert not state.transport_closed.is_set() and state.requests == 1
            state.cleanup_release.set()
            with pytest.raises(asyncio.CancelledError) as raised:
                await asyncio.wait_for(task, 15)
            if sys.version_info >= (3, 11):
                assert raised.value.args == ("original cancellation",)
            assert state.close_finished.is_set() and state.close_calls == 1
            assert not state.client.is_closed() and not state.provider.closed
            assert not state.transport_closed.is_set()
            chain: list[BaseException] = []
            error: BaseException | None = raised.value
            while error is not None:
                assert not any(error is previous for previous in chain), "Exception context cycle"
                chain.append(error)
                error = error.__context__
        finally:
            state.cleanup_release.set()
            if not task.done():
                task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 15)


@pytest.mark.parametrize("classic_read_end", ["blocked", "error"])
async def test_builtin_responses_join_scope_does_not_change_concurrent_classic_cancellation(
    classic_read_end: Literal["blocked", "error"],
) -> None:
    from agents.classic import RunConfig, Runner

    async with (
        _gated_builtin_responses_transport() as sdk,
        _gated_builtin_responses_transport(read_end=classic_read_end) as classic,
    ):
        sdk_task = asyncio.create_task(
            SDKRunner.run(
                Agent("SDK concurrent", model="selected"),
                "hello",
                model_provider=sdk.provider,
                model_invocation="stream",
            )
        )
        classic_run = Runner.run_streamed(
            Agent("Classic concurrent", model="selected"),
            "hello",
            run_config=RunConfig(model_provider=classic.provider, tracing_disabled=True),
        )

        async def read_classic() -> None:
            async for _event in classic_run.stream_events():
                pass

        classic_task = asyncio.create_task(read_classic())
        try:
            await asyncio.wait_for(sdk.reading.wait(), 15)
            await asyncio.wait_for(classic.reading.wait(), 15)
            if classic_read_end == "error":
                await asyncio.wait_for(classic.close_started.wait(), 15)
            sdk_task.cancel()
            classic_run.cancel()
            await asyncio.wait_for(sdk.close_started.wait(), 15)
            await asyncio.wait_for(classic.close_started.wait(), 15)
            await asyncio.wait_for(classic_task, 15)
            assert classic_run.is_complete
            assert not classic.close_finished.is_set(), "Classic must retain background close"
            await asyncio.sleep(0.05)
            assert not sdk_task.done(), "SDK cleanup scope must join only its own response"
            assert not sdk.close_finished.is_set()
            assert not sdk.client.is_closed() and not classic.client.is_closed()
            assert not sdk.provider.closed and not classic.provider.closed
            classic.cleanup_release.set()
            await asyncio.wait_for(classic.close_finished.wait(), 15)
            assert not sdk_task.done()
            sdk.cleanup_release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(sdk_task, 15)
            assert sdk.close_finished.is_set() and sdk.close_calls == classic.close_calls == 1
        finally:
            sdk.cleanup_release.set()
            classic.cleanup_release.set()
            classic_run.cancel()
            if not sdk_task.done():
                sdk_task.cancel()
            await asyncio.wait_for(
                asyncio.gather(sdk_task, classic_task, return_exceptions=True), 15
            )


@pytest.mark.parametrize("read_end", ["blocked", "error"])
async def test_builtin_joined_cleanup_retains_cancel_and_inner_error_before_native_transport(
    read_end: Literal["blocked", "error"],
) -> None:
    from agents.classic.models._run_context import joined_model_stream_cleanup

    # Cancellation transport through native callbacks may discard exception
    # contexts. Check diagnostics at the built-in model's owning boundary.
    captured: list[tuple[type[BaseException], str]] = []
    async with _gated_builtin_responses_transport(fail_close=True, read_end=read_end) as state:

        async def consume() -> None:
            with joined_model_stream_cleanup():
                try:
                    async for _event in state.model.stream_response(
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
                        pass
                except asyncio.CancelledError as primary:
                    seen: set[int] = set()
                    error: BaseException | None = primary
                    while error is not None:
                        assert id(error) not in seen, "Exception context cycle"
                        seen.add(id(error))
                        captured.append((type(error), str(error)))
                        error = error.__cause__ or error.__context__
                    raise

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(state.reading.wait(), 15)
            if read_end == "error":
                await asyncio.wait_for(state.close_started.wait(), 15)
            task.cancel("model cancellation")
            await asyncio.wait_for(state.close_started.wait(), 15)
            assert not task.done() and not state.close_finished.is_set()
            state.cleanup_release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 15)
            assert captured[0][0] is asyncio.CancelledError
            assert (RuntimeError, "synthetic HTTP close failure") in captured
            assert (OSError, "inner transport close diagnostic") in captured
            assert state.close_finished.is_set() and not state.client.is_closed()
        finally:
            state.cleanup_release.set()
            if not task.done():
                task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 15)


@pytest.mark.parametrize("read_end,fail_close", [("eof", False), ("error", False), ("error", True)])
async def test_builtin_responses_cancellation_joins_close_already_started(
    read_end: Literal["eof", "error"], fail_close: bool
) -> None:
    async with _gated_builtin_responses_transport(
        read_end=read_end, fail_close=fail_close
    ) as state:
        task = asyncio.create_task(
            SDKRunner.run(
                Agent("Already closing", model="selected"),
                "hello",
                model_provider=state.provider,
                model_invocation="stream",
            )
        )
        try:
            # Both paths start transport close before application cancellation.
            await asyncio.wait_for(state.close_started.wait(), 15)
            assert state.reading.is_set() and state.close_calls == 1
            task.cancel("original cancellation")
            await asyncio.sleep(0.05)
            task.cancel("repeated cancellation")
            await asyncio.sleep(0.05)
            assert not task.done(), "SDKRunner abandoned an already-started HTTP close"
            assert not state.close_finished.is_set() and state.close_calls == 1
            assert not state.client.is_closed() and not state.provider.closed
            state.cleanup_release.set()
            with pytest.raises(asyncio.CancelledError) as raised:
                await asyncio.wait_for(task, 15)
            if sys.version_info >= (3, 11):
                assert raised.value.args == ("original cancellation",)
            assert state.close_finished.is_set() and state.close_calls == 1
            assert state.requests == 1 and not state.transport_closed.is_set()
            assert not state.client.is_closed() and not state.provider.closed
        finally:
            state.cleanup_release.set()
            if not task.done():
                task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 15)


@pytest.mark.parametrize("consumer", ["sdk", "classic"])
async def test_builtin_terminal_close_failure_blocks_sdk_tools_and_preserves_classic_default(
    consumer: str,
) -> None:
    from agents.classic import RunConfig, Runner

    effects: list[str] = []

    @function_tool
    def effect() -> str:
        effects.append("ran")
        return "ran"

    completion = await terminal(tools=consumer == "sdk")
    async with _gated_builtin_responses_transport(fail_close=True, completion=completion) as state:
        agent = Agent("Terminal cleanup", model="selected", tools=[effect])
        if consumer == "sdk":
            task = asyncio.create_task(
                SDKRunner.run(
                    agent,
                    "hello",
                    model_provider=state.provider,
                    model_invocation="stream",
                )
            )
        else:
            running = Runner.run_streamed(
                agent,
                "hello",
                run_config=RunConfig(model_provider=state.provider, tracing_disabled=True),
            )

            async def finish_classic() -> str:
                async for _event in running.stream_events():
                    pass
                return running.final_output

            task = asyncio.create_task(finish_classic())
        try:
            await asyncio.wait_for(state.close_started.wait(), 15)
            assert not task.done() and effects == [] and state.close_calls == 1
            assert not state.client.is_closed() and not state.provider.closed
            state.cleanup_release.set()
            if consumer == "sdk":
                with pytest.raises(RuntimeError, match="synthetic HTTP close failure"):
                    await asyncio.wait_for(task, 15)
            else:
                assert await asyncio.wait_for(task, 15) == "done"
            assert effects == [] and state.close_finished.is_set()
            assert state.close_calls == state.requests == 1
            assert not state.client.is_closed() and not state.provider.closed
            assert not state.transport_closed.is_set()
        finally:
            state.cleanup_release.set()
            if not task.done():
                task.cancel()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 15)
