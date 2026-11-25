import asyncio
from contextlib import closing

import pytest

from agents import (
    Agent,
    AgentHooks,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    OutputGuardrailTripwireTriggered,
    RunConfig,
    Runner,
    RunState,
    SQLiteSession,
    UserError,
    function_tool,
    input_guardrail,
    output_guardrail,
)
from agents.run_config import Steering
from agents.stream_events import RunCompleted
from agents.testing import ScriptedModel, assistant_message, function_call

pytestmark = pytest.mark.asyncio


def library(**kwargs):
    entered, release = asyncio.Event(), asyncio.Event()

    @function_tool
    async def read() -> str:
        entered.set()
        await release.wait()
        return "report"

    model = ScriptedModel(
        [
            [function_call("read", {}, call_id="lookup")],
            [assistant_message("summary")],
            [assistant_message("revised")],
        ]
    )
    return Agent("Reader", model=model, tools=[read], **kwargs), model, entered, release


@pytest.mark.parametrize(
    "options,maximum,admitted,answers",
    [
        (Steering(mode="all"), 3, [True, True], ["summary"]),
        (Steering(), 3, [True, False], ["summary"]),
        (Steering(continue_after_final=True), 3, [True, True], ["summary", "revised"]),
        (Steering(continue_after_final=True), 2, [True, False], ["summary"]),
        (Steering(continue_after_final=True), None, [True, False], ["summary"]),
    ],
)
async def test_queued_steering_follows_guarded_turns_and_one_completion(
    options, maximum, admitted, answers
):
    ended, checked = [], []

    class Hooks(AgentHooks):
        async def on_end(self, context, agent, output):
            ended.append(output)

    @output_guardrail
    async def check(ctx, agent, answer):
        checked.append(answer)
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    agent, model, entered, release = library(hooks=Hooks(), output_guardrails=[check])
    with closing(SQLiteSession("test")) as session:
        run = Runner.run_streamed(
            agent,
            "start",
            session=session,
            max_turns=maximum,
            run_config=RunConfig(steering=options),
        )
        async with run.stream_approved_output() as output:
            await asyncio.wait_for(entered.wait(), 3)
            first, last = run.steer("first-note"), run.steer("last-note")
            assert "first-note" not in run.to_state().to_string()
            release.set()
            events = [event async for event in output]
        assert len(events) == 1 and isinstance(events[0], RunCompleted)
        assert [await first, await last] == admitted
        assert ended == [run.final_output] == answers[-1:]
        assert checked == answers
        assert len(model.calls) == run.current_turn == len(answers) + 1
        assert "first-note" in str(model.calls[1].input)
        assert ("last-note" in str(model.calls[1].input)) == (options.mode == "all")
        for items in (run.to_input_list(), await session.get_items(), model.calls[-1].input):
            assert str(items).count("first-note") == 1
            assert str(items).count("last-note") == int(admitted[1])
            if admitted[1]:
                assert str(items).index("first-note") < str(items).index("last-note")
            if len(answers) == 2:
                assert str(items).index("summary") < str(items).index("last-note")


@pytest.mark.parametrize("gate", ["input", "output"])
async def test_rejected_batch_or_reply_never_reaches_the_next_request(gate):
    @input_guardrail
    async def check(ctx, agent, items):
        deny = gate == "input" and "last-note" in str(items)
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=deny)

    @output_guardrail
    async def screen(ctx, agent, answer):
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=True)

    agent, model, entered, release = library(input_guardrails=[check], output_guardrails=[screen])
    options = Steering(mode="all") if gate == "input" else Steering(continue_after_final=True)
    with closing(SQLiteSession("test")) as session:
        run = Runner.run_streamed(
            agent, "start", session=session, run_config=RunConfig(steering=options)
        )
        async with run.stream_approved_output() as output:
            await asyncio.wait_for(entered.wait(), 3)
            first, last = run.steer("first-note"), run.steer("last-note")
            release.set()
            with pytest.raises(
                InputGuardrailTripwireTriggered
                if gate == "input"
                else OutputGuardrailTripwireTriggered
            ):
                [event async for event in output]
        assert [await first, await last] == [gate == "output", False]
        assert len(model.calls) == (2 if gate == "output" else 1)
        for items in (run.to_input_list(), await session.get_items()):
            assert "last-note" not in str(items) and "summary" not in str(items)


