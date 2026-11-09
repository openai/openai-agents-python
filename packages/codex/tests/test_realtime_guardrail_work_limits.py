"""Guardrail prefix construction has bounded work as well as retained text."""

from __future__ import annotations

import asyncio

import pytest

from agents.realtime import RealtimeGuardrailInput, RealtimeGuardrailResult
from tests.test_realtime_guardrail_retention import output, owner_for, settle


async def test_tiny_deltas_exhaust_work_before_retained_text_capacity() -> None:
    owner = owner_for(lambda _: RealtimeGuardrailResult(False), threshold=None)
    with pytest.raises(RuntimeError, match="guardrail text work capacity"):
        for _ in range(12_000):
            owner._record_guardrail_text(output("tiny", "x"), final=False)
    assert not any(item.task is not None for item in owner._guardrail_items.values())
    owner._retire_response_output("tiny")


async def test_work_budget_is_shared_and_released_with_item_retirement(monkeypatch) -> None:
    seen: list[str] = []

    def check(value: RealtimeGuardrailInput) -> RealtimeGuardrailResult:
        seen.append(value.text)
        return RealtimeGuardrailResult(False)

    owner = owner_for(check, threshold=None)
    monkeypatch.setattr(owner, "_GUARDRAIL_TEXT_WORK_LIMIT", 48, raising=False)
    for delta in ("a" * 8, "b" * 8, "c" * 8):
        owner._record_guardrail_text(output("first", delta), final=False)
    owner._record_guardrail_text(output("second", "d" * 8), final=False)
    with pytest.raises(RuntimeError, match="guardrail text work capacity"):
        owner._record_guardrail_text(output("second", "e" * 8), final=False)
    owner._retire_response_output("first")
    owner._record_guardrail_text(output("second", "e" * 8), final=False)
    owner._record_guardrail_text(output("second", "replacement"), final=True)
    await settle(owner)
    assert seen == ["replacement"]
    # Physical completion releases the second item's contribution too.
    for delta in ("a" * 8, "b" * 8, "c" * 8):
        owner._record_guardrail_text(output("third", delta), final=False)
    owner._retire_response_output("third")


async def test_settled_partial_checks_do_not_reset_work_budget(monkeypatch) -> None:
    owner = owner_for(lambda _: RealtimeGuardrailResult(False))
    monkeypatch.setattr(owner, "_GUARDRAIL_TEXT_WORK_LIMIT", 24, raising=False)
    for _ in range(6):
        owner._record_guardrail_text(output("partial", "x"), final=False)
        await settle(owner)
    with pytest.raises(RuntimeError, match="guardrail text work capacity"):
        owner._record_guardrail_text(output("partial", "x"), final=False)
    owner._retire_response_output("partial")


async def test_final_check_keeps_work_reserved_until_cancellation_finishes(monkeypatch) -> None:
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def check(value: RealtimeGuardrailInput) -> RealtimeGuardrailResult:
        if value.response_id == "held":
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
        return RealtimeGuardrailResult(False)

    owner = owner_for(check, threshold=None)
    monkeypatch.setattr(owner, "_GUARDRAIL_TEXT_WORK_LIMIT", 24, raising=False)
    owner._record_guardrail_text(output("held", "a" * 8), final=False)
    owner._record_guardrail_text(output("held", "b" * 8), final=False)
    owner._record_guardrail_text(output("held", "final"), final=True)
    await entered.wait()
    retiring = None
    try:
        owner._record_guardrail_text(output("other", "c" * 8), final=False)
        with pytest.raises(RuntimeError, match="guardrail text work capacity"):
            owner._record_guardrail_text(output("other", "d" * 8), final=False)
        retiring = asyncio.create_task(owner._cancel_guardrails("held"))
        await cancelled.wait()
        assert not retiring.done()
        with pytest.raises(RuntimeError, match="guardrail text work capacity"):
            owner._record_guardrail_text(output("other", "d" * 8), final=False)
    finally:
        release.set()
        if retiring is not None:
            await retiring
        else:
            await owner._cancel_guardrails("held")
    owner._record_guardrail_text(output("other", "d" * 8), final=False)
    owner._record_guardrail_text(output("other", "completed"), final=True)
    await settle(owner)


async def test_empty_deltas_do_not_spend_prefix_work(monkeypatch) -> None:
    owner = owner_for(lambda _: RealtimeGuardrailResult(False), threshold=None)
    monkeypatch.setattr(owner, "_GUARDRAIL_TEXT_WORK_LIMIT", 16, raising=False)
    owner._record_guardrail_text(output("empty", "a" * 8), final=False)
    owner._record_guardrail_text(output("empty", "b" * 8), final=False)
    for _ in range(100):
        owner._record_guardrail_text(output("empty", ""), final=False)
    owner._record_guardrail_text(output("empty", "final"), final=True)
    await settle(owner)


async def test_threshold_snapshots_coalesce_and_final_text_replaces_prefix() -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    seen: list[RealtimeGuardrailInput] = []

    async def check(value: RealtimeGuardrailInput) -> RealtimeGuardrailResult:
        seen.append(value)
        if len(seen) == 1:
            entered.set()
            await release.wait()
        return RealtimeGuardrailResult(False)

    owner = owner_for(check, threshold=3)
    owner._record_guardrail_text(output("snapshots", "a"), final=False)
    owner._record_guardrail_text(output("snapshots", "bc"), final=False)
    await entered.wait()
    try:
        for delta in ("d", "ef", "ghi"):
            owner._record_guardrail_text(output("snapshots", delta), final=False)
        owner._record_guardrail_text(output("snapshots", "rewritten"), final=True)
        assert seen[0].text == "abc" and not seen[0].final
    finally:
        release.set()
        await settle(owner)
    assert [(value.text, value.final) for value in seen] == [
        ("abc", False),
        ("rewritten", True),
    ]
