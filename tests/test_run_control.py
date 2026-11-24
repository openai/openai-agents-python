"""Public M2 acceptance checks with deterministic request and tool barriers."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest

from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrail,
    InputGuardrailTripwireTriggered,
    MaxTurnsExceeded,
    ModelSettings,
    RunConfig,
    RunContextWrapper,
    RunControl,
    Runner,
    RunState,
    UserError,
)
from agents.decorators import tool
from agents.models.interface import ModelProvider
from agents.retry import ModelRetrySettings
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
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


def user_text(item: Any) -> str | None:
    if not isinstance(item, dict) or item.get("role") != "user":
        return None
    content = item.get("content")
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in content or [] if isinstance(part, dict))


def messages(items: list[Any]) -> list[str]:
    return [text for item in items if (text := user_text(item)) is not None]


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
    control = RunControl()
    task = asyncio.create_task(drive(streamed, agent, control=control))
    try:
        await asyncio.gather(*(wait(event) for event in entered))
        first = control.send("Explain the failure.")
        second = control.send("Then suggest a fix.")
        assert first.status == second.status == "queued"
        release[1].set()
        await wait(completed[1])
        assert len(model.calls) == 1 and first.status == second.status == "queued"
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
            i for i, item in enumerate(payload) if user_text(item) in messages(payload)[-2:]
        ]
        assert len(output_positions) == 2 and max(output_positions) < min(correction_positions)
    finally:
        await finish(task, *release)


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("terminal", [False, True], ids=["max-turns", "terminal-tool"])
async def test_no_extra_request_for_unused_correction(streamed: bool, terminal: bool) -> None:
    agent, model, entered, release, _ = gate_tool(terminal=terminal)
    control = RunControl()
    task = asyncio.create_task(drive(streamed, agent, control=control, max_turns=1))
    try:
        await wait(entered)
        receipt = control.send("No extra request.")
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
            control.send("too late")
    finally:
        await finish(task, release)


@pytest.mark.parametrize("streamed", [False, True])
async def test_final_model_response_does_not_start_followup(streamed: bool) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(call):
        entered.set()
        await release.wait()
        return [assistant_message("done")]

    model = ScriptedModel([ModelStep.respond(respond)])
    control = RunControl()
    task = asyncio.create_task(drive(streamed, Agent(name="final", model=model), control=control))
    try:
        await wait(entered)
        receipt = control.send("Unused final correction.")
        release.set()
        assert (await asyncio.wait_for(task, 3)).final_output == "done"
        assert (await asyncio.wait_for(receipt.wait(), 3)).status == "not_submitted"
        assert len(model.calls) == 1
    finally:
        await finish(task, release)


@pytest.mark.parametrize("streamed", [False, True])
async def test_preopen_late_and_reused_handles_reject(streamed: bool) -> None:
    agent, _, entered, release, _ = gate_tool()
    control = RunControl()
    with pytest.raises(UserError):
        control.send("before binding")
    task = asyncio.create_task(drive(streamed, agent, control=control))
    try:
        with pytest.raises(UserError):
            control.send("task has not started")
        await wait(entered)
        release.set()
        await asyncio.wait_for(task, 3)
        with pytest.raises(UserError):
            control.send("after completion")
        with pytest.raises(UserError):
            await asyncio.wait_for(drive(streamed, agent, control=control), 3)
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
    control = RunControl()
    task = asyncio.create_task(drive(streamed, agent, control=control))
    try:
        await wait(entered)
        receipt = control.send("This does not approve the call.")
        release.set()
        result = await asyncio.wait_for(task, 3)
        assert len(result.interruptions) == 1 and effects == []
        outcome = await asyncio.wait_for(receipt.wait(), 3)
        assert outcome.status == "not_submitted" and outcome.retained is False
        assert len(model.calls) == 1
        with pytest.raises(UserError):
            control.send("after pause")
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
    control = RunControl()
    task = asyncio.create_task(drive(streamed, parent, control=control))
    try:
        await wait(nested_entered)
        receipt = control.send("parent correction")
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
    control = RunControl(max_pending=2)
    task = asyncio.create_task(drive(streamed, agent, control=control))
    try:
        await wait(entered)
        first, second = control.send("same"), control.send("same")
        with pytest.raises(UserError):
            control.send("overflow")
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
    control = RunControl()
    task = asyncio.create_task(drive(streamed, agent, control=control))
    waiter = None
    try:
        await wait(entered)
        receipt = control.send("Keep this occurrence.")
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
@pytest.mark.parametrize("mode", ["drop", "copy"], ids=["dropped", "reconstructed"])
async def test_filter_outcome_tracks_occurrence_instead_of_equal_text(
    streamed: bool, mode: str
) -> None:
    agent, model, entered, release, _ = gate_tool()
    control = RunControl()

    def filter_input(data):
        if mode == "drop":
            data.model_data.input = [
                item for item in data.model_data.input if user_text(item) != "correction"
            ]
        else:
            data.model_data.input = copy.deepcopy(data.model_data.input)
        return data.model_data

    task = asyncio.create_task(
        drive(
            streamed,
            agent,
            control=control,
            run_config=RunConfig(call_model_input_filter=filter_input, tracing_disabled=True),
        )
    )
    try:
        await wait(entered)
        receipt = control.send("correction")
        release.set()
        await asyncio.wait_for(task, 3)
        outcome = await asyncio.wait_for(receipt.wait(), 3)
        assert outcome.status == ("not_submitted" if mode == "drop" else "unknown")
        assert ("correction" in messages(model.calls[1].input)) == (mode == "copy")
    finally:
        await finish(task, release)


@pytest.mark.parametrize("streamed", [False, True])
async def test_live_guardrail_rejection_prevents_filter_model_and_session_exposure(
    streamed: bool,
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
    control = RunControl()
    task = asyncio.create_task(
        drive(
            streamed,
            agent,
            control=control,
            session=session,
            run_config=RunConfig(call_model_input_filter=filter_input, tracing_disabled=True),
        )
    )
    try:
        await wait(entered)
        receipt = control.send("blocked correction")
        release.set()
        with pytest.raises(InputGuardrailTripwireTriggered):
            await asyncio.wait_for(task, 3)
        outcome = await asyncio.wait_for(receipt.wait(), 3)
        assert outcome.status == "not_submitted" and outcome.retained is False
        assert len(model.calls) == 1
        assert "blocked correction" not in filter_inputs
        assert "blocked correction" not in messages(await session.get_items())
        assert any("blocked correction" in batch for batch in guarded)
    finally:
        await finish(task, release)


@pytest.mark.parametrize("streamed", [False, True])
async def test_cancelled_run_settles_queued_receipts_and_closes_intake(
    streamed: bool,
) -> None:
    agent, model, entered, release, effects = gate_tool()
    control = RunControl()
    task = asyncio.create_task(drive(streamed, agent, control=control))
    try:
        await wait(entered)
        receipt = control.send("pending cancellation")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        outcome = await asyncio.wait_for(receipt.wait(), 3)
        assert outcome.status == "not_submitted" and outcome.reason
        assert len(model.calls) == 1 and not effects
        with pytest.raises(UserError):
            control.send("too late")
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
    controls = {name: RunControl() for name in entered}
    tasks = {
        name: asyncio.create_task(drive(streamed, agent, context=name, control=controls[name]))
        for name in entered
    }
    try:
        await asyncio.gather(*(wait(event) for event in entered.values()))
        receipts = {name: controls[name].send(f"correction-{name}") for name in entered}
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
async def test_approved_tool_resume_accepts_input_before_tool_finishes(
    streamed: bool,
) -> None:
    agent, model, entered, release, effects = gate_tool(approval=True)
    paused = await drive(streamed, agent)
    state = paused.to_state()
    state.approve(state.get_interruptions()[0])
    control = RunControl()
    task = asyncio.create_task(drive(streamed, agent, state, control=control))
    try:
        await wait(entered)
        receipt = control.send("Explain before editing again.")
        assert receipt.status == "queued" and len(model.calls) == 1
        release.set()
        assert (await asyncio.wait_for(task, 3)).final_output == "done"
        assert (await receipt.wait()).status == "submitted"
        assert effects == ["worked"]
        assert messages(model.calls[1].input)[-1] == "Explain before editing again."
        assert model.calls[1].input[-2]["type"] == "function_call_output"
    finally:
        await finish(task, release)


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
        ]
    )
    agent.model = model
    control = RunControl()
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
    task = asyncio.create_task(drive(streamed, agent, control=control, run_config=config))
    try:
        await wait(entered)
        first = control.send("first correction")
        release.set()
        await wait(retry_entered)
        assert first.status == "submitted"
        second = control.send("during retry")
        retry_release.set()
        await asyncio.wait_for(task, 3)
        assert (await first.wait()).status == "submitted"
        assert (await second.wait()).status == "not_submitted"
        assert model.calls[1].input == model.calls[2].input
        assert messages(model.calls[2].input).count("first correction") == 1
        assert "during retry" not in messages(model.calls[2].input)
    finally:
        await finish(task, release, retry_release)


@pytest.mark.parametrize("streamed", [False, True])
async def test_send_after_filter_cutoff_waits_for_later_request(streamed: bool) -> None:
    agent, model, entered, release, _ = gate_tool()
    filtering, allow_filter = asyncio.Event(), asyncio.Event()
    count = 0

    async def filter_input(data):
        nonlocal count
        count += 1
        if count == 2:
            filtering.set()
            await allow_filter.wait()
        return data.model_data

    control = RunControl()
    task = asyncio.create_task(
        drive(
            streamed,
            agent,
            control=control,
            run_config=RunConfig(call_model_input_filter=filter_input, tracing_disabled=True),
        )
    )
    try:
        await wait(entered)
        first = control.send("before cutoff")
        release.set()
        await wait(filtering)
        second = control.send("after cutoff")
        assert first.status == second.status == "queued"
        allow_filter.set()
        await asyncio.wait_for(task, 3)
        assert (await first.wait()).status == "submitted"
        assert (await second.wait()).status == "not_submitted"
        assert messages(model.calls[1].input)[-1] == "before cutoff"
        assert "after cutoff" not in messages(model.calls[1].input)
    finally:
        await finish(task, release, allow_filter)


@pytest.mark.parametrize("streamed", [False, True])
async def test_local_session_retains_one_admitted_occurrence(streamed: bool) -> None:
    agent, model, entered, release, _ = gate_tool()
    session, control = SimpleListSession(), RunControl()
    task = asyncio.create_task(drive(streamed, agent, control=control, session=session))
    try:
        await wait(entered)
        receipt = control.send("stored correction")
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
    control = RunControl()
    task = asyncio.create_task(
        drive(streamed, source, control=control, auto_previous_response_id=server_managed)
    )
    try:
        await wait(entered)
        receipt = control.send("destination correction")
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

    session, control = SimpleListSession(), RunControl()
    config = RunConfig(
        tracing_disabled=True,
        redact_text=lambda value: value.replace("synthetic-secret", "[removed]"),
        input_guardrails=[InputGuardrail(guardrail_function=guard, run_in_parallel=True)],
        call_model_input_filter=filter_input,
    )
    task = asyncio.create_task(
        drive(streamed, agent, control=control, session=session, run_config=config)
    )
    try:
        await wait(entered)
        receipt = control.send("Explain synthetic-secret")
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
                item for item in data.model_data.input if user_text(item) != "server correction"
            ]
        return data.model_data

    control = RunControl()
    task = asyncio.create_task(
        drive(
            streamed,
            agent,
            control=control,
            auto_previous_response_id=True,
            run_config=RunConfig(call_model_input_filter=filter_input, tracing_disabled=True),
        )
    )
    try:
        await wait(entered)
        receipt = control.send("server correction")
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
            item for item in data.model_data.input if user_text(item) != "resume correction"
        ]
        return data.model_data

    control = RunControl()
    task = asyncio.create_task(
        drive(
            streamed,
            agent,
            control=control,
            auto_previous_response_id=True,
            run_config=RunConfig(call_model_input_filter=omit_correction, tracing_disabled=True),
        )
    )
    try:
        await wait(entered)
        receipt = control.send("resume correction")
        release.set()
        paused = await asyncio.wait_for(task, 3)
        assert len(paused.interruptions) == 1 and effects == ["worked"]
        outcome = await receipt.wait()
        assert outcome.status == "not_submitted" and outcome.retained is True
        state = await RunState.from_json(agent, paused.to_state().to_json())
        assert messages(state.pending_input) == ["resume correction"]
        state.approve(state.get_interruptions()[0])
        resumed = await drive(streamed, agent, state, control=RunControl())
        assert resumed.final_output == "done" and effects == ["worked", "protected"]
        assert messages(model.calls[2].input).count("resume correction") == 1
        assert state.pending_input == []
    finally:
        await finish(task, release)


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
    control = RunControl()
    task = asyncio.create_task(
        drive(streamed, agent, control=control, auto_previous_response_id=True)
    )
    try:
        await wait(entered)
        receipt = control.send("uncertain server acceptance")
        release.set()
        with pytest.raises(RuntimeError, match="request failed"):
            await asyncio.wait_for(task, 3)
        outcome = await asyncio.wait_for(receipt.wait(), 3)
        assert outcome.status == "submitted" and outcome.turn_number == 2
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
    session, control = SimpleListSession(), RunControl()
    task = asyncio.create_task(
        drive(
            streamed,
            agent,
            control=control,
            session=session,
            run_config=RunConfig(
                model_provider=Provider(), model_selector=select, tracing_disabled=True
            ),
        )
    )
    try:
        await wait(entered)
        receipt = control.send("recoverable correction")
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
