"""Runner streaming through synthetic provider and native adapter boundaries."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from openai.types.responses import ResponseFailedEvent, ResponseOutputMessage
from pydantic import BaseModel

from agents import (
    Agent,
    GuardrailFunctionOutput,
    ModelBehaviorError,
    ModelRetrySettings,
    ModelSettings,
    ModelTimeoutError,
    OutputGuardrailTripwireTriggered,
    RetryDecision,
    Runner,
    function_tool,
    output_guardrail,
)
from agents.models.multi_provider import MultiProvider
from agents.stream_events import ApprovedText, FinalOutputAccepted
from agents.testing import ModelStep, ScriptedModel, assistant_message

from .models.test_codex_provider import FakeThread, config, threads as threads
from .test_responses import get_function_tool_call, get_text_message
from .utils.simple_session import SimpleListSession

pytestmark = pytest.mark.asyncio


async def collect(events):
    return [event async for event in events]


def scripted(text="answer", **kwargs):
    model = ScriptedModel()
    model.enqueue([get_text_message(text)])
    return Runner.run_streamed(Agent(name="root", model=model), "start", **kwargs)


async def test_native_prefixes_and_raw_consumer_are_independent(tmp_path, threads):
    # Native envelopes distinguish the child turn before normalization.
    class Thread(FakeThread):
        async def next_event(self):
            event = await super().next_event()
            if event and "child" in event:
                return json.dumps({**json.loads(event), "id": "child"})
            return event

    thread = Thread(
        text="a🙂bc",
        events=[
            {"type": "agent_message_content_delta", "item_id": "child", "delta": "hidden"},
        ],
    )
    threads[0].append(thread)
    run = Runner.run_streamed(
        Agent(name="root"),
        "private instruction",
        session=SimpleListSession("session"),
        run_config=config(tmp_path),
    )
    seen = []

    def approve(text):
        seen.append(text)
        return text[:1]

    reader = run.subscribe_approved_output(approve)
    with pytest.raises(RuntimeError):
        run.subscribe_approved_output()
    raw, approved = await asyncio.gather(collect(run.stream_events()), collect(reader))
    assert [
        event.data.delta
        for event in raw
        if event.type == "raw_response_event" and event.data.type == "response.output_text.delta"
    ] == ["a🙂", "bc"]
    assert seen == ["a🙂", "🙂bc"]
    root = approved[0].submission_id
    assert approved == [
        ApprovedText(root, "session", 1, "turn_1", "item_1", 0, "a"),
        ApprovedText(root, "session", 1, "turn_1", "item_1", 0, "🙂"),
        FinalOutputAccepted(root, "session"),
    ]
    assert thread.closed and run.final_output == "a🙂bc"


@pytest.mark.parametrize("approval", [None, ""])
async def test_no_forced_flush(approval):
    run = scripted()
    events = await collect(run.subscribe_approved_output(lambda text: approval))
    assert len(events) == 1 and isinstance(events[0], FinalOutputAccepted)
    await collect(run.stream_events())


@pytest.mark.parametrize("approval", ["other", "\ud83d", False])
async def test_invalid_approval_fails_only_reader(approval):
    run = scripted("🙂")
    with pytest.raises(ValueError, match="exact text prefix"):
        await collect(run.subscribe_approved_output(lambda text: approval))
    await collect(run.stream_events())
    assert run.final_output == "🙂"


async def test_byte_bound_includes_unapproved_remainder(tmp_path, threads):
    text = "🙂" * 16384 + "."
    threads[0].append(FakeThread(text=text))
    run = Runner.run_streamed(Agent(name="root"), "start", run_config=config(tmp_path))
    with pytest.raises(ValueError, match="65536"):
        await collect(run.subscribe_approved_output(lambda text: None))
    await collect(run.stream_events())
    assert run.final_output == text


async def test_sequence_gap_fails_reader_without_reconstructing_text():
    # Drop a numbered event from an otherwise complete normalized provider stream.
    class Gap(ScriptedModel):
        async def stream_response(self, *args, **kwargs):
            async for event in super().stream_response(*args, **kwargs):
                if event.type != "response.content_part.added":
                    yield event

    model = Gap()
    model.enqueue([get_text_message("answer")])
    run = Runner.run_streamed(Agent(name="root", model=model), "start")
    with pytest.raises(ValueError, match="Gap"):
        await anext(run.subscribe_approved_output(lambda text: text))
    await collect(run.stream_events())
    assert run.final_output == "answer"


async def test_item_switch_revokes_awaited_approval(tmp_path, threads):
    entered, release, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Thread(FakeThread):
        async def events(self):
            yield {"type": "agent_message_content_delta", "item_id": "old", "delta": "skip"}
            await entered.wait()
            async for event in super().events():
                yield event
                release.set()
                await finish.wait()

    threads[0].append(Thread(text="ok"))
    run = Runner.run_streamed(Agent(name="root"), "start", run_config=config(tmp_path))

    async def approve(text):
        if text == "skip":
            entered.set()
            await release.wait()
        return text

    reader = run.subscribe_approved_output(approve)
    try:
        first = await asyncio.wait_for(anext(reader), 5)
        assert isinstance(first, ApprovedText) and (first.item_id, first.text) == ("item_1", "ok")
    finally:
        entered.set()
        release.set()
        finish.set()
        await collect(run.stream_events())
    assert [type(e) async for e in reader] == [FinalOutputAccepted]


@pytest.mark.parametrize("cancel", ["reader", "immediate", "after_turn"])
async def test_cancel_during_approval(tmp_path, threads, cancel):
    gate, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    thread = FakeThread(gate=gate)
    threads[0].append(thread)
    run = Runner.run_streamed(Agent(name="root"), "start", run_config=config(tmp_path))

    async def approve(text):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            pass
        return text

    reading = asyncio.create_task(collect(run.subscribe_approved_output(approve)))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if cancel == "reader":
            reading.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reading
            assert not thread.closed
        else:
            run.cancel(mode=cancel)
            release.set()
            assert await asyncio.wait_for(reading, 5) == []
    finally:
        release.set()
        gate.set()
        await collect(run.stream_events())
        await asyncio.gather(reading, return_exceptions=True)
    assert thread.closed
    if cancel == "reader":
        assert run.final_output == thread.text


async def test_early_prefix_survives_later_guardrail_failure(tmp_path, threads):
    @output_guardrail
    def reject(ctx, agent, output):
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    gate = asyncio.Event()
    threads[0].append(FakeThread(gate=gate))
    run = Runner.run_streamed(
        Agent(name="root", output_guardrails=[reject]), "start", run_config=config(tmp_path)
    )
    reader = run.subscribe_approved_output(lambda text: text)
    try:
        first = await asyncio.wait_for(anext(reader), 5)
        assert isinstance(first, ApprovedText) and first.text == "A "
    finally:
        gate.set()
    with pytest.raises(OutputGuardrailTripwireTriggered):
        await collect(run.stream_events())
    assert await collect(reader) == []


@pytest.mark.parametrize("display", [False, True])
async def test_no_final_after_persistence_failure(display):
    class FailingSession(SimpleListSession):
        async def add_items(self, items):
            if any(item.get("role") == "assistant" for item in items):
                raise RuntimeError("save failed")
            await super().add_items(items)

    run = scripted(session=FailingSession())
    if display:
        with pytest.raises(RuntimeError, match="save failed"):
            async with run.stream_approved_output() as events:
                async for _ in events:
                    pytest.fail("Unexpected receipt")
        return
    assert await collect(run.subscribe_approved_output()) == []
    with pytest.raises(RuntimeError, match="save failed"):
        await collect(run.stream_events())


@pytest.mark.parametrize("display", [False, True])
async def test_no_final_on_interruption(display):
    @function_tool(needs_approval=True)
    def pause():
        return "ok"

    model = ScriptedModel()
    model.enqueue([get_function_tool_call("pause", "{}")])
    run = Runner.run_streamed(Agent(name="root", model=model, tools=[pause]), "start")
    if display:
        async with run.stream_approved_output() as events:
            assert await collect(events) == []
    else:
        assert await collect(run.subscribe_approved_output()) == []
        await collect(run.stream_events())
    assert run.interruptions


@pytest.mark.parametrize("cancel", [False, True])
async def test_final_only_waits_for_provider_cleanup_even_with_empty_output(monkeypatch, cancel):
    entered, release = asyncio.Event(), asyncio.Event()
    close = MultiProvider.aclose

    async def held_close(provider):
        entered.set()
        await release.wait()
        await close(provider)

    monkeypatch.setattr(MultiProvider, "aclose", held_close)
    run = scripted("")
    reading = asyncio.create_task(collect(run.subscribe_approved_output()))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert run.final_output == ""
        assert not reading.done()
        if cancel:
            run.cancel()
    finally:
        release.set()
        events = await reading
        await collect(run.stream_events())
    if cancel:
        assert events == []
    else:
        assert len(events) == 1 and isinstance(events[0], FinalOutputAccepted)


async def test_nested_agent_and_tool_correction_stay_out_of_root_reader():
    child_model = ScriptedModel()
    child_model.enqueue([get_text_message("private correction")])
    child = Agent(name="child", model=child_model)
    model = ScriptedModel()
    model.enqueue([get_function_tool_call("ask", '{"input":"check"}')])
    model.enqueue([get_text_message("public answer")])
    root = Agent(
        name="root",
        model=model,
        tools=[child.as_tool(tool_name="ask", tool_description="Check the answer")],
    )
    run = Runner.run_streamed(root, "start")
    seen = []

    def approve(text):
        seen.append(text)
        return text

    events = await collect(run.subscribe_approved_output(approve))
    await collect(run.stream_events())
    assert seen == ["public answer"]
    assert [(e.turn, e.text) for e in events if isinstance(e, ApprovedText)] == [
        (2, "public answer")
    ]
    assert isinstance(events[-1], FinalOutputAccepted)
    assert child_model.calls


@pytest.mark.parametrize("boundary", ["part", "failure", "replacement"])
async def test_approval_cannot_outlive_its_candidate(boundary):
    # Hold the provider between parts, inside failed-stream cleanup, or during recovery.
    entered, retired, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    reply = asyncio.get_running_loop().create_future()

    class Answer(BaseModel):
        number: int

    async def hold():
        retired.set()
        await release.wait()

    class Model(ScriptedModel):
        async def stream_response(self, *args, **kwargs):
            async for event in super().stream_response(*args, **kwargs):
                if event.type == "response.created":
                    response = event.response
                if boundary == "failure" and event.type == "response.output_text.done":
                    try:
                        yield ResponseFailedEvent(
                            type="response.failed",
                            response=response.model_copy(update={"status": "failed"}),
                            sequence_number=event.sequence_number,
                        )
                    finally:
                        await hold()
                    return
                yield event
                if event.type == "response.output_text.delta":
                    await entered.wait()
                elif event.type == "response.content_part.added" and event.content_index == 1:
                    await hold()

    async def recover(error):
        await hold()
        return Answer(number=7)

    def approve(text):
        if text == "old":
            entered.set()
            return reply
        return text

    item = assistant_message("old")
    if boundary == "part":
        assert isinstance(item, ResponseOutputMessage)
        item.content.append(item.content[0].model_copy(update={"text": "new"}))
    root = Agent(
        name="root",
        model=Model([[item]]),
        output_type=Answer if boundary == "replacement" else None,
    )
    run = Runner.run_streamed(root, "start", error_handlers={"invalid_final_output": recover})
    reading = asyncio.create_task(collect(run.subscribe_approved_output(approve)))
    try:
        await asyncio.wait_for(retired.wait(), 5)
        # Register after the reader so it resumes before the provider is released.
        delivered = asyncio.gather(reply)
        reply.set_result("old")
        await delivered
    finally:
        entered.set()
        release.set()
        if not reply.done():
            reply.cancel()
        events = await reading
    if boundary == "failure":
        assert events == []
        with pytest.raises(ModelBehaviorError):
            await collect(run.stream_events())
    else:
        await collect(run.stream_events())
        assert isinstance(events[-1], FinalOutputAccepted)
        if boundary == "part":
            assert [(e.content_index, e.text) for e in events[:-1]] == [(1, "new")]
        else:
            assert len(events) == 1 and run.final_output == Answer(number=7)


async def test_retry_before_text_starts_a_fresh_response():
    # Interrupt only after the first attempt's numbered introduction.
    class Retry(ScriptedModel):
        async def stream_response(self, *args, **kwargs):
            async for event in super().stream_response(*args, **kwargs):
                yield event
                if event.type == "response.in_progress" and event.response.id == "first":
                    raise ConnectionError("retry")

    model = Retry(
        [
            ModelStep(response_id="first"),
            ModelStep(response_id="second", output=[assistant_message("answer")]),
        ]
    )
    retry = ModelRetrySettings(max_retries=1, policy=lambda _: RetryDecision(retry=True, delay=0))
    root = Agent(name="root", model=model, model_settings=ModelSettings(retry=retry))
    run = Runner.run_streamed(root, "start")
    events = await collect(run.subscribe_approved_output(lambda text: text))
    await collect(run.stream_events())
    chunk, final = events
    assert (chunk.turn, chunk.turn_id, chunk.text) == (1, "second", "answer")
    assert isinstance(final, FinalOutputAccepted)


@pytest.mark.parametrize("previews", [False, True])
@pytest.mark.allow_call_model_methods
async def test_hosted_child_raw_text_never_reaches_approval(previews):
    # The hosted adapter forwards child events before filtering the final output.
    from .extensions.experimental.hosted_multi_agent.test_model import (
        _completed,
        _created,
        _model,
        _root_final_message,
        _subagent_message,
    )

    child = _subagent_message("hidden")
    model, _ = _model(
        [
            _created("r", sequence_number=0),
            SimpleNamespace(
                type="response.output_item.added",
                sequence_number=1,
                output_index=0,
                item=SimpleNamespace(**child),
            ),
            SimpleNamespace(
                type="response.output_text.delta",
                sequence_number=2,
                output_index=0,
                item_id=child["id"],
                content_index=0,
                delta="hidden",
                agent=child["agent"],
            ),
            _completed("r", [child, _root_final_message("public")], sequence_number=3),
        ]
    )
    run = Runner.run_streamed(Agent(name="root", model=model), "start")
    reader = (
        run.subscribe_approved_output(lambda _: pytest.fail("Child draft"))
        if previews
        else run.subscribe_approved_output()
    )
    if previews:
        with pytest.raises(ValueError, match="isolate root"):
            await collect(reader)
    else:
        assert [type(event) async for event in reader] == [FinalOutputAccepted]
    raw = await collect(run.stream_events())
    assert any(
        e.type == "raw_response_event" and getattr(e.data, "delta", None) == "hidden" for e in raw
    )
    assert run.final_output == "public"


@pytest.mark.parametrize("ending", ["eof", "error", "cancel", "timeout"])
async def test_pending_approval_expires_before_attempt_cleanup(ending):
    entered, closing, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    reply = asyncio.get_running_loop().create_future()

    async def cleanup():
        closing.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            # The deadline can interrupt cleanup a second time.
            await finish.wait()

    # A provider iterator can end before a terminal event and still need an async close.
    class Stream:
        def __init__(self, events):
            self.events = events
            self.sent = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.sent:
                await entered.wait()
                if ending == "eof":
                    raise StopAsyncIteration
                if ending == "error":
                    raise ConnectionError("offline")
                try:
                    await asyncio.Event().wait()
                finally:
                    await cleanup()
            event = await anext(self.events)
            self.sent = event.type == "response.output_text.delta"
            return event

        async def aclose(self):
            await cleanup()
            await self.events.aclose()

    class Model(ScriptedModel):
        def stream_response(self, *args, **kwargs):
            return Stream(super().stream_response(*args, **kwargs))

    def approve(text):
        entered.set()
        return reply

    root = Agent(
        name="root",
        model=Model([[assistant_message("draft")]]),
        model_settings=ModelSettings(timeout=1 if ending == "timeout" else None),
    )
    run = Runner.run_streamed(root, "start")
    reading = asyncio.create_task(collect(run.subscribe_approved_output(approve)))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        if ending == "cancel":
            assert run.run_loop_task is not None
            run.run_loop_task.cancel()
        await asyncio.wait_for(closing.wait(), 5)
        delivered = asyncio.gather(reply)
        reply.set_result("draft")
        await delivered
    finally:
        finish.set()
        if not reply.done():
            reply.cancel()
        events = await reading
    assert events == []
    if ending == "cancel":
        await collect(run.stream_events())
    else:
        error = {"eof": ModelBehaviorError, "error": ConnectionError, "timeout": ModelTimeoutError}
        with pytest.raises(error[ending]):
            await collect(run.stream_events())


@pytest.mark.parametrize("timeout", [None, 30])
@pytest.mark.parametrize("terminal", [False, True])
async def test_cancelled_provider_close_releases_both_readers(timeout, terminal):
    # Cleanup itself can propagate cancellation without anyone cancelling Runner.
    class Stream:
        def __init__(self, inner):
            self.inner = inner

        def __aiter__(self):
            return self

        async def __anext__(self):
            event = await anext(self.inner)
            if not terminal and event.type == "response.completed":
                raise StopAsyncIteration
            return event

        async def aclose(self):
            await self.inner.aclose()
            cleanup = asyncio.get_running_loop().create_future()
            cleanup.cancel()
            await cleanup

    class Model(ScriptedModel):
        def stream_response(self, *args, **kwargs):
            return Stream(super().stream_response(*args, **kwargs))

    root = Agent(
        name="root",
        model=Model([[assistant_message("preview")]]),
        model_settings=ModelSettings(timeout=timeout),
    )
    run = Runner.run_streamed(root, "start")
    reader = run.subscribe_approved_output(lambda text: text)
    _, approved = await asyncio.wait_for(
        asyncio.gather(collect(run.stream_events()), collect(reader)), 5
    )
    assert [type(event) for event in approved] == [ApprovedText]
    assert run.final_output is None


async def test_display_owns_the_raw_reader_and_accepts_empty_output():
    run = scripted("")
    raw = run.stream_events()
    async with run.stream_approved_output() as events:
        with pytest.raises(RuntimeError, match="owns"):
            await anext(raw)
        with pytest.raises(RuntimeError):
            run.subscribe_approved_output()
        (receipt,) = await collect(events)
        assert isinstance(receipt, FinalOutputAccepted)
        assert run.final_output == ""
    with pytest.raises(RuntimeError, match="before"):
        async with run.stream_approved_output():
            pass


async def test_display_preview_survives_guardrail_rejection(tmp_path, threads):
    @output_guardrail
    def reject(ctx, agent, output):
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    thread = FakeThread(text="OK secret")
    threads[0].append(thread)
    run = Runner.run_streamed(
        Agent(name="root", output_guardrails=[reject]), "private", run_config=config(tmp_path)
    )
    seen = []
    async with run.stream_approved_output(
        lambda text: "OK" if text.startswith("OK") else None
    ) as events:
        with pytest.raises(OutputGuardrailTripwireTriggered):
            async for event in events:
                assert isinstance(event, ApprovedText)
                seen.append(event.text)
    assert seen == ["OK"]
    assert thread.closed


@pytest.mark.parametrize("ending", ["break", "cancel", "invalid"])
async def test_display_joins_native_execution_on_early_exit(tmp_path, threads, ending):
    thread = FakeThread(gate=asyncio.Event())
    threads[0].append(thread)
    shown = asyncio.Event()

    async def display():
        run = Runner.run_streamed(Agent(name="root"), "start", run_config=config(tmp_path))
        async with run.stream_approved_output(
            lambda text: "!" if ending == "invalid" else text
        ) as events:
            async for event in events:
                assert isinstance(event, ApprovedText)
                shown.set()
                if ending == "break":
                    break

    task = asyncio.create_task(display())
    if ending == "cancel":
        await asyncio.wait_for(shown.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    elif ending == "invalid":
        with pytest.raises(ValueError, match="prefix"):
            await asyncio.wait_for(task, 5)
    else:
        await asyncio.wait_for(task, 5)
        assert shown.is_set()
    assert thread.closed


async def test_early_display_exit_joins_sandbox_snapshot():
    from .sandbox.test_runtime import (
        Manifest,
        SandboxAgent,
        _FakeClient,
        _MarkerSnapshot,
        _PersistingStopSession,
        _sandbox_run_config,
        get_final_output_message,
    )

    release = asyncio.Event()
    session = _PersistingStopSession(Manifest(), release)
    model = ScriptedModel([[get_final_output_message("draft")]])
    run = Runner.run_streamed(
        SandboxAgent(name="root", model=model),
        "start",
        run_config=_sandbox_run_config(_FakeClient(session)),
    )
    async with run.stream_approved_output():
        while not session.stop_calls:
            await asyncio.sleep(0)
        asyncio.get_running_loop().call_soon(release.set)
    assert session.stop_calls == 1
    assert isinstance(session.state.snapshot, _MarkerSnapshot)
    assert session.state.snapshot.marker == "persisted"


@pytest.mark.parametrize("early", [False, True])
async def test_display_keeps_checkpoint_errors_detached(early):
    from agents import ResumableSession, ResumableSessionError

    from .memory.test_resumable_session import MemoryStore

    store = MemoryStore()
    session = await ResumableSession.create(store)
    store.fail_checkpoint = True
    try:
        raise ValueError("private caller data")
    except ValueError:
        run = scripted(session=session)
        with pytest.raises(ResumableSessionError) as caught:
            async with run.stream_approved_output() as events:
                if early:
                    assert run.run_loop_task is not None
                    await asyncio.wait((run.run_loop_task,))
                else:
                    async for _ in events:
                        pytest.fail("Unexpected receipt")
    assert caught.value.__context__ is None
