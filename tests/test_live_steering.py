import asyncio
from contextlib import closing
from typing import Any

import pytest

from agents import (
    Agent,
    GuardrailFunctionOutput,
    InputGuardrailTripwireTriggered,
    RunConfig,
    Runner,
    RunResultStreaming,
    SQLiteSession,
    UserError,
    function_tool,
    input_guardrail,
)
from agents.testing import ScriptedModel, assistant_message, function_call

pytestmark = pytest.mark.asyncio


async def drain(run: RunResultStreaming) -> None:
    async for _ in run.stream_events():
        pass


def reading(**kwargs: Any):
    busy, finish = asyncio.Event(), asyncio.Event()

    @function_tool
    async def fetch() -> str:
        busy.set()
        await finish.wait()
        return "paper"

    model = ScriptedModel([[function_call("fetch", {}, call_id="a")], [assistant_message("done")]])
    agent = Agent(name="reader", tools=[fetch], model=model, **kwargs)
    return agent, model, busy, finish


@pytest.mark.parametrize("outcome", ["accept", "deny", "cancel", "after_turn"])
async def test_live_input_is_private_until_checked_and_admitted(outcome: str):
    checking, proceed = asyncio.Event(), asyncio.Event()

    @input_guardrail
    async def check(ctx, agent, value):
        fresh = "NewNote" in str(value)
        if fresh:
            checking.set()
            await proceed.wait()
        return GuardrailFunctionOutput(
            output_info=None, tripwire_triggered=fresh and outcome == "deny"
        )

    agent, model, busy, finish = reading()
    with closing(SQLiteSession("reader")) as session:
        run = Runner.run_streamed(
            agent, "read", session=session, run_config=RunConfig(input_guardrails=[check])
        )
        with pytest.raises(UserError):
            run.steer("too early")
        consumed = asyncio.create_task(drain(run))
        await asyncio.wait_for(busy.wait(), 3)
        with pytest.raises(UserError, match="loop"):
            await asyncio.to_thread(run.steer, "wrong thread")
        receipt = run.steer("NewNote")
        with pytest.raises(UserError, match="pending"):
            run.steer("duplicate")
        finish.set()
        await asyncio.wait_for(checking.wait(), 3)
        assert not receipt.done()
        assert "NewNote" not in run.to_state().to_string()
        assert "NewNote" not in str(await session.get_items())
        if outcome == "cancel":
            run.cancel()
        elif outcome == "after_turn":
            run.cancel("after_turn")
        proceed.set()
        if outcome == "deny":
            with pytest.raises(InputGuardrailTripwireTriggered):
                await asyncio.wait_for(consumed, 3)
        else:
            await asyncio.wait_for(consumed, 3)
        accepted = outcome == "accept"
        assert await asyncio.wait_for(receipt, 3) is accepted
        assert ("NewNote" in str(await session.get_items())) is accepted
        assert ("NewNote" in str(run.to_input_list())) is accepted
        assert not run.to_state().pending_input
        assert len(model.calls) == (2 if accepted else 1)
        if accepted:
            assert "NewNote" in str(model.calls[1].input)
        with pytest.raises(UserError):
            run.steer("too late")


async def test_abandoning_a_receipt_does_not_cancel_admission():
    agent, model, busy, finish = reading()
    run = Runner.run_streamed(agent, "read")
    consumed = asyncio.create_task(drain(run))
    await asyncio.wait_for(busy.wait(), 3)
    run.steer("NewNote").cancel()
    finish.set()
    await asyncio.wait_for(consumed, 3)
    assert "NewNote" in str(model.calls[1].input)


async def test_graceful_stop_while_initial_check_settles_skips_new_checks(monkeypatch):
    from agents.run_internal import run_loop

    waiting, release = asyncio.Event(), asyncio.Event()
    check_initial = run_loop._check_streamed_input

    async def wait_for_initial(*args):
        waiting.set()
        await check_initial(*args)

    monkeypatch.setattr(run_loop, "_check_streamed_input", wait_for_initial)

    @input_guardrail
    async def check(ctx, agent, value):
        await release.wait()
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered="NewNote" in str(value))

    agent, model, busy, finish = reading(input_guardrails=[check])
    run = Runner.run_streamed(agent, "read")
    consumed = asyncio.create_task(drain(run))
    await asyncio.wait_for(busy.wait(), 3)
    receipt = run.steer("NewNote")
    finish.set()
    await asyncio.wait_for(waiting.wait(), 3)
    run.cancel("after_turn")
    release.set()
    await asyncio.wait_for(consumed, 3)
    assert await receipt is False
    assert len(run.input_guardrail_results) == 1 and len(model.calls) == 1