async def test_terminal_tool_never_continues_and_rejects_overflow():
    agent, model, entered, release = library(tool_use_behavior="stop_on_first_tool")
    run = Runner.run_streamed(
        agent,
        "start",
        run_config=RunConfig(steering=Steering(mode="all", continue_after_final=True)),
    )
    async with run.stream_approved_output() as output:
        await asyncio.wait_for(entered.wait(), 3)
        waiting = [run.steer("extra") for _ in range(16)]
        with pytest.raises(UserError, match="full"):
            run.steer("overflow")
        release.set()
        [event async for event in output]
    assert not any(await asyncio.gather(*waiting))
    assert run.final_output == "report" and len(model.calls) == 1


@pytest.mark.parametrize("gate", ["output", "input"])
async def test_checkpoint_cannot_restart_an_unchecked_continuation(gate):
    checking, settle = asyncio.Event(), asyncio.Event()

    @output_guardrail
    async def answer(ctx, agent, text):
        if gate == "output":
            checking.set()
            await settle.wait()
            raise RuntimeError("unavailable")
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    @input_guardrail
    async def addition(ctx, agent, items):
        denied = "last-note" in str(items)
        if denied:
            checking.set()
            await settle.wait()
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=denied)

    agent, model, entered, release = library(
        input_guardrails=[addition], output_guardrails=[answer]
    )
    with closing(SQLiteSession("guard")) as session:
        config = RunConfig(steering=Steering(continue_after_final=True))
        run = Runner.run_streamed(agent, "start", session=session, run_config=config)
        async with run.stream_approved_output() as events:
            await asyncio.wait_for(entered.wait(), 3)
            first, last = run.steer("first-note"), run.steer("last-note")
            release.set()
            await asyncio.wait_for(checking.wait(), 3)
            during = run.to_state().to_string()
            settle.set()
            with pytest.raises(
                RuntimeError if gate == "output" else InputGuardrailTripwireTriggered
            ):
                [event async for event in events]
        assert await first and not await last
        assert ("summary" in str(await session.get_items())) == (gate == "input")
        for payload in (during, run.to_state().to_string()):
            assert "last-note" not in payload
            assert ("summary" in payload) == (gate == "input")
            state = await RunState.from_string(agent, payload)
            with pytest.raises(UserError, match="unrecoverable"):
                await Runner.run(agent, state, session=session)
        assert len(model.calls) == 2


async def test_only_waiting_receipts_can_be_withdrawn():
    checking, proceed = asyncio.Event(), asyncio.Event()

    @input_guardrail
    async def check(ctx, agent, items):
        if "first-note" in str(items):
            checking.set()
            await proceed.wait()
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    agent, model, entered, release = library(input_guardrails=[check])
    config = RunConfig(steering=Steering(mode="all", continue_after_final=True))
    with closing(SQLiteSession("queue")) as session:
        run = Runner.run_streamed(agent, "start", session=session, run_config=config)
        async with run.stream_approved_output() as events:
            await asyncio.wait_for(entered.wait(), 3)
            first, withdrawn = run.steer("first-note"), run.steer("discard")
            assert run.withdraw(withdrawn) and not run.withdraw(withdrawn)
            assert not await withdrawn
            release.set()
            await asyncio.wait_for(checking.wait(), 3)
            assert not run.withdraw(first)
            last = run.steer("last-note")
            proceed.set()
            [event async for event in events]
        assert await first and await last
        assert len(model.calls) == 3
        assert "last-note" not in str(model.calls[1].input)
        assert "last-note" in str(model.calls[2].input)
        assert "discard" not in str(await session.get_items())


