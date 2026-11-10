"""Detached checks cannot retire linked playback or inject linked feedback."""

import asyncio

from agents.realtime import RealtimeGuardrailResult
from tests.test_realtime_guardrail_retention import output, owner_for, settle


async def test_detached_guardrail_targets_only_its_response_without_linked_feedback():
    checked, interrupted = [], []

    def check(value):
        checked.append(value)
        return RealtimeGuardrailResult(True)

    owner = owner_for(check)
    owner._response_id = "linked"
    owner._guardrails = (
        type(owner._guardrails[0])("policy", check, feedback="Linked feedback only"),
    )

    async def interrupt(response_id, **kwargs):
        interrupted.append((response_id, kwargs))

    owner.interrupt = interrupt
    owner._payload(
        {"DetachedResponse": {"OutputTranscriptDone": output("detached", "checked text")}}, None
    )
    await settle(owner)
    assert checked[0].response_id == "detached"
    assert checked[0].text == "checked text"
    assert interrupted == [("detached", {"playback": None, "feedback": None})]
    assert owner._response_id == "linked"
    assert "linked" not in owner._guardrail_trips


async def test_unqualified_interrupt_preserves_detached_check_and_output():
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def check(value):
        if value.response_id == "detached":
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return RealtimeGuardrailResult(False)

    owner = owner_for(check)
    owner._session._check_callback_operation = lambda _: None
    owner._ready.set_result(None)

    async def call(operation, **kwargs):
        assert operation == "interrupt"
        return {
            "retired_response_ids": ["linked"],
            "cancelled_response_id": "linked",
            "joined_root_turn_ids": [],
        }

    owner._call = call
    owner._payload(
        {"DetachedResponse": {"OutputTranscriptDone": output("detached", "retained")}}, None
    )
    await entered.wait()
    task = next(item.task for item in owner._guardrail_items.values())
    await owner.interrupt()
    assert not task.done()
    assert not cancelled.is_set()
    events = owner.events()
    event = await events.__anext__()
    assert event.response_id == "detached" and event.text == "retained"
    await events.aclose()
    release.set()
    await settle(owner)
