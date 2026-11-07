"""A retained root Session owns every unfinished output-repair Session."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agents import Agent, PromptedOutput, Session, get_cleanup
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_agent_declarations import _observe_implicit
from tests.test_native_prompted_repair import Report


@pytest.mark.parametrize(
    ("outcome", "fail_root"),
    [("corrected", False), ("primary", False), ("cancelled", False), ("corrected", True)],
)
async def test_repair_failure_retains_complete_root_cleanup(
    responses, monkeypatch: pytest.MonkeyPatch, outcome: str, fail_root: bool
) -> None:
    providers = _observe_implicit(monkeypatch, responses.url)
    repair_entered = asyncio.Event()
    repair_release = asyncio.Event()
    repairs: list[Session[Any, Any]] = []
    repair_failure = OSError("transient repair retirement")
    root_failure = OSError("transient root retirement")
    primary = RuntimeError("original repair operation failed")
    repair_failures = 0
    root_failed = False
    original_close = Session._close_session
    original_start = Session._start

    async def close(self: Session[Any, Any]) -> None:
        nonlocal repair_failures, root_failed
        if self._context_transform_purpose == "output_repair":
            if self not in repairs:
                repairs.append(self)
            if repair_failures < 2:
                repair_failures += 1
                raise repair_failure
        elif fail_root and not root_failed:
            root_failed = True
            raise root_failure
        await original_close(self)

    async def start(self: Session[Any, Any], *args: Any, **kwargs: Any):
        if self._context_transform_purpose == "output_repair" and outcome == "primary":
            raise primary
        return await original_start(self, *args, **kwargs)

    correction = assistant_output('{"label":"corrected"}')

    async def hold(event: str) -> None:
        if outcome == "cancelled" and event == correction:
            repair_entered.set()
            await repair_release.wait()

    responses.before_event = hold
    responses.respond(assistant_output('{"label":"x"}'))
    if outcome != "primary":
        responses.respond(correction)
    monkeypatch.setattr(Session, "_close_session", close)
    monkeypatch.setattr(Session, "_start", start)
    running = asyncio.create_task(
        Agent(instructions="Return a report.", output_type=PromptedOutput(Report, 1)).run("Report")
    )
    owner = None
    try:
        async with timeout(20):
            if outcome == "cancelled":
                await repair_entered.wait()
                running.cancel()
                repair_release.set()
            expected = (
                asyncio.CancelledError
                if outcome == "cancelled"
                else RuntimeError
                if outcome == "primary"
                else OSError
            )
            with pytest.raises(expected) as caught:
                await running
            if outcome != "cancelled":
                assert caught.value is (primary if outcome == "primary" else repair_failure)
            else:
                assert running.cancelled()
            owner = get_cleanup(caught.value)
            assert isinstance(owner, Session)
            assert len(repairs) == len(providers) == 1
            assert owner is not repairs[0]
            assert not repairs[0]._closed
            if fail_root:
                with pytest.raises(OSError) as retry:
                    await owner.aclose()
                assert retry.value is repair_failure
                assert get_cleanup(retry.value) is owner
            await owner.aclose()
            await owner.aclose()
            assert owner._closed and repairs[0]._closed
            assert vars(providers[0]).get("_closed") is True
    finally:
        repair_release.set()
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        repair_failures = 2
        for repair in repairs:
            await repair.aclose()
        if owner is not None:
            await owner.aclose()
        for provider in providers:
            await provider.aclose()


async def test_successful_repair_retires_owned_sessions_and_provider(
    responses, monkeypatch
) -> None:
    providers = _observe_implicit(monkeypatch, responses.url)
    retired: list[Session[Any, Any]] = []
    original_close = Session._close_session

    async def close(self: Session[Any, Any]) -> None:
        retired.append(self)
        await original_close(self)

    monkeypatch.setattr(Session, "_close_session", close)
    responses.respond(assistant_output('{"label":"x"}'))
    responses.respond(assistant_output('{"label":"corrected"}'))
    result = await Agent(
        instructions="Return a report.", output_type=PromptedOutput(Report, 1)
    ).run("Report")
    assert result.final_output == Report(label="corrected")
    assert len(retired) == 2 and all(session._closed for session in retired)
    assert len(providers) == 1 and vars(providers[0]).get("_closed") is True


async def test_later_turn_retries_previous_repair_before_opening_another(responses, monkeypatch):
    providers = _observe_implicit(monkeypatch, responses.url)
    repairs: list[Session[Any, Any]] = []
    allow_close = False
    failure = OSError("repair retirement remains unavailable")
    original_close = Session._close_session

    async def close(self: Session[Any, Any]) -> None:
        if self._context_transform_purpose == "output_repair":
            if self not in repairs:
                repairs.append(self)
            if not allow_close:
                raise failure
        await original_close(self)

    monkeypatch.setattr(Session, "_close_session", close)
    agent = Agent(instructions="Return a report.", output_type=PromptedOutput(Report, 1))
    try:
        async with agent.session() as session:
            responses.respond(assistant_output('{"label":"x"}'))
            responses.respond(assistant_output('{"label":"first correction"}'))
            with pytest.raises(OSError) as first:
                await (await session.submit("first")).get_result()
            assert first.value is failure
            original_repair = repairs[0]
            responses.respond(assistant_output('{"label":"y"}'))
            with pytest.raises(OSError) as second:
                await (await session.submit("second")).get_result()
            assert second.value is failure
            assert repairs == [original_repair]
            assert not original_repair._closed
            allow_close = True
            responses.respond(assistant_output('{"label":"z"}'))
            responses.respond(assistant_output('{"label":"third correction"}'))
            result = await (await session.submit("third")).get_result()
            assert result.final_output == Report(label="third correction")
            assert original_repair._closed
        assert len(repairs) == 2 and all(repair._closed for repair in repairs)
        assert vars(providers[0]).get("_closed") is True
    finally:
        allow_close = True
        for repair in repairs:
            await repair.aclose()
        for provider in providers:
            await provider.aclose()


async def test_native_cancellation_retains_public_cleanup(responses, monkeypatch) -> None:
    from agents import NativeProvider

    entered, release = asyncio.Event(), asyncio.Event()
    original_close = Session._close_session
    retained: list[Session[Any, Any]] = []
    failed = False

    async def hold(event: str) -> None:
        entered.set()
        await release.wait()

    async def close(self: Session[Any, Any]) -> None:
        nonlocal failed
        if not failed:
            failed = True
            retained.append(self)
            raise OSError("transient cancelled native retirement")
        await original_close(self)

    responses.before_event = hold
    responses.respond(assistant_output("done"))
    monkeypatch.setattr(Session, "_close_session", close)
    provider = NativeProvider(base_url=responses.url, api_key="synthetic")
    running = asyncio.create_task(Agent(instructions="Answer.", provider=provider).run("question"))
    try:
        async with timeout(20):
            await entered.wait()
            running.cancel()
            with pytest.raises(asyncio.CancelledError) as caught:
                await running
            owner = get_cleanup(caught.value)
            assert running.cancelled()
            assert owner is retained[0]
            assert not owner._closed
            await owner.aclose()
            await owner.aclose()
            assert owner._closed
            assert not vars(provider).get("_closed", False)
    finally:
        release.set()
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        for owner in retained:
            await owner.aclose()
        await provider.aclose()