@pytest.mark.parametrize("outcome", ["cancel", "lost_reply"])
async def test_append_already_committed_does_not_report_a_drop(outcome: str):
    committed, release = asyncio.Event(), asyncio.Event()

    class DelayedSession(SQLiteSession):
        async def add_items(self, items):
            await super().add_items(items)
            if "NewNote" in str(items):
                committed.set()
                await release.wait()
                if outcome == "lost_reply":
                    raise OSError("lost reply")

    agent, model, busy, finish = reading()
    with closing(DelayedSession("reader")) as session:
        run = Runner.run_streamed(agent, "read", session=session)
        consumed = asyncio.create_task(drain(run))
        await asyncio.wait_for(busy.wait(), 3)
        receipt = run.steer("NewNote")
        finish.set()
        await asyncio.wait_for(committed.wait(), 3)
        during_write = run.to_state()
        if outcome == "cancel":
            run.cancel()
        release.set()
        if outcome == "lost_reply":
            with pytest.raises(OSError, match="lost reply"):
                await asyncio.wait_for(consumed, 3)
            with pytest.raises(UserError, match="Check session history"):
                await asyncio.wait_for(receipt, 3)
            with pytest.raises(UserError, match="unrecoverable"):
                await drain(Runner.run_streamed(agent, run.to_state(), session=session))
        else:
            await asyncio.wait_for(consumed, 3)
            assert await asyncio.wait_for(receipt, 3) is True
            assert str(run.to_input_list()).count("NewNote") == 1
            assert "NewNote" in run.to_state().to_string()
        with pytest.raises(UserError, match="unrecoverable"):
            await Runner.run(agent, during_write, session=session)
        assert str(await session.get_items()).count("NewNote") == 1
        assert len(model.calls) == 1


async def test_server_owned_history_rejects_live_input():
    agent, model, busy, finish = reading()
    run = Runner.run_streamed(agent, "read", previous_response_id="prior")
    consumed = asyncio.create_task(drain(run))
    await asyncio.wait_for(busy.wait(), 3)
    with pytest.raises(UserError, match="client-managed"):
        run.steer("NewNote")
    finish.set()
    await asyncio.wait_for(consumed, 3)
    assert "NewNote" not in str(model.calls[1].input)


async def test_redaction_applies_before_live_input_guardrails():
    inspected = []

    @input_guardrail
    async def check(ctx, agent, value):
        inspected.append(value)
        return GuardrailFunctionOutput(output_info=None, tripwire_triggered=False)

    agent, model, busy, finish = reading(input_guardrails=[check])
    run = Runner.run_streamed(
        agent,
        "read",
        run_config=RunConfig(redact_text=lambda s: s.replace("PrivateNote", "masked")),
    )
    consumed = asyncio.create_task(drain(run))
    await asyncio.wait_for(busy.wait(), 3)
    receipt = run.steer("PrivateNote")
    finish.set()
    await asyncio.wait_for(consumed, 3)
    assert await receipt
    for value in (inspected, model.calls[1].input, run.to_input_list()):
        assert "PrivateNote" not in str(value)
        assert "masked" in str(value)


async def test_redactor_failure_and_early_refusal_discard_private_arguments():
    import traceback

    def redact(text: str):
        if "PrivateNote" in text:
            raise ValueError("invalid PrivateNote")
        return text

    agent, model, busy, finish = reading()
    run = Runner.run_streamed(agent, "read", run_config=RunConfig(redact_text=redact))
    consumed = asyncio.create_task(drain(run))
    for early in (True, False):
        if not early:
            await asyncio.wait_for(busy.wait(), 3)
        with pytest.raises(UserError) as exc:
            run.steer("PrivateNote")
        assert "PrivateNote" not in str(exc.value)
        for frame, _ in traceback.walk_tb(exc.value.__traceback__):
            if frame.f_globals.get("__name__", "").startswith("agents."):
                assert "PrivateNote" not in repr(frame.f_locals)
    finish.set()
    await asyncio.wait_for(consumed, 3)
    assert "PrivateNote" not in str(model.calls[1].input)


async def test_redactor_checks_tool_output_alongside_live_text_before_publication():
    agent, model, busy, finish = reading()
    run = Runner.run_streamed(
        agent,
        "read",
        run_config=RunConfig(redact_text=lambda s: s.replace("paper-secret", "masked")),
    )
    consumed = asyncio.create_task(drain(run))
    await asyncio.wait_for(busy.wait(), 3)
    receipt = run.steer("-secret")
    finish.set()
    with pytest.raises(UserError):
        await asyncio.wait_for(consumed, 3)
    assert await receipt is False
    assert "-secret" not in str(run.new_items)
    assert len(model.calls) == 1
