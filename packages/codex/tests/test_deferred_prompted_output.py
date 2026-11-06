"""Prompted deferred roots preserve typed validation and actual restored authority."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from pydantic import BaseModel, Field

from agents import Agent, FunctionTool, InteractionRequired, NativeOutput, PromptedOutput, Turn
from agents.schema import OutputValidationError
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_deferred_jobs import Input, Journal, Output, Provider, declared, done
from tests.test_native_manual_tools import next_request, provider
from tests.test_native_output_modes import output_format


class FinalReport(BaseModel):
    report: str
    pages: int = Field(gt=0)


async def test_prompted_deferred_start_restores_actual_schema_and_future_default(
    responses: ResponsesFixture,
) -> None:
    journal, backend = Journal(), Provider([done("job-rendered")])
    jobs, tool = declared(journal, backend)
    entered = []

    class ScopedAgent(Agent):
        @asynccontextmanager
        async def session(self, **kwargs):
            restoring = kwargs.get("state") is not None
            entered.append(("enter", restoring, kwargs.get("context"), self.output_type))
            try:
                async with super().session(**kwargs) as session:
                    yield session
            finally:
                entered.append(("exit", restoring))

    default = NativeOutput(str)
    agent = ScopedAgent(instructions="Render.", provider=provider(responses), output_type=default)
    context = object()
    responses.respond(function_call("render", {"pages": 2}, "render-once"))
    responses.respond(assistant_output('{"report":"typed prompted","pages":2}'))
    responses.respond(assistant_output('{"response":"future native"}'))
    async with timeout(20), agent.session(tools=[tool], context=context) as live:
        original = await live.submit("Render", output_type=PromptedOutput(FinalReport))
        old = await next_request(live.interactions(), tool)
        async with jobs.start(live, old) as (restored, outcome):
            assert isinstance(outcome, Turn) and outcome is restored.restored_turn
            assert outcome.turn_id == original.turn_id
            assert outcome.submission_id != original.submission_id
            with pytest.raises((RuntimeError, ValueError)):
                await old.respond(Output(report="stale"))
            result = await outcome.get_result()
            assert result.final_output == FinalReport(report="typed prompted", pages=2)
            saved = SessionState.from_json(journal.state_json)
            assert saved.native_continuation is not None
            assert saved.native_continuation.output_schema is None
            assert saved.format_version == 5
            later = await restored.submit("Next")
            assert (await later.get_result()).final_output == "future native"
            assert (await outcome.get_result()).final_output == result.final_output
        with pytest.raises(InteractionRequired):
            await original.get_result()
    assert entered == [
        ("enter", False, context, default),
        ("enter", True, context, default),
        ("exit", True),
        ("exit", False),
    ]
    assert backend.calls == [("start", 2)] and backend.active == 0
    assert journal.log.count("prepare") == journal.log.count("save_result") == 1
    before, resumed, later_request = responses.requests
    assert output_format(before) is None and output_format(resumed) is None
    assert "job-rendered" in tool_output(resumed, "render-once")
    assert "FinalReport" in json.dumps(before["input"])
    assert "FinalReport" in json.dumps(resumed["input"])
    assert output_format(later_request)["schema"]["properties"]["response"]["type"] == "string"


async def test_explicit_prompted_prepare_preserves_completed_job_on_later_restore(
    responses: ResponsesFixture,
) -> None:
    journal, backend = Journal(), Provider([done("already-rendered")])
    jobs, tool = declared(journal, backend)
    manual = FunctionTool(
        name="approval", description="Approve", input_type=Input, output_type=Output
    )
    agent = Agent(instructions="Render.", tools=[tool, manual], provider=provider(responses))
    responses.respond(
        function_call("render", {"pages": 2}, "render"),
        function_call("approval", {"pages": 2}, "still-pending"),
    )
    async with timeout(25):
        async with agent.session() as live:
            await live.submit("Render", output_type=PromptedOutput(FinalReport))
            grant = await next_request(live.interactions(), tool)
            saved = await jobs.prepare(live, grant)
            assert saved == await live.suspend()
            with pytest.raises((RuntimeError, ValueError)):
                await grant.respond(Output(report="expired"))
        current = agent.with_output_type(PromptedOutput(FinalReport))
        with pytest.raises(ValueError, match="output contract"):
            async with agent.session(state=saved):
                pytest.fail("plain restore acquired prompted authority")
        assert backend.calls == []
        async with current.session(state=saved) as first:
            render = await next_request(first.interactions(), tool)
            assert isinstance(await jobs.advance(first, render), Turn)
            assert backend.calls == [("start", 2)]
            assert len(responses.requests) == 1
            # Abandon a different unfilled sibling. The journal still owns the prepared state.
        async with current.session(state=SessionState.from_json(journal.state_json)) as second:
            render = await next_request(second.interactions(), tool)
            outcome = await jobs.advance(second, render)
            assert isinstance(outcome, Turn)
            approval = await next_request(second.interactions(), manual)
            responses.respond(assistant_output('{"report":"resumed","pages":2}'))
            await approval.respond(Output(report="approved"))
            assert (await outcome.get_result()).final_output == FinalReport(
                report="resumed", pages=2
            )
        assert journal.log.count("save_result") == 1 and "claim-deliver" in journal.log
        assert backend.calls == [("start", 2)]
        assert len(responses.requests) == 2
        assert "already-rendered" in tool_output(responses.requests[-1], "render")
        assert "approved" in tool_output(responses.requests[-1], "still-pending")
        assert all(output_format(body) is None for body in responses.requests)


async def test_invalid_prompted_completion_rejects_output_without_replaying_job(
    responses: ResponsesFixture,
) -> None:
    journal, backend = Journal(), Provider([done()])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    responses.respond(function_call("render", {"pages": 1}, "one-effect"))
    responses.respond(assistant_output('{"report":"wrong","pages":0}'))
    async with timeout(15), agent.session() as live:
        await live.submit("Render", output_type=PromptedOutput(FinalReport))
        grant = await next_request(live.interactions(), tool)
        async with jobs.start(live, grant) as (_, outcome):
            assert isinstance(outcome, Turn)
            with pytest.raises(OutputValidationError):
                await outcome.get_result()
    assert backend.calls == [("start", 1)]
    assert journal.log.count("save_result") == 1
    assert len(responses.requests) == 2


@pytest.mark.parametrize("borrow", ["other-agent", "other-task"])
async def test_prompted_restore_contract_cannot_be_borrowed_during_hook_entry(
    responses: ResponsesFixture,
    borrow: str,
) -> None:
    journal, backend = Journal(), Provider([done()])
    jobs, tool = declared(journal, backend)
    original_provider = provider(responses)
    other = Agent(instructions="Render.", tools=[tool], provider=original_provider)
    attempted = []

    class CheckedAgent(Agent):
        @asynccontextmanager
        async def session(self, **kwargs):
            if kwargs.get("state") is not None:
                explicit = super().session

                async def cannot_restore():
                    target = other.session if borrow == "other-agent" else explicit
                    with pytest.raises(ValueError, match="output contract"):
                        async with target(**kwargs):
                            pytest.fail("Borrowed a prepared prompted contract")
                    attempted.append(borrow)

                if borrow == "other-agent":
                    await cannot_restore()
                else:
                    await asyncio.create_task(cannot_restore())
                assert not backend.calls and len(responses.requests) == 1
            async with super().session(**kwargs) as live:
                yield live

    agent = CheckedAgent(instructions="Render.", tools=[tool], provider=original_provider)
    responses.respond(function_call("render", {"pages": 4}, "live"))
    responses.respond(assistant_output('{"report":"owner only","pages":4}'))
    async with timeout(15), agent.session() as live:
        await live.submit("Render", output_type=PromptedOutput(FinalReport))
        grant = await next_request(live.interactions(), tool)
        async with jobs.start(live, grant) as (_, outcome):
            assert isinstance(outcome, Turn)
            assert (await outcome.get_result()).final_output == FinalReport(
                report="owner only", pages=4
            )
    assert attempted == [borrow]
    assert backend.calls == [("start", 4)]


@pytest.mark.parametrize("stop", ["denied", "cancelled"])
async def test_prompted_unconfirmed_prepare_keeps_joined_seal_and_expired_grants(
    responses: ResponsesFixture,
    stop: str,
) -> None:
    entered, rollback, release = (asyncio.Event() for _ in range(3))
    received = []

    class Store(Journal):
        async def prepare(self, state_json, bindings):
            received.append(state_json)
            entered.set()
            if stop == "denied":
                raise PermissionError("private denial")
            try:
                await asyncio.Future()
            finally:
                rollback.set()
                await release.wait()

    journal, backend = Store(), Provider([])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    responses.respond(function_call("render", {"pages": 2}, "joined"))
    async with timeout(15):
        async with agent.session() as live:
            original = await live.submit("Render", output_type=PromptedOutput(FinalReport))
            grant = await next_request(live.interactions(), tool)
            driver = asyncio.create_task(jobs.prepare(live, grant))
            try:
                await entered.wait()
                if stop == "cancelled":
                    driver.cancel()
                    await rollback.wait()
                    driver.cancel()
                    finished, _ = await asyncio.wait((driver,), timeout=0.01)
                    assert not finished
                    release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await driver
                else:
                    with pytest.raises(PermissionError, match="preparation was denied"):
                        await driver
                sealed = await live.suspend()
                assert received == [sealed.to_json()]
                assert await live.suspend() == sealed
                with pytest.raises((RuntimeError, ValueError)):
                    await grant.respond(Output(report="old"))
            finally:
                release.set()
                if not driver.done():
                    driver.cancel()
                await asyncio.gather(driver, return_exceptions=True)
        with pytest.raises(InteractionRequired) as paused:
            await original.get_result()
        assert paused.value.state == sealed
        async with agent.with_output_type(PromptedOutput(FinalReport)).session(
            state=sealed
        ) as fresh:
            current = await next_request(fresh.interactions(), tool)
            assert current.call_id == grant.call_id and current.submission_id != grant.submission_id
            with pytest.raises(PermissionError, match="operation was denied"):
                await jobs.advance(fresh, current)
        assert not backend.calls and not journal.rows and len(responses.requests) == 1


async def test_native_schema_root_stays_live_when_deferred_prepare_refuses(
    responses: ResponsesFixture,
) -> None:
    journal, backend = Journal(), Provider([])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    responses.respond(function_call("render", {"pages": 1}, "native"))
    responses.respond(assistant_output('{"report":"ordinary reply","pages":1}'))
    async with timeout(15), agent.session() as live:
        original = await live.submit("Render", output_type=NativeOutput(FinalReport))
        grant = await next_request(live.interactions(), tool)
        with pytest.raises(ValueError, match="could not be prepared"):
            async with jobs.start(live, grant):
                pytest.fail("Native pending checkpoint cannot be restored")
        assert not journal.log and not backend.calls
        await grant.respond(Output(report="manual"))
        assert (await original.get_result()).final_output == FinalReport(
            report="ordinary reply", pages=1
        )
    assert all(output_format(body) is not None for body in responses.requests)
