"""Run inbox behavior at model, tool, and conversation boundaries."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest

from agents import (
    Agent,
    AgentHooks,
    ContinueWithFeedback,
    GuardrailFunctionOutput,
    InputGuardrail,
    InputGuardrailTripwireTriggered,
    ItemHelpers,
    MaxTurnsExceeded,
    MessageOutputItem,
    MessageReceipt,
    ModelResponse,
    ModelSettings,
    ModelTimeoutError,
    OutputGuardrailTripwireTriggered,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    RunInbox,
    Runner,
    RunState,
    ToolCallOutputItem,
    ToolExecutionConfig,
    ToolGuardrailFunctionOutput,
    ToolInputGuardrailData,
    ToolOutputGuardrailData,
    ToolsToFinalOutputResult,
    UserError,
    function_tool,
    handoff,
    output_guardrail,
    tool_input_guardrail,
    tool_output_guardrail,
)
from agents.decorators import tool
from agents.models.interface import ModelProvider
from agents.result import RunResultBase
from agents.retry import ModelRetrySettings
from agents.run import AgentRunner
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from agents.usage import Usage
from tests.test_responses import get_refusal_message
from tests.utils.simple_session import SimpleListSession

pytestmark = pytest.mark.asyncio


async def drive(streamed: bool, agent: Agent[Any], value: Any = "start", **kwargs: Any) -> Any:
    kwargs.setdefault("run_config", RunConfig(tracing_disabled=True))
    if not streamed:
        return await Runner.run(agent, value, **kwargs)
    result = Runner.run_streamed(agent, value, **kwargs)
    try:
        async for _ in result.stream_events():
            pass
        return result
    finally:
        if not result.is_complete:
            result.cancel()


async def wait(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), 3)


async def finish(task: asyncio.Task[Any], *release: asyncio.Event) -> None:
    for event in release:
        event.set()
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def message_text(item: Any, role: str = "user") -> str | None:
    if not isinstance(item, dict) or item.get("role") != role:
        return None
    content = item.get("content")
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in content or [] if isinstance(part, dict))


def messages(items: list[Any]) -> list[str]:
    return [text for item in items if (text := message_text(item)) is not None]


def gate_tool(*, terminal: bool = False, approval: bool = False):
    entered, release = asyncio.Event(), asyncio.Event()
    effects: list[str] = []

    @tool(needs_approval=approval)
    async def work() -> str:
        entered.set()
        await release.wait()
        effects.append("worked")
        return "tool-result"

    model = ScriptedModel(
        [[function_call("work", "{}", call_id="work-1")], [assistant_message("done")]]
    )
    agent = Agent(
        name="worker",
        model=model,
        tools=[work],
        tool_use_behavior="stop_on_first_tool" if terminal else "run_llm_again",
    )
    return agent, model, entered, release, effects


@pytest.mark.parametrize("streamed", [False, True], ids=["awaited", "streamed"])
async def test_live_input_follows_all_parallel_tools_in_send_order(
    streamed: bool,
) -> None:
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    completed = [asyncio.Event(), asyncio.Event()]

    @tool
    async def work(index: int) -> str:
        entered[index].set()
        await release[index].wait()
        completed[index].set()
        return f"tool-result-{index}"

    model = ScriptedModel(
        [
            [
                function_call("work", '{"index":0}', call_id="zero"),
                function_call("work", '{"index":1}', call_id="one"),
            ],
            [assistant_message("done")],
        ]
    )
    agent = Agent(name="worker", model=model, tools=[work])
    inbox = RunInbox()
    task = asyncio.create_task(drive(streamed, agent, inbox=inbox))
    try:
        await asyncio.gather(*(wait(event) for event in entered))
        first = inbox.steer("Explain the failure.")
        second = inbox.steer("Then suggest a fix.")
        assert first.status == second.status == "queued"
        release[1].set()
        await wait(completed[1])
        calls_while_waiting = model.calls
        assert len(calls_while_waiting) == 1 and first.status == second.status == "queued"
        release[0].set()
        assert (await asyncio.wait_for(task, 3)).final_output == "done"
        outcomes = await asyncio.gather(first.wait(), second.wait())
        assert [outcome.status for outcome in outcomes] == ["submitted", "submitted"]
        assert [outcome.turn_number for outcome in outcomes] == [2, 2]
        payload = model.calls[1].input
        assert messages(payload)[-2:] == ["Explain the failure.", "Then suggest a fix."]
        output_positions = [
            i for i, item in enumerate(payload) if item.get("type") == "function_call_output"
        ]
        correction_positions = [
            i for i, item in enumerate(payload) if message_text(item) in messages(payload)[-2:]
        ]
        assert len(output_positions) == 2 and max(output_positions) < min(correction_positions)
    finally:
        await finish(task, *release)


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("terminal", [False, True], ids=["max-turns", "terminal-tool"])
async def test_no_extra_request_for_unused_correction(streamed: bool, terminal: bool) -> None:
    agent, model, entered, release, _ = gate_tool(terminal=terminal)
    inbox = RunInbox()
    task = asyncio.create_task(drive(streamed, agent, inbox=inbox, max_turns=1))
    try:
        await wait(entered)
        receipt = inbox.steer("No extra request.")
        release.set()
        if terminal:
            assert (await asyncio.wait_for(task, 3)).final_output == "tool-result"
        else:
            with pytest.raises(MaxTurnsExceeded):
                await asyncio.wait_for(task, 3)
        outcome = await asyncio.wait_for(receipt.wait(), 3)
        assert outcome.status == "not_submitted" and outcome.reason
        assert outcome.retained is False and len(model.calls) == 1
        with pytest.raises(UserError):
            inbox.steer("too late")
    finally:
        await finish(task, release)


@pytest.mark.parametrize("streamed", [False, True])
async def test_steer_continues_after_a_final_model_response(streamed: bool) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(call):
        entered.set()
        await release.wait()
        return [assistant_message("done")]

    model = ScriptedModel([ModelStep.respond(respond), [assistant_message("revised")]])
    inbox = RunInbox()
    task = asyncio.create_task(drive(streamed, Agent(name="final", model=model), inbox=inbox))
    try:
        await wait(entered)
        receipt = inbox.steer("Revise that answer.")
        release.set()
        result = await asyncio.wait_for(task, 3)
        assert result.final_output == "revised"
        outcome = await asyncio.wait_for(receipt.wait(), 3)
        assert outcome.status == "submitted" and outcome.turn_number == 2
        assert len(model.calls) == 2
        assert messages(model.calls[0].input) == ["start"]
        assert messages(model.calls[1].input) == ["start", "Revise that answer."]
        assert model.calls[1].input[-2]["role"] == "assistant"
    finally:
        await finish(task, release)


@pytest.mark.parametrize("streamed", [False, True])
async def test_preopen_late_and_reused_handles_reject(streamed: bool) -> None:
    agent, _, entered, release, _ = gate_tool()
    inbox = RunInbox()
    with pytest.raises(UserError):
        inbox.steer("before binding")
    task = asyncio.create_task(drive(streamed, agent, inbox=inbox))
    try:
        with pytest.raises(UserError):
            inbox.steer("task has not started")
        await wait(entered)
        with pytest.raises(UserError):
            await asyncio.to_thread(inbox.queue, "from a different thread")
        release.set()
        await asyncio.wait_for(task, 3)
        with pytest.raises(UserError):
            inbox.steer("after completion")
        with pytest.raises(UserError):
            await asyncio.wait_for(drive(streamed, agent, inbox=inbox), 3)
    finally:
        await finish(task, release)


@pytest.mark.parametrize("streamed", [False, True])
async def test_approval_pause_closes_unused_receipt_without_executing_tool(
    streamed: bool,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    effects = []

    @tool(needs_approval=True)
    def protected() -> str:
        effects.append("executed")
        return "effect"

    async def pending(call):
        entered.set()
        await release.wait()
        return [function_call("protected", "{}", call_id="protected-1")]

    model = ScriptedModel([ModelStep.respond(pending)])
    agent = Agent(name="approval", model=model, tools=[protected])
    inbox = RunInbox()
    task = asyncio.create_task(drive(streamed, agent, inbox=inbox))
    try:
        await wait(entered)
        receipt = inbox.steer("This does not approve the call.")
        queued = inbox.queue("Wait until the action is approved.")
        release.set()
        result = await asyncio.wait_for(task, 3)
        assert len(result.interruptions) == 1 and effects == []
        skipped = await asyncio.wait_for(queued.wait(), 3)
        assert skipped.status == "not_submitted" and skipped.retained is False
        outcome = await asyncio.wait_for(receipt.wait(), 3)
        assert outcome.status == "not_submitted" and outcome.retained is False
        assert len(model.calls) == 1
        with pytest.raises(UserError):
            inbox.steer("after pause")
    finally:
        await finish(task, release)


@pytest.mark.parametrize("streamed", [False, True])
async def test_parent_correction_never_enters_nested_agent_request(
    streamed: bool,
) -> None:
    nested_entered, nested_release = asyncio.Event(), asyncio.Event()

    async def child_response(call):
        nested_entered.set()
        await nested_release.wait()
        return [assistant_message("child answer")]

    child_model = ScriptedModel([ModelStep.respond(child_response)])
    child = Agent(name="child", model=child_model)
    child_tool = child.as_tool(tool_name="delegate", tool_description="Delegate a question.")
    parent_model = ScriptedModel(
        [
            [function_call("delegate", '{"input":"child question"}', call_id="delegate-1")],
            [assistant_message("parent answer")],
        ]
    )
    parent = Agent(name="parent", model=parent_model, tools=[child_tool])
    inbox = RunInbox()
    task = asyncio.create_task(drive(streamed, parent, inbox=inbox))
    try:
        await wait(nested_entered)
        receipt = inbox.steer("parent correction")
        assert receipt.status == "queued"
        nested_release.set()
        assert (await asyncio.wait_for(task, 3)).final_output == "parent answer"
        assert (await receipt.wait()).status == "submitted"
        assert "parent correction" not in messages(child_model.calls[0].input)
        assert messages(parent_model.calls[1].input)[-1] == "parent correction"
        assert len(child_model.calls) == 1 and len(parent_model.calls) == 2
    finally:
        await finish(task, nested_release)


@pytest.mark.parametrize("streamed", [False, True])
async def test_queue_overflow_preserves_existing_receipts_and_duplicate_messages(
    streamed: bool,
) -> None:
    agent, model, entered, release, _ = gate_tool()
    inbox = RunInbox(max_pending=2)
    task = asyncio.create_task(drive(streamed, agent, inbox=inbox))
    try:
        await wait(entered)
        first, second = inbox.steer("same"), inbox.steer("same")
        with pytest.raises(UserError):
            inbox.queue("overflow")
        release.set()
        await asyncio.wait_for(task, 3)
        assert (await first.wait()).status == (await second.wait()).status == "submitted"
        assert messages(model.calls[1].input).count("same") == 2
        assert "overflow" not in messages(model.calls[1].input)
    finally:
        await finish(task, release)


@pytest.mark.parametrize("streamed", [False, True])
async def test_cancelled_receipt_waiter_preserves_other_waiters_and_delivery(
    streamed: bool,
) -> None:
    agent, model, entered, release, _ = gate_tool()
    inbox = RunInbox()
    task = asyncio.create_task(drive(streamed, agent, inbox=inbox))
    waiter = None
    try:
        await wait(entered)
        receipt = inbox.steer("Keep this occurrence.")
        waiting = asyncio.Event()

        async def cancelled_waiter():
            waiting.set()
            return await receipt.wait()

        waiter = asyncio.create_task(cancelled_waiter())
        await wait(waiting)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert receipt.status == "queued"
        release.set()
        await asyncio.wait_for(task, 3)
        assert (await asyncio.wait_for(receipt.wait(), 3)).status == "submitted"
        assert messages(model.calls[1].input).count("Keep this occurrence.") == 1
    finally:
        if waiter is not None:
            await finish(waiter)
        await finish(task, release)


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("mode", ["drop", "copy", "merge", "instructions"])
async def test_filter_outcome_tracks_occurrence_instead_of_equal_text(
    streamed: bool, mode: str
) -> None:
    agent, model, entered, release, _ = gate_tool()
    inbox = RunInbox()

    def filter_input(data):
        if mode == "drop":
            data.model_data.input = [
                item for item in data.model_data.input if message_text(item) != "correction"
            ]
        elif mode == "copy":
            data.model_data.input = copy.deepcopy(data.model_data.input)
        elif "correction" in messages(data.model_data.input):
            items = data.model_data.input
            correction = items.pop()
            if mode == "merge":
                items[0].update(correction)
            else:
                data.model_data.instructions = message_text(correction)
        return data.model_data

    task = asyncio.create_task(
        drive(
            streamed,
            agent,
            inbox=inbox,
            run_config=RunConfig(call_model_input_filter=filter_input, tracing_disabled=True),
        )
    )
    try:
        await wait(entered)
        receipt = inbox.steer("correction")
        release.set()
        await asyncio.wait_for(task, 3)
        outcome = await asyncio.wait_for(receipt.wait(), 3)
        assert outcome.status == ("not_submitted" if mode == "drop" else "unknown")
        assert ("correction" in messages(model.calls[1].input)) == (mode in {"copy", "merge"})
        if mode == "instructions":
            assert model.calls[1].system_instructions == "correction"
    finally:
        await finish(task, release)


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("queued", [False, True], ids=["steer", "queue"])
async def test_live_guardrail_rejection_prevents_filter_model_and_session_exposure(
    streamed: bool,
    queued: bool,
) -> None:
    agent, model, entered, release, _ = gate_tool()
    guarded, filter_inputs = [], []
    session = SimpleListSession()

    async def guard(context, current_agent, value):
        text = [value] if isinstance(value, str) else messages(value)
        guarded.append(text)
        return GuardrailFunctionOutput(
            output_info=None, tripwire_triggered="blocked correction" in text
        )

    def filter_input(data):
        filter_inputs.extend(messages(data.model_data.input))
        return data.model_data

    agent.input_guardrails = [InputGuardrail(guardrail_function=guard, run_in_parallel=True)]
    inbox = RunInbox()
    task = asyncio.create_task(
        drive(
            streamed,
            agent,
            inbox=inbox,
            session=session,
            run_config=RunConfig(call_model_input_filter=filter_input, tracing_disabled=True),
        )
    )
    try:
        await wait(entered)
        send = inbox.queue if queued else inbox.steer
        receipt = send("blocked correction")
        release.set()
        with pytest.raises(InputGuardrailTripwireTriggered):
            await asyncio.wait_for(task, 3)
        outcome = await asyncio.wait_for(receipt.wait(), 3)
        assert outcome.status == "not_submitted" and outcome.retained is False
        assert len(model.calls) == (2 if queued else 1)
        assert "blocked correction" not in filter_inputs
        history = await session.get_items()
        assert messages(history) == ["start"]
        if queued:
            assert history[-1].get("role") == "assistant"
            assert message_text(history[-1], "assistant") == "done"
        assert any("blocked correction" in batch for batch in guarded)
    finally:
        await finish(task, release)


@pytest.mark.parametrize("streamed", [False, True])
async def test_cancelled_run_settles_queued_receipts_and_closes_intake(
    streamed: bool,
) -> None:
    agent, model, entered, release, effects = gate_tool()
    inbox = RunInbox()
    task = asyncio.create_task(drive(streamed, agent, inbox=inbox))
    try:
        await wait(entered)
        receipt = inbox.steer("pending cancellation")
        queued = inbox.queue("also pending")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        outcome = await asyncio.wait_for(receipt.wait(), 3)
        assert outcome.status == "not_submitted" and outcome.reason
        assert (await asyncio.wait_for(queued.wait(), 3)).status == "not_submitted"
        assert len(model.calls) == 1 and not effects
        with pytest.raises(UserError):
            inbox.steer("too late")
    finally:
        await finish(task, release)


@pytest.mark.parametrize("streamed", [False, True])
async def test_concurrent_runs_never_exchange_corrections(streamed: bool) -> None:
    entered = {name: asyncio.Event() for name in ("left", "right")}
    release = {name: asyncio.Event() for name in entered}

    @tool
    async def work(ctx: RunContextWrapper[str]) -> str:
        entered[ctx.context].set()
        await release[ctx.context].wait()
        return ctx.context

    payloads = {}

    async def final(call):
        context = next(
            item["output"] for item in call.input if item.get("type") == "function_call_output"
        )
        payloads[context] = messages(call.input)
        return [assistant_message(context)]

    model = ScriptedModel(
        [
            [function_call("work", "{}", call_id="left-tool")],
            [function_call("work", "{}", call_id="right-tool")],
            ModelStep.respond(final),
            ModelStep.respond(final),
        ]
    )
    agent = Agent(name="shared", model=model, tools=[work])
    inboxes = {name: RunInbox() for name in entered}
    tasks = {
        name: asyncio.create_task(drive(streamed, agent, context=name, inbox=inboxes[name]))
        for name in entered
    }
    try:
        await asyncio.gather(*(wait(event) for event in entered.values()))
        receipts = {name: inboxes[name].steer(f"correction-{name}") for name in entered}
        release["right"].set()
        assert (await asyncio.wait_for(tasks["right"], 3)).final_output == "right"
        assert receipts["left"].status == "queued"
        release["left"].set()
        await asyncio.wait_for(tasks["left"], 3)
        for name in entered:
            assert (await receipts[name].wait()).status == "submitted"
            assert payloads[name] == ["start", f"correction-{name}"]
    finally:
        await asyncio.gather(*(finish(tasks[name], release[name]) for name in entered))


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("server_managed", [False, True], ids=["local", "server"])
@pytest.mark.parametrize(
    "terminal", [False, True], ids=["steer-before-next-turn", "queue-after-terminal-tool"]
)
async def test_approved_tool_resume_accepts_input_before_tool_finishes(
    streamed: bool,
    server_managed: bool,
    terminal: bool,
) -> None:
    agent, model, entered, release, effects = gate_tool(approval=True, terminal=terminal)
    paused = await drive(streamed, agent, auto_previous_response_id=server_managed)
    state = paused.to_state()
    state.approve(state.get_interruptions()[0])
    inbox = RunInbox()
    task = asyncio.create_task(drive(streamed, agent, state, inbox=inbox))
    try:
        await wait(entered)
        send = inbox.queue if terminal else inbox.steer
        receipt = send("Explain before editing again.")
        assert receipt.status == "queued" and len(model.calls) == 1
        release.set()
        result = await asyncio.wait_for(task, 3)
        assert result.final_output == "done"
        assert (await receipt.wait()).status == "submitted"
        assert effects == ["worked"]
        # ScriptedModel reuses the response ID. Keep both calls exactly once.
        assert len(model.calls) == 2
        first, second = result.raw_responses
        assert first.response_id == second.response_id
        assert [first.output[0].type, second.output[0].type] == ["function_call", "message"]
        saved = result.to_state().to_json()["model_responses"]
        assert [response["output"][0]["type"] for response in saved] == [
            "function_call",
            "message",
        ]
        assert messages(model.calls[1].input)[-1] == "Explain before editing again."
        assert model.calls[1].input[-2]["type"] == "function_call_output"
    finally:
        await finish(task, release)


@pytest.mark.parametrize("outcome", ["complete", "failure", "provider_timeout", "cancel"])
async def test_inbox_closes_provider_stream_without_aclose(outcome: str) -> None:
    entered = asyncio.Event()
    close_calls = 0
    failure = (
        ModelTimeoutError(3) if outcome == "provider_timeout" else RuntimeError("provider failed")
    )

    # ScriptedModel normally returns an async generator, which always exposes aclose().
    class CloseOnlyModel(ScriptedModel):
        def stream_response(self, *args: Any, **kwargs: Any) -> Any:
            events = super().stream_response(*args, **kwargs)

            class Stream:
                def __aiter__(self) -> Stream:
                    return self

                async def __anext__(self) -> Any:
                    entered.set()
                    if outcome == "cancel":
                        await asyncio.Event().wait()
                    return await anext(events)

                def close(self) -> None:
                    nonlocal close_calls
                    close_calls += 1
                    raise RuntimeError("cleanup failed")

            return Stream()

    model = CloseOnlyModel(
        [failure if outcome in ("failure", "provider_timeout") else [assistant_message("done")]]
    )
    result = Runner.run_streamed(
        Agent(name="worker", model=model),
        "start",
        inbox=RunInbox(),
        run_config=RunConfig(tracing_disabled=True),
    )

    async def consume() -> None:
        async for _ in result.stream_events():
            pass

    if outcome in ("failure", "provider_timeout"):
        with pytest.raises(type(failure)) as caught:
            await consume()
        assert caught.value is failure
    else:
        if outcome == "cancel":
            try:
                await wait(entered)
            finally:
                result.cancel()
        await consume()
        if outcome == "complete":
            assert result.final_output == "done"
    assert close_calls == 1


@pytest.mark.parametrize("streamed", [False, True])
async def test_retry_preserves_submission_and_defers_new_correction(
    streamed: bool,
) -> None:
    agent, model, entered, release, _ = gate_tool()
    retry_entered, retry_release = asyncio.Event(), asyncio.Event()

    async def fail(call):
        retry_entered.set()
        await retry_release.wait()
        raise RuntimeError("transient")

    model = ScriptedModel(
        [
            [function_call("work", "{}", call_id="work-1")],
            ModelStep.respond(fail),
            [assistant_message("done")],
            [assistant_message("revised")],
        ]
    )
    agent.model = model
    inbox = RunInbox()
    config = RunConfig(
        tracing_disabled=True,
        model_settings=ModelSettings(
            retry=ModelRetrySettings(
                max_retries=1,
                backoff={"initial_delay": 0},
                policy=lambda _: True,
            )
        ),
    )
    task = asyncio.create_task(drive(streamed, agent, inbox=inbox, run_config=config))
    try:
        await wait(entered)
        first = inbox.steer("first correction")
        release.set()
        await wait(retry_entered)
        assert first.status == "submitted"
        second = inbox.steer("during retry")
        retry_release.set()
        await asyncio.wait_for(task, 3)
        assert (await first.wait()).status == "submitted"
        outcome = await asyncio.wait_for(second.wait(), 3)
        assert outcome.status == "submitted" and outcome.turn_number == 3
        assert model.calls[1].input == model.calls[2].input
        assert messages(model.calls[2].input).count("first correction") == 1
        assert "during retry" not in messages(model.calls[2].input)
        assert messages(model.calls[3].input) == ["start", "first correction", "during retry"]
    finally:
        await finish(task, release, retry_release)


@pytest.mark.parametrize("streamed", [False, True])
async def test_steer_after_filter_cutoff_waits_for_later_request(streamed: bool) -> None:
    agent, model, entered, release, _ = gate_tool()
    model.enqueue([assistant_message("revised")])
    filtering, allow_filter = asyncio.Event(), asyncio.Event()
    count = 0

    async def filter_input(data):
        nonlocal count
        count += 1
        if count == 2:
            filtering.set()
            await allow_filter.wait()
        return data.model_data

    inbox = RunInbox()
    task = asyncio.create_task(
        drive(
            streamed,
            agent,
            inbox=inbox,
            run_config=RunConfig(call_model_input_filter=filter_input, tracing_disabled=True),
        )
    )
    try:
        await wait(entered)
        first = inbox.steer("before cutoff")
        release.set()
        await wait(filtering)
        second = inbox.steer("after cutoff")
        assert first.status == second.status == "queued"
        allow_filter.set()
        await asyncio.wait_for(task, 3)
        assert (await first.wait()).status == "submitted"
        outcome = await asyncio.wait_for(second.wait(), 3)
        assert outcome.status == "submitted" and outcome.turn_number == 3
        assert messages(model.calls[1].input) == ["start", "before cutoff"]
        assert messages(model.calls[2].input) == ["start", "before cutoff", "after cutoff"]
    finally:
        await finish(task, release, allow_filter)


@pytest.mark.parametrize("streamed", [False, True])
async def test_local_session_retains_one_admitted_occurrence(streamed: bool) -> None:
    agent, model, entered, release, _ = gate_tool()
    session, inbox = SimpleListSession(), RunInbox()
    task = asyncio.create_task(drive(streamed, agent, inbox=inbox, session=session))
    try:
        await wait(entered)
        receipt = inbox.steer("stored correction")
        release.set()
        await asyncio.wait_for(task, 3)
        outcome = await receipt.wait()
        assert outcome.status == "submitted" and outcome.retained is True
        assert messages(await session.get_items()).count("stored correction") == 1
        model.enqueue([assistant_message("followup")])
        await drive(streamed, agent, "follow up", session=session)
        assert messages(model.calls[-1].input).count("stored correction") == 1
        assert messages(await session.get_items()).count("stored correction") == 1
    finally:
        await finish(task, release)


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("server_managed", [False, True])
async def test_handoff_keeps_correction_in_same_run_for_destination(
    streamed: bool,
    server_managed: bool,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def transfer(call):
        entered.set()
        await release.wait()
        return [function_call("transfer_to_destination", "{}", call_id="handoff")]

    first = ScriptedModel([ModelStep.respond(transfer)])
    second = ScriptedModel([[assistant_message("destination")]])
    destination = Agent(name="destination", model=second)
    source = Agent(name="source", model=first, handoffs=[destination])
    inbox = RunInbox()
    task = asyncio.create_task(
        drive(streamed, source, inbox=inbox, auto_previous_response_id=server_managed)
    )
    try:
        await wait(entered)
        receipt = inbox.steer("destination correction")
        release.set()
        assert (await asyncio.wait_for(task, 3)).final_output == "destination"
        outcome = await receipt.wait()
        assert outcome.status == "submitted" and outcome.turn_number == 2
        assert messages(second.calls[0].input)[-1] == "destination correction"
        assert len(first.calls) == len(second.calls) == 1
    finally:
        await finish(task, release)


@pytest.mark.parametrize("streamed", [False, True])
async def test_redaction_precedes_live_guardrails_filter_and_persistence(
    streamed: bool,
) -> None:
    agent, model, entered, release, _ = gate_tool()
    guard_inputs, filter_inputs = [], []

    async def guard(context, current_agent, value):
        guard_inputs.append(value if isinstance(value, str) else messages(value))
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    def filter_input(data):
        filter_inputs.extend(messages(data.model_data.input))
        return data.model_data

    session, inbox = SimpleListSession(), RunInbox()
    config = RunConfig(
        tracing_disabled=True,
        redact_text=lambda value: value.replace("synthetic-secret", "[removed]"),
        input_guardrails=[InputGuardrail(guardrail_function=guard, run_in_parallel=True)],
        call_model_input_filter=filter_input,
    )
    task = asyncio.create_task(
        drive(streamed, agent, inbox=inbox, session=session, run_config=config)
    )
    try:
        await wait(entered)
        receipt = inbox.steer("Explain synthetic-secret")
        assert "synthetic-secret" not in repr(receipt)
        release.set()
        await asyncio.wait_for(task, 3)
        outcome = await receipt.wait()
        assert outcome.status == "submitted"
        assert "synthetic-secret" not in repr(outcome)
        assert "synthetic-secret" not in repr(guard_inputs)
        assert "Explain [removed]" in filter_inputs
        assert "Explain [removed]" in messages(model.calls[1].input)
        assert "synthetic-secret" not in repr(await session.get_items())
    finally:
        await finish(task, release)


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("drop", [False, True], ids=["sent-delta", "filtered-delta"])
async def test_server_managed_delta_commits_only_sent_live_occurrence(
    streamed: bool, drop: bool
) -> None:
    agent, _, entered, release, _ = gate_tool()
    model = ScriptedModel(
        [
            [function_call("work", "{}", call_id="first")],
            [function_call("work", "{}", call_id="second")],
            [assistant_message("done")],
        ]
    )
    agent.model = model
    filter_calls = 0

    def filter_input(data):
        nonlocal filter_calls
        filter_calls += 1
        if drop and filter_calls == 2:
            data.model_data.input = [
                item for item in data.model_data.input if message_text(item) != "server correction"
            ]
        return data.model_data

    inbox = RunInbox()
    task = asyncio.create_task(
        drive(
            streamed,
            agent,
            inbox=inbox,
            auto_previous_response_id=True,
            run_config=RunConfig(call_model_input_filter=filter_input, tracing_disabled=True),
        )
    )
    try:
        await wait(entered)
        receipt = inbox.steer("server correction")
        release.set()
        result = await asyncio.wait_for(task, 3)
        outcome = await receipt.wait()
        assert outcome.status == ("not_submitted" if drop else "submitted")
        assert messages(model.calls[1].input).count("server correction") == int(not drop)
        assert messages(model.calls[2].input).count("server correction") == int(drop)
        assert "start" not in messages(model.calls[1].input)
        assert result.to_state().pending_input == []
        assert len(model.calls) == 3
    finally:
        await finish(task, release)


@pytest.mark.parametrize("streamed", [False, True])
async def test_server_filtered_input_survives_approval_state_roundtrip(
    streamed: bool,
) -> None:
    agent, _, entered, release, effects = gate_tool()

    @tool(needs_approval=True)
    def protected() -> str:
        effects.append("protected")
        return "approved"

    agent.tools.append(protected)
    model = ScriptedModel(
        [
            [function_call("work", "{}", call_id="first")],
            [function_call("protected", "{}", call_id="protected")],
            [assistant_message("done")],
        ]
    )
    agent.model = model

    def omit_correction(data):
        data.model_data.input = [
            item for item in data.model_data.input if message_text(item) != "resume correction"
        ]
        return data.model_data

    inbox = RunInbox()
    task = asyncio.create_task(
        drive(
            streamed,
            agent,
            inbox=inbox,
            auto_previous_response_id=True,
            run_config=RunConfig(call_model_input_filter=omit_correction, tracing_disabled=True),
        )
    )
    try:
        await wait(entered)
        receipt = inbox.steer("resume correction")
        release.set()
        paused = await asyncio.wait_for(task, 3)
        assert len(paused.interruptions) == 1 and effects == ["worked"]
        outcome = await receipt.wait()
        assert outcome.status == "not_submitted" and outcome.retained is True
        state = await RunState.from_json(agent, paused.to_state().to_json())
        assert messages(state.pending_input) == ["resume correction"]
        state.approve(state.get_interruptions()[0])
        resumed = await drive(streamed, agent, state, inbox=RunInbox())
        assert resumed.final_output == "done" and effects == ["worked", "protected"]
        assert messages(model.calls[2].input).count("resume correction") == 1
        assert state.pending_input == []
    finally:
        await finish(task, release)


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("serialize", [False, True], ids=["in-memory", "json"])
async def test_approved_tool_guardrail_checks_follow_up_after_resume(
    streamed: bool, serialize: bool
) -> None:
    inbox = RunInbox()
    checked: list[list[str]] = []
    effects: list[str] = []

    def first_answer(call):
        inbox.queue("Review the private draft.")
        return [assistant_message("Public draft reviewed.")]

    @tool_input_guardrail
    def check_publication(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        task = messages(data.context.turn_input)
        checked.append(task)
        if task == ["Review the public draft."]:
            return ToolGuardrailFunctionOutput.allow()
        return ToolGuardrailFunctionOutput.reject_content("Publication blocked.")

    @function_tool(needs_approval=True, tool_input_guardrails=[check_publication])
    async def publish() -> str:
        effects.append("published")
        return "Published."

    model = ScriptedModel(
        [
            ModelStep.respond(first_answer),
            [function_call("publish", "{}", call_id="publish-draft")],
            [assistant_message("Review complete.")],
        ]
    )
    agent = Agent(name="editor", model=model, tools=[publish])
    paused = await drive(streamed, agent, "Review the public draft.", inbox=inbox)
    assert len(paused.interruptions) == 1
    assert checked == effects == []
    state = paused.to_state()
    if serialize:
        state = await RunState.from_string(agent, state.to_string())
    state.approve(state.get_interruptions()[0])
    await drive(streamed, agent, state)

    assert checked == [["Review the private draft."]]
    assert effects == []
    assert model.calls[-1].input[-1]["output"] == "Publication blocked."


@pytest.mark.parametrize(
    "invalid",
    [
        "missing-field-generated",
        "missing-field-pending",
        "missing-item-id",
        "unknown-id",
        "ambiguous-id",
        "duplicate-id",
        "duplicate-position",
        "boolean-position",
        "negative-position",
        "out-of-range-position",
        "history-missing-items",
        "history-empty",
        "history-negative",
        "history-boolean",
        "history-duplicate",
        "history-out-of-range",
        "history-unsorted",
        "history-non-user",
        "generated-non-user",
        "pending-non-user",
        "non-list",
        "older-schema",
    ],
)
async def test_approval_rejects_invalid_task_input_refs(invalid: str) -> None:
    inbox = RunInbox()
    effects: list[str] = []

    def first_answer(call):
        inbox.steer("Review the private draft.")
        inbox.steer("Do not publish it.")
        return [assistant_message("Public draft reviewed.")]

    @function_tool(needs_approval=True)
    async def publish() -> str:
        effects.append("published")
        return "Published."

    model = ScriptedModel()
    if invalid != "missing-field-pending":
        model.enqueue(ModelStep.respond(first_answer))
    model.enqueue([function_call("publish", "{}", call_id="publish-draft")])
    agent = Agent(name="editor", model=model, tools=[publish])
    paused = await drive(False, agent, "Review the public draft.", inbox=inbox)
    state = paused.to_state()
    state.approve(state.get_interruptions()[0])
    if invalid != "missing-field-generated":
        state.add_input("One more instruction.")
    payload = state.to_json()
    if invalid.startswith("missing-field"):
        payload.pop("task_input_refs", None)
        with pytest.raises(UserError):
            await RunState.from_json(agent, payload)
        assert effects == []
        return
    first, second = [
        item["input_id"] for item in payload["generated_items"] if item["type"] == "input_item"
    ]
    if invalid in {"missing-item-id", "ambiguous-id"}:
        for history in ("generated_items", "session_items"):
            for item in payload[history]:
                if item.get("input_id") == second:
                    if invalid == "missing-item-id":
                        del item["input_id"]
                    else:
                        item["input_id"] = first
        payload["task_input_refs"] = [second if invalid == "missing-item-id" else first]
    elif invalid == "generated-non-user":
        for history in ("generated_items", "session_items"):
            for item in payload[history]:
                if item.get("input_id") == first:
                    item["raw_item"]["role"] = "assistant"
    elif invalid == "pending-non-user":
        payload["pending_input"][0]["role"] = "assistant"
        payload["task_input_refs"] = [0]
    elif invalid == "older-schema":
        payload["$schemaVersion"] = "1.26"
        payload.pop("task_input_refs", None)
        # The intermediate writer omitted follow-up identity. It cannot resume safely.
        with pytest.raises(UserError, match="schema version is not supported"):
            await RunState.from_json(agent, payload)
        payload["$schemaVersion"] = "1.25"
        # The older label alone is valid. It cannot carry a newer task selection.
        await RunState.from_json(agent, payload)
        payload["task_input_refs"] = [first, second]
    elif invalid.startswith("history-"):
        payload["original_input"] = [
            {"role": "user", "content": "Review the public draft."},
            {"role": "assistant", "content": "Which section?"},
            {"role": "user", "content": "The introduction."},
        ]
        positions = {
            "history-missing-items": [0],
            "history-empty": [],
            "history-negative": [-1],
            "history-boolean": [False],
            "history-duplicate": [0, 0],
            "history-out-of-range": [3],
            "history-unsorted": [2, 0],
            "history-non-user": [1],
        }[invalid]
        payload["task_input_refs"] = {"history": positions, "items": []}
        if invalid == "history-missing-items":
            del payload["task_input_refs"]["items"]
    else:
        payload["task_input_refs"] = {
            "unknown-id": ["missing"],
            "duplicate-id": [first, first],
            "duplicate-position": [0, 0],
            "boolean-position": [False],
            "negative-position": [-1],
            "out-of-range-position": [1],
            "non-list": first,
        }[invalid]

    with pytest.raises(UserError):
        await RunState.from_json(agent, payload)
    assert effects == []


@pytest.mark.parametrize("resume_streamed", [False, True])
@pytest.mark.parametrize("replace_pending", [False, True], ids=["retained", "replaced"])
@pytest.mark.parametrize("serialized", [False, True])
async def test_follow_up_survives_session_append_failure(
    resume_streamed: bool, replace_pending: bool, serialized: bool
) -> None:
    class Session(SimpleListSession):
        failed = False

        async def add_items(self, items):
            if not self.failed and "second task" in messages(items):
                self.failed = True
                raise RuntimeError("follow-up append failed")
            await super().add_items(items)

    effects: list[str] = []

    def enabled(ctx, agent):
        return messages(ctx.turn_input) == ["first task"]

    @function_tool(is_enabled=enabled)
    def first_only() -> str:
        effects.append("ran")
        return "done"

    def respond_to_resume(call):
        if call.tools:
            return [function_call("first_only", "{}", call_id="first-only")]
        return [assistant_message("done")]

    session = Session()
    model = ScriptedModel(
        [[assistant_message("first answer")], ModelStep.respond(respond_to_resume)]
    )
    agent = Agent(
        name="worker",
        model=model,
        instructions=lambda ctx, agent: " | ".join(messages(ctx.turn_input)),
        tools=[first_only],
        tool_use_behavior="stop_on_first_tool",
    )
    stream = Runner.run_streamed(
        agent, "first task", session=session, run_config=RunConfig(tracing_disabled=True)
    )
    receipt = stream.queue("second task")
    with pytest.raises(RuntimeError, match="follow-up append failed"):
        async for _ in stream.stream_events():
            pass
    outcome = await receipt.wait()
    assert outcome.status == "not_submitted" and outcome.retained
    assert len(model.calls) == 1 and model.calls[0].tools
    assert messages(await session.get_items()) == ["first task"]

    state = stream.to_state()
    if serialized:
        state = await RunState.from_string(agent, state.to_string())
    assert messages(state.pending_input) == ["second task"]
    if replace_pending:
        state.clear_pending_input()
        if serialized:
            state = await RunState.from_json(agent, state.to_json())
        state.add_input("replacement")
    expected = "replacement" if replace_pending else "second task"

    result = await drive(resume_streamed, agent, state, session=session)
    assert result.final_output == "done"
    assert len(model.calls) == 2
    request = model.calls[1]
    assert messages(request.input) == ["first task", expected]
    assert messages(await session.get_items()) == ["first task", expected]
    assert not effects
    assert not request.tools
    assert request.system_instructions == expected
    assert await receipt.wait() == outcome


@pytest.mark.parametrize("resume_streamed", [False, True])
@pytest.mark.parametrize("clear_pending", [False, True], ids=["retained", "cleared"])
async def test_resume_after_stream_filter_failure_respects_pending_task_input(
    resume_streamed: bool,
    clear_pending: bool,
) -> None:
    effects: list[str] = []

    def enabled(ctx, agent):
        return "second task" in messages(ctx.turn_input)

    @function_tool(is_enabled=enabled)
    def restricted() -> str:
        effects.append("ran")
        return "done"

    def fail_before_dispatch(data):
        if "second task" in messages(data.model_data.input):
            raise RuntimeError("filter failed")
        return data.model_data

    def respond_to_resume(call):
        if call.tools:
            return [function_call("restricted", "{}", call_id="restricted-1")]
        return [assistant_message("done")]

    model = ScriptedModel(
        [[assistant_message("first answer")], ModelStep.respond(respond_to_resume)]
    )
    agent = Agent(
        name="worker",
        model=model,
        instructions=lambda ctx, agent: " | ".join(messages(ctx.turn_input)),
        tools=[restricted],
        tool_use_behavior="stop_on_first_tool",
    )
    stream = Runner.run_streamed(
        agent,
        "first task",
        auto_previous_response_id=True,
        run_config=RunConfig(call_model_input_filter=fail_before_dispatch, tracing_disabled=True),
    )
    receipt = stream.queue("second task")
    with pytest.raises(RuntimeError, match="filter failed"):
        async for _ in stream.stream_events():
            pass
    assert len(model.calls) == 1
    outcome = await receipt.wait()
    assert outcome.status == "not_submitted" and outcome.retained

    state = stream.to_state()
    assert messages(state.pending_input) == ["second task"]
    if clear_pending:
        state.clear_pending_input()
        state.add_input("replacement")

    result = await drive(resume_streamed, agent, state)
    assert result.final_output == "done"
    assert len(model.calls) == 2
    request = model.calls[1]
    assert messages(request.input) == (["replacement"] if clear_pending else ["second task"])
    assert effects == ([] if clear_pending else ["ran"])
    assert bool(request.tools) == (not clear_pending)
    assert request.system_instructions == ("replacement" if clear_pending else "second task")


@pytest.mark.parametrize("needs_approval", [False, True], ids=["fresh", "resumed"])
@pytest.mark.parametrize("serialize", [False, True], ids=["in-memory", "json"])
async def test_policy_continuation_preserves_tool_guardrails_after_stream_recovery(
    needs_approval: bool, serialize: bool
) -> None:
    inbox = RunInbox()
    effects: list[str] = []

    @tool_input_guardrail
    def check_input(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.allow(output_info=data.context.tool_call_id)

    @tool_output_guardrail
    def check_output(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
        return ToolGuardrailFunctionOutput.allow(output_info=data.context.tool_call_id)

    @function_tool(
        needs_approval=needs_approval,
        tool_input_guardrails=[check_input],
        tool_output_guardrails=[check_output],
    )
    async def work() -> str:
        effects.append("worked")
        inbox.steer("Use the result.")
        return "value"

    model = ScriptedModel(
        [[function_call("work", "{}", call_id="work-1")], [assistant_message("done")]]
    )

    class Hooks(RunHooks[Any]):
        async def on_llm_end(
            self, context: RunContextWrapper[Any], agent: Agent[Any], response: ModelResponse
        ) -> None:
            if len(model.calls) == 2:
                raise RuntimeError("after acceptance")

    agent = Agent(
        name="worker",
        model=model,
        tools=[work],
        tool_use_behavior="stop_on_first_tool",
        hooks=AgentHooks(
            final_output_policy=lambda ctx, proposal: (
                "continue" if proposal.kind == "tool" else "finish"
            )
        ),
    )
    config = RunConfig(tracing_disabled=True)
    value: str | RunState[Any] = "start"
    if needs_approval:
        paused = await Runner.run(agent, value, auto_previous_response_id=True, run_config=config)
        value = paused.to_state()
        value.approve(value.get_interruptions()[0])

    failed = Runner.run_streamed(
        agent, value, auto_previous_response_id=True, hooks=Hooks(), inbox=inbox, run_config=config
    )
    with pytest.raises(RuntimeError, match="after acceptance"):
        async for _ in failed.stream_events():
            pass

    state = failed.to_state()
    if serialize:
        state = await RunState.from_string(agent, state.to_string())
    recovered = await Runner.run(agent, state, run_config=config)

    assert recovered.final_output == "done"
    assert len(model.calls) == 2
    assert effects == ["worked"]
    for result in (failed, recovered):
        assert [r.output.output_info for r in result.tool_input_guardrail_results] == ["work-1"]
        assert [r.output.output_info for r in result.tool_output_guardrail_results] == ["work-1"]


@pytest.mark.parametrize("streamed", [False, True])
async def test_failed_server_request_preserves_dispatch_receipt(streamed: bool) -> None:
    agent, _, entered, release, _ = gate_tool()
    model = ScriptedModel(
        [
            [function_call("work", "{}", call_id="first")],
            RuntimeError("request failed"),
        ]
    )
    agent.model = model
    inbox = RunInbox()
    task = asyncio.create_task(drive(streamed, agent, inbox=inbox, auto_previous_response_id=True))
    try:
        await wait(entered)
        receipt = inbox.steer("uncertain server acceptance")
        queued = inbox.queue("skip after failure")
        release.set()
        with pytest.raises(RuntimeError, match="request failed"):
            await asyncio.wait_for(task, 3)
        outcome = await asyncio.wait_for(receipt.wait(), 3)
        assert outcome.status == "submitted" and outcome.turn_number == 2
        assert (await asyncio.wait_for(queued.wait(), 3)).status == "not_submitted"
        assert messages(model.calls[1].input).count("uncertain server acceptance") == 1
        assert len(model.calls) == 2
    finally:
        await finish(task, release)


@pytest.mark.parametrize("streamed", [False, True])
async def test_selector_failure_after_local_save_retains_one_recoverable_occurrence(
    streamed: bool,
) -> None:
    agent, model, entered, release, _ = gate_tool()

    class Provider(ModelProvider):
        def get_model(self, name):
            return model

    def select(data):
        if data.turn_number == 2:
            raise RuntimeError("selector failed")

    agent.model = "configured"
    session, inbox = SimpleListSession(), RunInbox()
    task = asyncio.create_task(
        drive(
            streamed,
            agent,
            inbox=inbox,
            session=session,
            run_config=RunConfig(
                model_provider=Provider(), model_selector=select, tracing_disabled=True
            ),
        )
    )
    try:
        await wait(entered)
        receipt = inbox.steer("recoverable correction")
        release.set()
        with pytest.raises(RuntimeError, match="selector failed"):
            await asyncio.wait_for(task, 3)
        outcome = await asyncio.wait_for(receipt.wait(), 3)
        assert outcome.status == "not_submitted" and outcome.retained is True
        assert len(model.calls) == 1
        assert messages(await session.get_items()).count("recoverable correction") == 1
        await drive(
            streamed,
            agent,
            "follow up",
            session=session,
            run_config=RunConfig(model_provider=Provider(), tracing_disabled=True),
        )
        assert messages(model.calls[-1].input).count("recoverable correction") == 1
        assert messages(await session.get_items()).count("recoverable correction") == 1
    finally:
        await finish(task, release)


@pytest.mark.parametrize("streamed", [False, True])
async def test_steers_take_priority_and_each_queued_task_finishes_before_the_next(
    streamed: bool,
) -> None:
    inbox = RunInbox()
    session = SimpleListSession()
    receipts: list[MessageReceipt] = []
    guarded: list[str] = []

    def first_response(call):
        receipts.extend(
            [
                inbox.queue("first task"),
                inbox.steer("shorter"),
                inbox.queue("second task"),
                inbox.steer("in Spanish"),
            ]
        )
        return [assistant_message("initial answer")]

    @tool
    def work() -> str:
        return "first task tool result"

    @output_guardrail
    def check_output(ctx, agent, output):
        guarded.append(output)
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    model = ScriptedModel(
        [
            ModelStep.respond(first_response),
            [assistant_message("revised answer")],
            [function_call("work", "{}", call_id="first-task")],
            [assistant_message("first answer")],
            [assistant_message("second answer")],
        ]
    )
    agent = Agent(name="worker", model=model, tools=[work], output_guardrails=[check_output])
    result = await drive(streamed, agent, inbox=inbox, session=session)

    steered = ["start", "shorter", "in Spanish"]
    assert [messages(call.input) for call in model.calls] == [
        ["start"],
        steered,
        [*steered, "first task"],
        [*steered, "first task"],
        [*steered, "first task", "second task"],
    ]
    assert model.calls[3].input[-1]["output"] == "first task tool result"
    assert result.final_output == "second answer"
    assert guarded == ["initial answer", "revised answer", "first answer", "second answer"]
    history = result.to_input_list()
    for items in (history, await session.get_items()):
        assert messages(items) == [*steered, "first task", "second task"]
        assert [
            message_text(item, "assistant") for item in items if item.get("role") == "assistant"
        ] == [
            "initial answer",
            "revised answer",
            "first answer",
            "second answer",
        ]
    outcomes = await asyncio.wait_for(asyncio.gather(*(r.wait() for r in receipts)), 3)
    assert [(o.status, o.turn_number) for o in outcomes] == [
        ("submitted", 3),
        ("submitted", 2),
        ("submitted", 5),
        ("submitted", 2),
    ]


@pytest.mark.parametrize("streamed", [False, True])
async def test_queued_tasks_share_the_turn_limit_and_preserve_the_last_answer(
    streamed: bool,
) -> None:
    inbox = RunInbox()
    receipts: list[MessageReceipt] = []

    def respond(call):
        receipts.extend([inbox.queue("within budget"), inbox.queue("over budget")])
        return [assistant_message("first")]

    model = ScriptedModel([ModelStep.respond(respond), [assistant_message("last")]])
    result = await drive(streamed, Agent(name="limited", model=model), inbox=inbox, max_turns=2)
    assert result.final_output == "last"
    assert len(model.calls) == 2
    assert messages(result.to_input_list()) == ["start", "within budget"]
    first, skipped = await asyncio.wait_for(asyncio.gather(*(r.wait() for r in receipts)), 3)
    assert first.status == "submitted" and first.turn_number == 2
    assert skipped.status == "not_submitted" and not skipped.retained


@pytest.mark.parametrize("streamed", [False, True])
async def test_output_guardrail_can_stop_a_queued_followup(streamed: bool) -> None:
    inbox = RunInbox()
    receipts: list[MessageReceipt] = []

    @output_guardrail
    def reject(ctx, agent, output):
        receipts.append(inbox.queue("never start"))
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    model = ScriptedModel([[assistant_message("blocked")]])
    agent = Agent(name="guarded", model=model, output_guardrails=[reject])
    with pytest.raises(OutputGuardrailTripwireTriggered):
        await drive(streamed, agent, inbox=inbox)
    assert len(model.calls) == 1
    outcome = await asyncio.wait_for(receipts[0].wait(), 3)
    assert outcome.status == "not_submitted" and not outcome.retained


@pytest.mark.parametrize("explicit_inbox", [False, True])
async def test_stream_accepts_immediate_input_and_emits_each_answer(explicit_inbox: bool) -> None:
    inbox = RunInbox() if explicit_inbox else None
    answers = ["original", "revised", "followup"]
    model = ScriptedModel([[assistant_message(answer)] for answer in answers])
    result = Runner.run_streamed(
        Agent(name="stream", model=model),
        "start",
        inbox=inbox,
        run_config=RunConfig(tracing_disabled=True),
    )
    try:
        queued = (inbox or result).queue("next task")
        steer = result.steer("revise")
        assert queued.status == steer.status == "queued"
        seen = []
        async for event in result.stream_events():
            if event.type == "run_item_stream_event" and isinstance(event.item, MessageOutputItem):
                seen.append(ItemHelpers.text_message_output(event.item))
        assert seen == answers
        assert result.final_output == "followup"
        assert [messages(call.input) for call in model.calls] == [
            ["start"],
            ["start", "revise"],
            ["start", "revise", "next task"],
        ]
        submitted = await asyncio.wait_for(asyncio.gather(steer.wait(), queued.wait()), 3)
        assert [outcome.status for outcome in submitted] == ["submitted", "submitted"]
        with pytest.raises(UserError):
            result.queue("too late")
    finally:
        if not result.is_complete:
            result.cancel()


async def test_immediate_stream_cancellation_settles_receipts_before_first_request() -> None:
    model = ScriptedModel()
    inbox = RunInbox()
    result = Runner.run_streamed(
        Agent(name="stream", model=model),
        "start",
        inbox=inbox,
        run_config=RunConfig(tracing_disabled=True),
    )
    steer = result.steer("revise")
    queued = result.queue("next task")
    result.cancel()
    try:
        with pytest.raises(UserError):
            result.steer("after cancel")
        with pytest.raises(UserError):
            inbox.queue("after cancel")
    finally:
        async for _ in result.stream_events():
            pass
    outcomes = await asyncio.wait_for(asyncio.gather(steer.wait(), queued.wait()), 3)
    assert [outcome.status for outcome in outcomes] == ["not_submitted", "not_submitted"]
    assert len(model.calls) == 0
    with pytest.raises(UserError):
        result.steer("too late")


@pytest.mark.parametrize("streamed", [False, True])
async def test_handled_refusal_stops_queued_work(streamed: bool) -> None:
    inbox = RunInbox()
    receipts: list[MessageReceipt] = []

    def refuse(call):
        receipts.append(inbox.queue("never start"))
        return [get_refusal_message("Cannot complete this task.")]

    ended: list[str] = []

    class Hooks(AgentHooks):
        async def on_end(self, context, agent, output):
            ended.append(output)
            with pytest.raises(UserError, match="not accepting"):
                inbox.steer("too late")
            with pytest.raises(UserError, match="not accepting"):
                inbox.queue("too late")

    model = ScriptedModel([ModelStep.respond(refuse)])
    result = await drive(
        streamed,
        Agent(name="worker", model=model, hooks=Hooks()),
        inbox=inbox,
        error_handlers={"model_refusal": lambda data: "safe fallback"},
    )
    assert result.final_output == "safe fallback"
    assert ended == ["safe fallback"]
    assert len(model.calls) == 1
    assert messages(result.to_input_list()) == ["start"]
    outcome = await asyncio.wait_for(receipts[0].wait(), 3)
    assert outcome.status == "not_submitted" and not outcome.retained


async def test_cancel_after_turn_finishes_the_tool_without_starting_more_work() -> None:
    agent, model, entered, release, effects = gate_tool()
    result = Runner.run_streamed(agent, "start", run_config=RunConfig(tracing_disabled=True))
    try:
        await wait(entered)
        steer = result.steer("revise")
        queued = result.queue("next task")
        result.cancel(mode="after_turn")
        release.set()
        async for _ in result.stream_events():
            pass
        assert effects == ["worked"]
        assert len(model.calls) == 1
        outcomes = await asyncio.wait_for(asyncio.gather(steer.wait(), queued.wait()), 3)
        assert [outcome.status for outcome in outcomes] == ["not_submitted", "not_submitted"]
    finally:
        release.set()
        if not result.is_complete:
            result.cancel()


async def test_cancel_after_turn_from_final_message_preserves_answer_and_skips_queue() -> None:
    saved = asyncio.Event()

    # Hold the consumer until the first answer is durable so it can cancel at the turn boundary.
    class Session(SimpleListSession):
        async def add_items(self, items):
            await super().add_items(items)
            if any(item.get("role") == "assistant" for item in items):
                saved.set()

    model = ScriptedModel([[assistant_message("first answer")]])
    result = Runner.run_streamed(
        Agent(name="stream", model=model),
        "start",
        session=Session(),
        run_config=RunConfig(tracing_disabled=True),
    )
    queued = result.queue("next task")
    seen = []
    async for event in result.stream_events():
        if event.type == "run_item_stream_event" and isinstance(event.item, MessageOutputItem):
            seen.append(ItemHelpers.text_message_output(event.item))
            await wait(saved)
            result.cancel(mode="after_turn")
    assert seen == ["first answer"]
    assert result.final_output == "first answer"
    assert len(model.calls) == 1
    assert messages(result.to_input_list()) == ["start"]
    outcome = await asyncio.wait_for(queued.wait(), 3)
    assert outcome.status == "not_submitted" and not outcome.retained


@pytest.mark.parametrize("streamed", [False, True])
async def test_queued_tasks_send_only_new_input_with_the_previous_response_id(
    streamed: bool,
) -> None:
    inbox = RunInbox()
    receipts: list[MessageReceipt] = []

    def first_response(call):
        receipts.extend([inbox.queue("second task"), inbox.queue("third task")])
        return ModelStep(output=[assistant_message("first answer")], response_id="resp-first")

    model = ScriptedModel(
        [
            ModelStep.respond(first_response),
            ModelStep(output=[assistant_message("second answer")], response_id="resp-second"),
            ModelStep(output=[assistant_message("third answer")], response_id="resp-third"),
        ]
    )
    result = await drive(
        streamed, Agent(name="worker", model=model), inbox=inbox, auto_previous_response_id=True
    )
    first, second, third = model.calls
    assert first.previous_response_id is None
    for call, previous_id, text in (
        (second, "resp-first", "second task"),
        (third, "resp-second", "third task"),
    ):
        assert call.previous_response_id == previous_id
        (item,) = call.input
        assert message_text(item) == text
    assert [receipt.status for receipt in receipts] == ["submitted", "submitted"]
    assert result.final_output == "third answer"
    assert result.previous_outputs == ("first answer", "second answer")
    assert result.last_response_id == "resp-third"
    assert result.to_state().pending_input == []


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize(
    "feedback, remove, status",
    [
        ("policy feedback", None, "submitted"),
        ("correction", "steer", "not_submitted"),
        ("correction", "feedback", "submitted"),
    ],
)
async def test_policy_feedback_does_not_own_the_steering_receipt(
    streamed: bool,
    feedback: str,
    remove: str | None,
    status: str,
) -> None:
    inbox = RunInbox()
    receipts: list[MessageReceipt] = []

    def draft(call):
        receipts.append(inbox.steer("correction"))
        return [assistant_message("draft")]

    def filter_input(data):
        items = data.model_data.input
        if len(items) > 1:
            assert messages(items) == ["start", "correction", feedback]
            if remove is not None:
                items.pop(-2 if remove == "steer" else -1)
        return data.model_data

    model = ScriptedModel([ModelStep.respond(draft), [assistant_message("done")]])
    agent = Agent(
        name="worker",
        model=model,
        hooks=AgentHooks(
            final_output_policy=lambda ctx, proposal: (
                ContinueWithFeedback(feedback) if proposal.turn == 1 else "finish"
            )
        ),
    )
    await drive(
        streamed,
        agent,
        inbox=inbox,
        run_config=RunConfig(
            tracing_disabled=True, call_model_input_filter=filter_input if remove else None
        ),
    )
    expected = ["start", "correction"]
    if remove is None:
        expected.append(feedback)
    assert messages(model.calls[1].input) == expected
    assert receipts[0].status == status


async def test_failed_stream_task_creation_closes_the_inbox() -> None:
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    inbox = RunInbox()

    # Exercise the synchronous failure before the stream producer starts.
    def reject(loop, coro, **kwargs):
        raise RuntimeError("task factory rejected the producer")

    loop.set_task_factory(reject)
    try:
        with pytest.raises(RuntimeError, match="task factory rejected the producer"):
            Runner.run_streamed(
                Agent(name="worker", model=ScriptedModel()),
                "start",
                inbox=inbox,
                run_config=RunConfig(tracing_disabled=True),
            )
    finally:
        loop.set_task_factory(previous_factory)
    with pytest.raises(UserError):
        inbox.steer("no producer")


async def test_sync_agent_runner_rejects_inbox_before_dispatch() -> None:
    model = ScriptedModel([[assistant_message("unexpected")]])
    options: dict[str, Any] = {"inbox": RunInbox(), "run_config": RunConfig(tracing_disabled=True)}
    with pytest.raises(TypeError, match="inbox"):
        await asyncio.to_thread(
            lambda: AgentRunner().run_sync(Agent(name="worker", model=model), "start", **options)
        )
    assert not model.calls


@pytest.mark.parametrize("pause", ["guardrail", "persistence", "guardrail-after-tool"])
async def test_cancel_after_turn_during_message_admission_stops_before_dispatch(pause: str) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    after_tool = pause == "guardrail-after-tool"

    async def suspend():
        entered.set()
        await release.wait()

    async def check_input(ctx, agent, value):
        if pause != "persistence" and isinstance(value, list) and "next" in messages(value):
            await suspend()
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    # Pause the session write after the input guardrail has allowed the follow-up.
    class Session(SimpleListSession):
        async def add_items(self, items):
            if pause == "persistence" and "next" in messages(items):
                await suspend()
            await super().add_items(items)

    @tool
    def work() -> str:
        return "tool result"

    session = Session()
    first = (
        function_call("work", "{}", call_id="work-1")
        if after_tool
        else assistant_message("first answer")
    )
    model = ScriptedModel([[first], [assistant_message("unexpected")]])
    agent = Agent(
        name="worker",
        model=model,
        tools=[work],
        input_guardrails=[InputGuardrail(guardrail_function=check_input)],
    )
    result = Runner.run_streamed(
        agent, "start", session=session, run_config=RunConfig(tracing_disabled=True)
    )
    receipt = result.steer("next") if after_tool else result.queue("next")
    try:
        await wait(entered)
        result.cancel(mode="after_turn")
        release.set()
        async for _ in result.stream_events():
            pass
        assert len(model.calls) == result.current_turn == 1
        assert result.final_output == (None if after_tool else "first answer")
        assert result.previous_outputs == ()
        outcome = await asyncio.wait_for(receipt.wait(), 3)
        assert outcome.status == "not_submitted"
        assert outcome.retained == (pause == "persistence")
        history = await session.get_items()
        assert messages(history) == (["start", "next"] if pause == "persistence" else ["start"])
        if after_tool:
            assert [
                item.get("output") for item in history if item.get("type") == "function_call_output"
            ] == ["tool result"]
            checkpoint = result.to_state().to_json()
            assert [item["type"] for item in checkpoint["generated_items"]] == [
                "tool_call_item",
                "tool_call_output_item",
            ]
    finally:
        release.set()
        if not result.is_complete:
            result.cancel()


async def test_immediate_cancel_during_committed_input_write_never_dispatches() -> None:
    saving, committed = asyncio.Event(), asyncio.Event()
    effects: list[str] = []

    # A session can finish a commit even when its caller requests cancellation.
    class Session(SimpleListSession):
        async def add_items(self, items):
            if "next" in messages(items):
                saving.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await super().add_items(items)
                    committed.set()
                    return
            await super().add_items(items)

    @tool
    def work() -> str:
        effects.append("unexpected")
        return "done"

    session = Session()
    model = ScriptedModel(
        [[assistant_message("first")], [function_call("work", "{}", call_id="unexpected")]]
    )
    result = Runner.run_streamed(
        Agent(name="worker", model=model, tools=[work], tool_use_behavior="stop_on_first_tool"),
        "start",
        session=session,
        run_config=RunConfig(tracing_disabled=True),
    )
    receipt = result.queue("next")
    try:
        await wait(saving)
    finally:
        result.cancel()
        async for _ in result.stream_events():
            pass
    assert committed.is_set()
    assert len(model.calls) == result.current_turn == 1
    assert effects == []
    assert result.previous_outputs == () and result.final_output is None
    assert messages(await session.get_items()) == ["start", "next"]
    outcome = await asyncio.wait_for(receipt.wait(), 3)
    assert (outcome.status, outcome.reason) == ("not_submitted", "cancelled")
    assert outcome.retained


@pytest.mark.parametrize("pause", ["answer_write", "session", "filter"])
@pytest.mark.parametrize("mode", ["propagate", "swallow", "preexisting", "uncancel"])
async def test_awaited_inbox_respects_owner_cancellation(pause: str, mode: str) -> None:
    if mode in ("swallow", "uncancel") and not hasattr(asyncio.Task, "cancelling"):
        pytest.skip("Python 3.10 cannot observe a cancellation swallowed by a callback")
    entered, release = asyncio.Event(), asyncio.Event()
    inbox = RunInbox()
    receipts: list[MessageReceipt] = []

    async def pause_before_dispatch() -> None:
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            if mode == "propagate":
                raise
            if mode == "uncancel":
                uncancel = getattr(asyncio.current_task(), "uncancel", None)
                assert uncancel is not None
                uncancel()

    class Session(SimpleListSession):
        async def add_items(self, items):
            if pause == "session" and "next" in messages(items):
                await pause_before_dispatch()
            if pause == "answer_write" and any(
                message_text(item, role="assistant") == "first" for item in items
            ):
                await pause_before_dispatch()
            await super().add_items(items)

    async def filter_input(data):
        if pause == "filter" and "next" in messages(data.model_data.input):
            await pause_before_dispatch()
        return data.model_data

    def first(call):
        receipts.append(inbox.queue("next"))
        return [assistant_message("first")]

    model = ScriptedModel([ModelStep.respond(first), [assistant_message("second")]])
    session = Session()
    config = RunConfig(tracing_disabled=True, call_model_input_filter=filter_input)
    agent = Agent(name="worker", model=model)
    preparations: list[str] = []
    if pause == "answer_write":
        from agents.sandbox import Capability, SandboxAgent, SandboxRunConfig
        from agents.testing import scripted_sandbox_session

        class ObservePreparation(Capability):
            def process_context(self, context):
                preparations.append("prepared")
                return context

        agent = SandboxAgent(
            name="worker", model=model, capabilities=[ObservePreparation(type="observe")]
        )
        config.sandbox = SandboxRunConfig(session=scripted_sandbox_session())

    async def run():
        if mode != "propagate":
            # The caller handled an earlier cancellation before starting this invocation.
            owner = asyncio.current_task()
            assert owner is not None
            owner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.sleep(0)
        return await Runner.run(
            agent,
            "start",
            inbox=inbox,
            session=session,
            run_config=config,
        )

    task = asyncio.create_task(run())
    try:
        await wait(entered)
        if mode == "preexisting":
            release.set()
        else:
            task.cancel()
        if mode in ("propagate", "swallow"):
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            assert len(model.calls) == 1
        else:
            assert (await asyncio.wait_for(task, 3)).final_output == "second"
            assert len(model.calls) == 2
        outcome = await asyncio.wait_for(receipts[0].wait(), 3)
        if mode in ("propagate", "swallow"):
            assert (outcome.status, outcome.reason) == ("not_submitted", "cancelled")
        else:
            assert outcome.status == "submitted"
        stopped_at_answer = pause == "answer_write" and mode in ("propagate", "swallow")
        assert outcome.retained == (not stopped_at_answer)
        if pause == "answer_write":
            assert len(preparations) == len(model.calls)
            if mode == "swallow":
                assert any(
                    message_text(item, role="assistant") == "first"
                    for item in await session.get_items()
                )
        wrote_input = not stopped_at_answer and (pause == "filter" or mode != "propagate")
        expected = ["start", "next"] if wrote_input else ["start"]
        assert messages(await session.get_items()) == expected
    finally:
        await finish(task, release)


@pytest.mark.parametrize(
    "queued,pause",
    [(False, "model"), (True, "model"), (True, "prepare"), (True, "llm_end")],
)
async def test_immediate_cancel_prevents_effects_from_late_response(
    queued: bool, pause: str
) -> None:
    entered = asyncio.Event()
    effects: list[str] = []
    end_hooks: list[str] = []

    async def finish_despite_cancellation() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass

    @tool
    async def work() -> str:
        effects.append("tool")
        return "unexpected"

    async def transfer(ctx: RunContextWrapper[Any]) -> None:
        effects.append("handoff")

    async def respond(call):
        if pause == "model":
            await finish_despite_cancellation()
        name = "work" if queued else "transfer_to_destination"
        return ModelStep(
            output=[function_call(name, "{}", call_id="late-call")],
            response_id="resp-late",
            usage=Usage(input_tokens=7, total_tokens=7),
        )

    async def prepare(call):
        await finish_despite_cancellation()
        return call.arguments

    class Hooks(AgentHooks):
        async def on_llm_end(self, context, agent, response):
            end_hooks.append(response.response_id)
            if pause == "llm_end" and response.response_id == "resp-late":
                await finish_despite_cancellation()

    model = ScriptedModel()
    if queued:
        model.enqueue(ModelStep(output=[assistant_message("first")], response_id="resp-first"))
    model.enqueue(ModelStep.respond(respond))
    destination = Agent(name="destination", model=ScriptedModel())
    agent = Agent(
        name="worker",
        model=model,
        tools=[work],
        handoffs=[] if queued else [handoff(destination, on_handoff=transfer)],
        hooks=Hooks(),
        tool_use_behavior="stop_on_first_tool",
    )
    result = Runner.run_streamed(
        agent,
        "start",
        auto_previous_response_id=queued,
        run_config=RunConfig(
            tracing_disabled=True,
            tool_execution=ToolExecutionConfig(prepare_tool_input=prepare)
            if pause == "prepare"
            else None,
        ),
    )
    receipt = result.queue("next") if queued else None
    try:
        await wait(entered)
    finally:
        result.cancel()
        async for _ in result.stream_events():
            pass

    assert effects == []
    assert result.final_output is None
    assert result.context_wrapper.usage.total_tokens == 7
    assert len(model.calls) == (2 if queued else 1)
    expected_hooks = ["resp-first"] if queued else []
    if pause == "llm_end":
        expected_hooks.append("resp-late")
    assert end_hooks == expected_hooks
    if receipt is not None:
        assert (await receipt.wait()).status == "submitted"
        assert result.previous_outputs == ("first",)
        state = result.to_state()
        assert state.pending_input == []
        assert [response["response_id"] for response in state.to_json()["model_responses"]] == [
            "resp-first",
            "resp-late",
        ]


@pytest.mark.skipif(not hasattr(asyncio.Task, "cancelling"), reason="Requires Task.cancelling")
async def test_awaited_cancel_prevents_tool_from_late_queued_response() -> None:
    inbox = RunInbox()
    entered = asyncio.Event()
    effects: list[str] = []
    receipts: list[MessageReceipt] = []

    @tool
    async def work() -> str:
        effects.append("tool")
        return "unexpected"

    def first(call):
        receipts.append(inbox.queue("next"))
        return [assistant_message("first")]

    async def late_response(call):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return [function_call("work", "{}", call_id="late-call")]

    model = ScriptedModel([ModelStep.respond(first), ModelStep.respond(late_response)])
    agent = Agent(name="worker", model=model, tools=[work], tool_use_behavior="stop_on_first_tool")
    task = asyncio.create_task(
        Runner.run(agent, "start", inbox=inbox, run_config=RunConfig(tracing_disabled=True))
    )
    try:
        await wait(entered)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert effects == []
        assert (await receipts[0].wait()).status == "submitted"
    finally:
        await finish(task)


async def test_immediate_cancel_during_input_filter_never_dispatches() -> None:
    filtering = asyncio.Event()

    async def filter_input(data):
        if "next" in messages(data.model_data.input):
            filtering.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass
        return data.model_data

    model = ScriptedModel([[assistant_message("first")], [assistant_message("unexpected")]])
    result = Runner.run_streamed(
        Agent(name="worker", model=model),
        "start",
        run_config=RunConfig(tracing_disabled=True, call_model_input_filter=filter_input),
    )
    receipt = result.queue("next")
    try:
        await wait(filtering)
    finally:
        result.cancel()
        async for _ in result.stream_events():
            pass
    assert len(model.calls) == 1
    assert result.previous_outputs == ("first",) and result.final_output is None
    outcome = await asyncio.wait_for(receipt.wait(), 3)
    assert (outcome.status, outcome.reason) == ("not_submitted", "cancelled")
    assert outcome.retained


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("resumed", [False, True])
async def test_approval_pause_closes_intake_before_session_write(
    streamed: bool, resumed: bool
) -> None:
    saving, release = asyncio.Event(), asyncio.Event()
    inbox = RunInbox()
    receipts: list[MessageReceipt] = []

    # Hold the append of the new call, or the approved sibling's output on resume.
    class Session(SimpleListSession):
        async def add_items(self, items):
            target = "function_call_output" if resumed else "function_call"
            if any(item.get("type") == target for item in items):
                saving.set()
                await release.wait()
            await super().add_items(items)

    @tool(needs_approval=True)
    async def protected() -> str:
        receipts.append(inbox.queue("during approved tool"))
        return "worked"

    def first_response(call):
        if not resumed:
            receipts.append(inbox.queue("before approval pause"))
        return [function_call("protected", "{}", call_id=key) for key in ("one", "two")]

    session = Session()
    model = ScriptedModel([ModelStep.respond(first_response)])
    agent = Agent(name="worker", model=model, tools=[protected])
    value: Any = "start"
    if resumed:
        first = await drive(streamed, agent, session=session)
        value = first.to_state()
        value.approve(value.get_interruptions()[0])
    task = asyncio.create_task(drive(streamed, agent, value, inbox=inbox, session=session))
    try:
        await wait(saving)
        with pytest.raises(UserError, match="not accepting input"):
            inbox.steer("during approval persistence")
        release.set()
        result = await asyncio.wait_for(task, 3)
        assert len(result.interruptions) == (1 if resumed else 2)
        assert len(receipts) == 1
        outcome = await asyncio.wait_for(receipts[0].wait(), 3)
        assert (outcome.status, outcome.reason) == ("not_submitted", "approval_pause")
    finally:
        await finish(task, release)


@pytest.mark.parametrize("streamed", [False, True])
async def test_tool_failure_closes_inbox_before_saving_completed_sibling(streamed: bool) -> None:
    finished, saving, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    inbox = RunInbox()
    receipts: list[MessageReceipt] = []

    @function_tool
    async def success() -> str:
        receipts.append(inbox.queue("before failure"))
        return "completed"

    class Hooks(AgentHooks):
        async def on_tool_end(self, context, agent, tool, result):
            if tool.name == "success":
                finished.set()

    @function_tool(failure_error_function=None)
    async def failure() -> str:
        await finished.wait()
        raise ValueError("terminal tool failure")

    class Session(SimpleListSession):
        async def add_items(self, items):
            if any(item.get("type") == "function_call_output" for item in items):
                saving.set()
                await release.wait()
            await super().add_items(items)

    model = ScriptedModel(
        [[function_call("success", {}, call_id="ok"), function_call("failure", {}, call_id="bad")]]
    )
    agent = Agent(name="worker", model=model, tools=[success, failure], hooks=Hooks())
    session = Session()
    config = RunConfig(tracing_disabled=True)
    stream = (
        Runner.run_streamed(agent, "start", inbox=inbox, session=session, run_config=config)
        if streamed
        else None
    )

    async def consume():
        if stream is None:
            await Runner.run(agent, "start", inbox=inbox, session=session, run_config=config)
        else:
            async for _ in stream.stream_events():
                pass

    task = asyncio.create_task(consume())
    try:
        await wait(saving)
        sender = stream or inbox
        with pytest.raises(UserError, match="not accepting input"):
            sender.steer("too late")
        with pytest.raises(UserError, match="not accepting input"):
            sender.queue("too late")
        release.set()
        with pytest.raises(UserError, match="terminal tool failure"):
            await asyncio.wait_for(task, 3)
        assert len(model.calls) == 1
        history = await session.get_items()
        assert [
            item["output"] for item in history if item.get("type") == "function_call_output"
        ] == ["completed"]
        outcome = await asyncio.wait_for(receipts[0].wait(), 3)
        assert (outcome.status, outcome.reason) == ("not_submitted", "run_failed")
    finally:
        await finish(task, release)


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("ending", ["approval", "failure"])
async def test_terminal_run_rejects_messages_while_cleanup_waits(
    streamed: bool, ending: str
) -> None:
    cleaning, release = asyncio.Event(), asyncio.Event()

    # ScriptedModel normally completes run-end cleanup without yielding to the caller.
    class Model(ScriptedModel):
        async def _cleanup_on_run_end(self, owner: object) -> None:
            cleaning.set()
            await release.wait()

    @tool(needs_approval=True)
    def protected() -> str:
        pytest.fail("The pending tool has not been approved.")

    step = (
        [function_call("protected", "{}", call_id="protected-1")]
        if ending == "approval"
        else RuntimeError("provider failed")
    )
    inbox = RunInbox()
    receipts: list[MessageReceipt] = []

    def respond(call):
        receipts.append(inbox.queue("before shutdown"))
        return step

    agent = Agent(name="worker", model=Model([ModelStep.respond(respond)]), tools=[protected])
    config = RunConfig(tracing_disabled=True)
    result = (
        Runner.run_streamed(agent, "start", inbox=inbox, run_config=config) if streamed else None
    )

    async def consume():
        if result is None:
            return await Runner.run(agent, "start", inbox=inbox, run_config=config)
        async for _ in result.stream_events():
            pass
        return result

    task = asyncio.create_task(consume())
    try:
        await wait(cleaning)
        if result is not None:
            assert result.is_complete
            assert len(result.interruptions) == (1 if ending == "approval" else 0)
            with pytest.raises(UserError):
                result.steer("during cleanup")
        with pytest.raises(UserError):
            inbox.queue("during cleanup")
        release.set()
        if ending == "failure":
            with pytest.raises(RuntimeError, match="provider failed"):
                await asyncio.wait_for(task, 3)
        else:
            assert len((await asyncio.wait_for(task, 3)).interruptions) == 1
    finally:
        await finish(task, release)
    outcome = await asyncio.wait_for(receipts[0].wait(), 3)
    assert outcome.status == "not_submitted"
    assert outcome.reason == ("approval_pause" if ending == "approval" else "run_failed")


@pytest.mark.parametrize("streamed", [False, True])
async def test_tool_callback_answer_stays_out_of_the_queued_task_transcript(
    streamed: bool,
) -> None:
    inbox, session = RunInbox(), SimpleListSession()
    guarded: list[str] = []
    events: list[tuple[str, str]] = []

    @tool
    def compute() -> str:
        return "raw=21"

    @output_guardrail
    def check(ctx, agent, output):
        guarded.append(output)
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    def first(call):
        inbox.queue("repeat the answer")
        return [function_call("compute", "{}", call_id="compute-1")]

    model = ScriptedModel([ModelStep.respond(first), [assistant_message("last answer")]])
    agent = Agent(
        name="worker",
        model=model,
        tools=[compute],
        output_guardrails=[check],
        tool_use_behavior=lambda ctx, results: ToolsToFinalOutputResult(
            is_final_output=True, final_output="Approved answer is 42"
        ),
    )
    config = RunConfig(tracing_disabled=True)
    result: RunResultBase
    if streamed:
        result = Runner.run_streamed(
            agent, "start", inbox=inbox, session=session, run_config=config
        )
        async for event in result.stream_events():
            assert "Approved answer is 42" not in repr(event)
            if event.type != "run_item_stream_event":
                continue
            if isinstance(event.item, ToolCallOutputItem):
                events.append(("tool", event.item.output))
            elif isinstance(event.item, MessageOutputItem):
                events.append(("assistant", ItemHelpers.text_message_output(event.item)))
    else:
        result = await Runner.run(agent, "start", inbox=inbox, session=session, run_config=config)

    def transcript(items):
        entries = []
        for item in items:
            if item.get("type") == "function_call_output":
                entries.append(("tool", item["output"]))
            elif item.get("role") in ("assistant", "user"):
                content = item["content"]
                text = (
                    content
                    if isinstance(content, str)
                    else "".join(part["text"] for part in content)
                )
                entries.append((item["role"], text))
        return entries

    expected = [
        ("user", "start"),
        ("tool", "raw=21"),
        ("user", "repeat the answer"),
        ("assistant", "last answer"),
    ]
    assert result.final_output == "last answer"
    assert result.previous_outputs == ("Approved answer is 42",)
    assert guarded == ["Approved answer is 42", "last answer"]
    history = result.to_input_list()
    saved = await session.get_items()
    assert "Approved answer is 42" not in repr(
        (
            model.calls[1].input,
            model.calls[1].system_instructions,
            history,
            saved,
        )
    )
    assert transcript(model.calls[1].input) == expected[:-1]
    assert transcript(history) == expected
    assert transcript(saved) == expected
    if streamed:
        assert events == [entry for entry in expected if entry[0] != "user"]


def agent_with_two_callback_answers(inbox: RunInbox, answer, *, output_type=str):
    @tool
    def work() -> str:
        return "public tool result"

    def first(call):
        inbox.queue("next task")
        return [function_call("work", "{}", call_id="first")]

    return Agent(
        name="worker",
        model=ScriptedModel(
            [ModelStep.respond(first), [function_call("work", "{}", call_id="second")]]
        ),
        tools=[work],
        output_type=output_type,
        tool_use_behavior=lambda ctx, results: ToolsToFinalOutputResult(
            is_final_output=True, final_output=answer()
        ),
    )


@pytest.mark.parametrize("streamed", [False, True])
async def test_previous_output_snapshots_a_mutable_callback_answer(streamed: bool) -> None:
    inbox = RunInbox()
    shared: list[list[str]] = [[]]

    def answer():
        shared[0].append("answer")
        return shared

    agent = agent_with_two_callback_answers(inbox, answer, output_type=list[list[str]])
    result = await drive(streamed, agent, inbox=inbox)
    assert result.final_output == [["answer", "answer"]]
    assert result.previous_outputs == ([["answer"]],)


@pytest.mark.parametrize("streamed", [False, True])
async def test_redaction_checks_secrets_spanning_private_callback_answers(streamed: bool) -> None:
    inbox = RunInbox()
    parts = ["synthetic-", "secret"]
    agent = agent_with_two_callback_answers(inbox, lambda: parts.pop(0))
    config = RunConfig(
        tracing_disabled=True,
        redact_text=lambda text: text.replace("synthetic-secret", "[removed]"),
    )
    with pytest.raises(UserError, match="redact_text"):
        await drive(streamed, agent, inbox=inbox, run_config=config)
    assert parts == []


@pytest.mark.parametrize("streamed", [False, True])
async def test_previous_output_copies_model_fields_without_copying_private_resources(
    streamed: bool,
) -> None:
    from collections import UserList, deque
    from collections.abc import Sequence
    from functools import cached_property
    from threading import Lock

    from pydantic import BaseModel, PrivateAttr, computed_field, field_serializer

    class Answer(BaseModel):
        values: deque[int]
        _lock: Any = PrivateAttr(default_factory=Lock)

        @computed_field  # type: ignore[prop-decorator]
        @cached_property
        def displayed_values(self) -> deque[int]:
            return self.values

    class Page(BaseModel):
        rows: Sequence[Answer]

        @field_serializer("rows")
        def serialize_rows(self, rows: Sequence[Answer]) -> list[Answer]:
            return list(rows)

    inbox = RunInbox()
    shared = Answer(values=deque())

    page = Page(rows=UserList([shared]))

    def answer() -> Page:
        shared.values.append(len(shared.values) + 1)
        shared.model_dump()  # Materialize the cached field before returning the answer.
        return page

    agent = agent_with_two_callback_answers(inbox, answer, output_type=Page)
    result = await drive(streamed, agent, inbox=inbox)
    (first_page,) = result.previous_outputs
    assert isinstance(first_page.rows, UserList)
    (first,) = first_page.rows
    assert isinstance(first, Answer)
    assert list(first.values) == [1]
    assert first.displayed_values is first.values
    assert first.model_dump(mode="json") == {"values": [1], "displayed_values": [1]}
    assert first._lock is shared._lock
    assert list(result.final_output.rows[0].values) == [1, 2]


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("failure", ["guardrail", "policy", "redaction"])
async def test_rejection_closes_intake_before_saving_tool_results(
    streamed: bool, failure: str
) -> None:
    inbox = RunInbox()
    checking_output = False
    rejections: list[str] = []

    class Session(SimpleListSession):
        async def add_items(self, items):
            if checking_output:
                for send in (inbox.queue, inbox.steer):
                    try:
                        send("too late")
                    except UserError as error:
                        rejections.append(str(error))
            await super().add_items(items)

    @output_guardrail
    def check_output(ctx, agent, output):
        nonlocal checking_output
        checking_output = True
        if failure == "redaction":
            # The guardrail passes. The subsequent check must reject the changed answer.
            output.append("synthetic-secret")
            return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    def fail_policy(ctx, proposal):
        nonlocal checking_output
        checking_output = True
        raise RuntimeError("policy failed")

    agent, model, _, release, effects = gate_tool(terminal=True)
    config = RunConfig(tracing_disabled=True)
    error: type[Exception] = OutputGuardrailTripwireTriggered
    match = None
    if failure == "policy":
        agent.hooks = AgentHooks(final_output_policy=fail_policy)
        error, match = RuntimeError, "policy failed"
    elif failure == "redaction":
        agent.output_type = list[str]
        agent.tool_use_behavior = lambda ctx, results: ToolsToFinalOutputResult(
            is_final_output=True, final_output=["answer"]
        )
        agent.hooks = AgentHooks(final_output_policy=lambda ctx, proposal: "continue")
        config.redact_text = lambda text: text.replace("synthetic-secret", "[removed]")
        error, match = UserError, "redact_text"
    agent.output_guardrails = [check_output]
    release.set()
    session = Session()
    with pytest.raises(error, match=match):
        await drive(streamed, agent, inbox=inbox, session=session, run_config=config)
    assert len(rejections) == 2
    assert all("not accepting" in message for message in rejections)
    assert len(model.calls) == 1
    assert effects == ["worked"]
    if failure == "redaction":
        saved = await session.get_items()
        assert saved[-1]["type"] == "function_call_output"
        assert "synthetic-secret" not in repr(saved)


@pytest.mark.skipif(not hasattr(asyncio.Task, "cancelling"), reason="Requires Task.cancelling")
async def test_awaited_retry_does_not_dispatch_after_owner_cancellation() -> None:
    inbox = RunInbox()
    entered = asyncio.Event()
    receipts: list[MessageReceipt] = []

    def first(call):
        receipts.append(inbox.queue("next"))
        return [assistant_message("first")]

    async def retry_policy(ctx):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return True

    model = ScriptedModel(
        [
            ModelStep.respond(first),
            ModelStep.raise_error(RuntimeError("transient")),
            [assistant_message("unexpected retry")],
        ]
    )
    config = RunConfig(
        tracing_disabled=True,
        model_settings=ModelSettings(
            retry=ModelRetrySettings(
                max_retries=1, backoff={"initial_delay": 0}, policy=retry_policy
            )
        ),
    )
    task = asyncio.create_task(
        Runner.run(Agent(name="worker", model=model), "start", inbox=inbox, run_config=config)
    )
    try:
        await wait(entered)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert len(model.calls) == 2
        assert (await receipts[0].wait()).status == "submitted"
    finally:
        await finish(task)
