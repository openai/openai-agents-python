"""Faults at the boundary between provider completion and native publication."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from _agents_codex._decision_accounting import begin_decision_response, consume_decision_response
from _agents_codex.session import _ChildCallbacks
from agents import DecisionResponse, NativeError


def boundary(*, child=False):
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    commits, observed = [], []
    receipt = SimpleNamespace(completion_id="decision-1", thread_id="root", turn_id="turn-1")

    async def commit(metadata):
        commits.append(metadata)
        entered.set()
        await release.wait()
        finished.set()

    async def capture(_message):
        return receipt

    receipt.commit = commit
    state = SimpleNamespace(
        handle=SimpleNamespace(turn_id="turn-1"), budget=SimpleNamespace(observe=observed.append)
    )
    event = {
        "id": "turn-1",
        "msg": {
            "type": "raw_response_completed",
            "response_id": "decision-1",
            "token_usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
        },
    }
    if child:
        receipt.thread_id = "child"
        state = _ChildCallbacks(
            handle=SimpleNamespace(child_id="child", turn_id="turn-1", instance_id="incarnation-1"),
            native_instance=17,
        )
        event.update(
            subagent_id="child", subagent_incarnation="incarnation-1", subagent_instance_id=17
        )
    session = SimpleNamespace(
        id="root",
        _native=SimpleNamespace(_capture_decision=capture),
        _decision_completions={},
        _reader=asyncio.get_running_loop().create_future(),
    )
    return SimpleNamespace(
        session=session,
        state=state,
        receipt=receipt,
        event=event,
        entered=entered,
        release=release,
        finished=finished,
        commits=commits,
        observed=observed,
    )


@pytest.mark.asyncio
async def test_cancellation_joins_commit_and_publication_exactly_once():
    fixture = boundary()
    record = await begin_decision_response(fixture.session, fixture.state, {})
    task = asyncio.create_task(record(DecisionResponse(value=True)))
    await fixture.entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    fixture.release.set()
    await fixture.finished.wait()
    assert not task.done()
    assert consume_decision_response(fixture.session, fixture.event)
    assert consume_decision_response(fixture.session, fixture.event)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert len(fixture.commits) == len(fixture.observed) == 1
    assert fixture.session._decision_completions == {}
    with pytest.raises(RuntimeError, match="already recorded"):
        await record(DecisionResponse(value=True))


@pytest.mark.asyncio
async def test_reader_termination_fails_publication_without_hanging():
    fixture = boundary()
    record = await begin_decision_response(fixture.session, fixture.state, {})
    task = asyncio.create_task(record(DecisionResponse(value=True)))
    await fixture.entered.wait()
    fixture.session._reader.set_result(None)
    fixture.release.set()
    with pytest.raises(NativeError, match="not delivered"):
        await asyncio.wait_for(task, 1)
    assert fixture.observed == []
    assert fixture.session._decision_completions == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("child", "field", "value"),
    [
        (False, "id", "next-turn"),
        (False, "subagent_id", "child"),
        (True, "subagent_incarnation", "successor"),
        (True, "subagent_instance_id", 18),
    ],
)
async def test_publication_rejects_another_owner(child, field, value):
    fixture = boundary(child=child)
    record = await begin_decision_response(fixture.session, fixture.state, {})
    task = asyncio.create_task(record(DecisionResponse(value=True)))
    await fixture.entered.wait()
    fixture.event[field] = value
    fixture.release.set()
    assert consume_decision_response(fixture.session, fixture.event)
    with pytest.raises(RuntimeError, match="another callback"):
        await asyncio.wait_for(task, 1)
    assert fixture.observed == []
    assert fixture.session._decision_completions == {}


@pytest.mark.asyncio
async def test_capture_rejects_another_owner_before_provider_work():
    fixture = boundary()
    fixture.receipt.thread_id = "another-thread"
    with pytest.raises(NativeError, match="another callback"):
        await begin_decision_response(fixture.session, fixture.state, {})
    assert fixture.commits == []
    assert fixture.session._decision_completions == {}
