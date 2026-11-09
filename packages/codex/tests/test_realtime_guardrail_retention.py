"""Completed guardrails release transcripts without abandoning active callbacks."""

from __future__ import annotations

import asyncio
from contextvars import copy_context
from types import SimpleNamespace
from typing import Any, cast

import pytest

from _agents_codex.realtime import RealtimeConfig, RealtimeSession
from agents import Session
from agents.realtime import RealtimeGuardrailInput, RealtimeGuardrailResult, RealtimeOutputGuardrail


def owner_for(callback: Any, threshold: int | None = 1) -> RealtimeSession:
    session = cast(
        Session[Any, Any],
        SimpleNamespace(_context=None, _agent_for_profile=lambda _: None),
    )
    owner = RealtimeSession(
        session,
        config=RealtimeConfig(
            output_guardrails=(RealtimeOutputGuardrail("policy", callback),),
            guardrail_text_threshold=threshold,
        ),
    )
    owner._live = True
    owner._context = copy_context()
    return owner


def output(response: str, text: str) -> dict[str, Any]:
    return {
        "response_id": response,
        "item_id": f"item-{response}",
        "content_index": 0,
        "output_kind": "text",
        "delta": text,
        "text": text,
    }


async def settle(owner: RealtimeSession) -> None:
    tasks = tuple(item.task for item in owner._guardrail_items.values() if item.task is not None)
    await asyncio.gather(*tasks)
    while not owner._queue.empty():
        owner._dequeue()


async def test_guardrails_reclaim_more_than_the_historical_item_capacity() -> None:
    seen: list[str] = []

    def check(value: RealtimeGuardrailInput) -> RealtimeGuardrailResult:
        seen.append(value.response_id)
        return RealtimeGuardrailResult(False)

    owner = owner_for(check)
    for index in range(1030):
        owner._record_guardrail_text(output(str(index), "final"), final=True)
        await settle(owner)
    assert len(seen) == 1030
    assert owner._live


async def test_guardrails_release_historical_text_and_exact_identity_reservations() -> None:
    owner = owner_for(lambda _: RealtimeGuardrailResult(False))
    # An unrelated reservation must survive every completed guardrail.
    owner._reserve_identity("unrelated")
    for index in range(5):
        owner._record_guardrail_text(output(f"{index}-" + "i" * 400_000, "x" * 600_000), final=True)
        await settle(owner)
    assert owner._identity_bytes == 4 * len("unrelated")


async def test_recent_final_receipts_deduplicate_and_reject_mutations() -> None:
    seen: list[str] = []

    def check(value: RealtimeGuardrailInput) -> RealtimeGuardrailResult:
        seen.append(value.text)
        return RealtimeGuardrailResult(False)

    owner = owner_for(check)
    owner._record_guardrail_text(output("first", "final"), final=True)
    await settle(owner)
    owner._record_guardrail_text(output("first", "final"), final=True)
    assert seen == ["final"]
    with pytest.raises(RuntimeError, match="changed after"):
        owner._record_guardrail_text(output("first", "changed"), final=True)
    with pytest.raises(RuntimeError, match="changed after"):
        owner._record_guardrail_text(output("first", "late"), final=False)
    owner._record_guardrail_text(output("unicode", "\ud800"), final=True)
    await settle(owner)
    with pytest.raises(RuntimeError, match="changed after"):
        owner._record_guardrail_text(output("unicode", "\ud801"), final=True)


async def test_blocked_final_callback_remains_owned_until_physical_cleanup() -> None:
    entered, cancelled, release, cleaned = (asyncio.Event() for _ in range(4))

    async def check(value: RealtimeGuardrailInput) -> RealtimeGuardrailResult:
        if value.response_id == "held":
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            finally:
                cleaned.set()
        return RealtimeGuardrailResult(False)

    owner = owner_for(check)
    owner._record_guardrail_text(output("held", "x" * 600_000), final=True)
    await entered.wait()
    retiring: asyncio.Task[None] | None = None
    try:
        for index in range(1030):
            owner._record_guardrail_text(output(str(index), "final"), final=True)
            tasks = tuple(
                item.task
                for item in owner._guardrail_items.values()
                if item.task is not None and item.snapshot.response_id != "held"
            )
            await asyncio.gather(*tasks)
            while not owner._queue.empty():
                owner._dequeue()
        with pytest.raises(RuntimeError, match="text capacity"):
            owner._record_guardrail_text(output("overflow", "x" * 600_000), final=True)
        retiring = asyncio.create_task(owner._cancel_guardrails("held"))
        await cancelled.wait()
        assert not retiring.done()
        assert not cleaned.is_set()
        release.set()
        await retiring
        assert cleaned.is_set()
        assert owner._queue.empty()
        owner._record_guardrail_text(output("held", "late final"), final=True)
        assert owner._queue.empty()
    finally:
        release.set()
        if retiring is not None:
            await retiring
        await owner._cancel_guardrails()


@pytest.mark.parametrize("threshold", [None, 1])
async def test_retired_partial_rows_release_after_owned_work_settles(threshold: int | None) -> None:
    seen: list[str] = []

    def check(value: RealtimeGuardrailInput) -> RealtimeGuardrailResult:
        seen.append(value.response_id)
        return RealtimeGuardrailResult(False)

    owner = owner_for(check, threshold)
    for index in range(1030):
        response = str(index)
        owner._record_guardrail_text(output(response, "partial"), final=False)
        owner._retire_response_output(response)
        await owner._cancel_guardrails(response)
    for index in range(1030):
        owner._retire_response_output(f"other-{index}")
    owner._record_guardrail_text(output("1029", "late final"), final=True)
    await settle(owner)
    assert not seen
    assert owner._live


async def test_cancellation_before_worker_first_step_releases_retired_row() -> None:
    owner = owner_for(lambda _: RealtimeGuardrailResult(False))
    owner._record_guardrail_text(output("first", "final"), final=True)
    await owner._cancel_guardrails("first")
    assert owner._identity_bytes == 0
    assert not owner._guardrail_items
    owner._record_guardrail_text(output("first", "late"), final=False)
    assert not owner._guardrail_items


async def test_live_oversized_transcript_remains_bounded() -> None:
    owner = owner_for(lambda _: RealtimeGuardrailResult(False))
    with pytest.raises(RuntimeError, match="text capacity"):
        owner._record_guardrail_text(output("large", "x" * 1_048_577), final=True)
    assert not any(item.task is not None for item in owner._guardrail_items.values())