async def test_unknown_commit_errors_only_the_reserved_batch():
    committed, settle = asyncio.Event(), asyncio.Event()

    class Session(SQLiteSession):
        async def add_items(self, items):
            await super().add_items(items)
            if "first-note" in str(items):
                committed.set()
                await settle.wait()
                raise OSError("unconfirmed")

    agent, model, entered, release = library()
    config = RunConfig(steering=Steering(mode="all", continue_after_final=True))
    with closing(Session("queue")) as session:
        run = Runner.run_streamed(agent, "start", session=session, run_config=config)
        async with run.stream_approved_output() as events:
            await asyncio.wait_for(entered.wait(), 3)
            first, last = run.steer("first-note"), run.steer("last-note")
            release.set()
            await asyncio.wait_for(committed.wait(), 3)
            assert not run.withdraw(last)
            later = run.steer("later-note")
            settle.set()
            with pytest.raises(OSError):
                [event async for event in events]
        for receipt in (first, last):
            with pytest.raises(UserError, match="Unconfirmed"):
                await receipt
        assert not await later
        history = str(await session.get_items())
        assert "later-note" not in history and history.count("last-note") == 1
        with pytest.raises(UserError, match="unrecoverable"):
            await Runner.run(agent, run.to_state(), session=session)
        assert len(model.calls) == 1


@pytest.mark.parametrize("boundary", ["handoff", "approval"])
async def test_queue_does_not_follow_a_handoff_or_approval(boundary):
    agent, _, entered, release = library()
    specialist = ScriptedModel([[assistant_message("done")]])
    target = Agent("Next", model=specialist)

    @function_tool(needs_approval=True)
    def save() -> None:
        raise AssertionError("No approval")

    action = "save" if boundary == "approval" else "transfer_to_next"
    model = ScriptedModel(
        [
            [function_call("read", {}, call_id="lookup")],
            [function_call(action, {}, call_id="move")],
        ]
    )
    agent = agent.clone(model=model, tools=[*agent.tools, save], handoffs=[target])
    run = Runner.run_streamed(
        agent, "start", run_config=RunConfig(steering=Steering(continue_after_final=True))
    )
    async with run.stream_approved_output() as events:
        await asyncio.wait_for(entered.wait(), 3)
        first = run.steer("first-note")
        tail = [run.steer("private-a"), run.steer("private-b")]
        release.set()
        [event async for event in events]
    assert await first and not any(await asyncio.gather(*tail))
    assert bool(run.interruptions) == (boundary == "approval")
    assert "private-" not in run.to_state().to_string()
    if boundary == "handoff":
        assert run.last_agent is target
        assert "private-" not in str(specialist.calls[0].input)


async def test_secret_split_across_a_batch_is_refused():
    agent, model, entered, release = library()
    config = RunConfig(
        steering=Steering(mode="all"),
        redact_text=lambda text: text.replace("private-code", "masked"),
    )
    run = Runner.run_streamed(agent, "start", run_config=config)
    async with run.stream_approved_output() as events:
        await asyncio.wait_for(entered.wait(), 3)
        receipts = [run.steer("private-"), run.steer("code")]
        release.set()
        with pytest.raises(UserError):
            [event async for event in events]
    assert not any(await asyncio.gather(*receipts))
    assert len(model.calls) == 1 and "private-" not in str(run.new_items)


@pytest.mark.parametrize("stop", ["budget", "policy"])
async def test_explicit_finish_and_exhausted_budget_override_optional_continuation(stop):
    from agents import RunBudget

    agent, model, entered, release = library()
    if stop == "policy":
        agent.hooks = AgentHooks(final_output_policy=lambda ctx, proposal: "finish")
    config = RunConfig(
        budget=RunBudget(max_model_calls=2) if stop == "budget" else None,
        steering=Steering(continue_after_final=True),
    )
    run = Runner.run_streamed(agent, "start", run_config=config)
    async with run.stream_approved_output() as events:
        await asyncio.wait_for(entered.wait(), 3)
        first, last = run.steer("first-note"), run.steer("last-note")
        release.set()
        [event async for event in events]
    assert await first and not await last
    assert run.final_output == "summary" and len(model.calls) == 2
