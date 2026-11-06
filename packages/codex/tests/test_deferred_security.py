"""Private provider facts can cross into a manual reply only through a bounded projection."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

import pytest
from pydantic import BaseModel, ConfigDict

from agents import Agent, FunctionTool, NativeError, Turn
from agents.experimental.deferred import JobUpdate
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_deferred_jobs import Journal, Output, Provider, declared, done, prepare_one
from tests.test_native_manual_tools import next_request, provider


@pytest.mark.parametrize(
    "public_value,limit",
    [
        ("secret-task-receipt", 128),
        ("prefix-private-operation-key", 128),
        ("synthetic-manual-test", 128),
        ("é" * 18, 32),
        ("x" * 200, 128),
    ],
)
async def test_private_or_oversized_projection_is_never_replied(
    responses, caplog, capsys, public_value, limit
):
    journal = Journal()
    backend = Provider([done(public_value)])
    jobs, tool = declared(journal, backend, max_output_bytes=limit)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(5):
        state, _ = await prepare_one(agent, jobs, tool, responses)
        for _ in range(2):
            async with agent.session(state=state) as session:
                req = await next_request(session.interactions(), tool)
                with pytest.raises(ValueError) as error:
                    await jobs.advance(session, req)
                assert error.value.__context__ is None and error.value.__cause__ is None
                assert str(error.value) == "Deferred job operation could not be completed"
                history = [item async for item in session.items()]
                assert not any(item["type"] == "function_call_output" for item in history)
    [row] = journal.rows.values()
    assert row.update.status == "completed" and row.result is None
    assert backend.calls == [("start", 2)] and len(responses.requests) == 1
    outputs = capsys.readouterr()
    sinks = (
        json.dumps(responses.requests),
        state.to_json(),
        repr(row.update),
        outputs.out,
        outputs.err,
        repr([record.__dict__ for record in caplog.records]),
    )
    for secret in (
        "secret-task-receipt",
        "private-operation-key",
        "private-artifact",
        public_value,
    ):
        assert all(secret not in sink for sink in sinks)


async def test_incidental_receipt_match_leaves_request_open_for_reviewed_reply(responses):
    journal = Journal()
    update = JobUpdate("completed", "a", {"public": "ready"})
    backend = Provider([update])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(5):
        state, _ = await prepare_one(agent, jobs, tool, responses)
        async with agent.session(state=state) as session:
            request = await next_request(session.interactions(), tool)
            with pytest.raises(ValueError, match="Deferred job operation could not be completed"):
                await jobs.advance(session, request)
            [row] = journal.rows.values()
            assert row.update == update and row.result is None
            assert len(responses.requests) == 1

            responses.respond(assistant_output("done"))
            await request.respond(Output(report="ready"))
            assert (await session.restored_turn.get_result()).final_output == "done"
    assert len(responses.requests) == 2
    assert tool_output(responses.requests[-1], request.call_id) == '{"report":"ready"}'
    assert row.result is None and backend.calls == [("start", 2)]


async def test_projection_must_match_actual_manual_wire_bytes(responses):
    journal, backend = Journal(), Provider([done("public")])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(5):
        state, original = await prepare_one(agent, jobs, tool, responses)
        journal.forbidden.add("deliver")
        async with agent.session(state=state) as first:
            req = await next_request(first.interactions(), tool)
            with pytest.raises(PermissionError):
                await jobs.advance(first, req)
        [row] = journal.rows.values()
        approved = row.result
        assert approved == '{"report":"public"}'
        journal.forbidden.clear()
        async with agent.session(state=state) as fresh:
            req = await next_request(fresh.interactions(), tool)
            row.result = r'{"report":"\u0070ublic"}'
            with pytest.raises(ValueError):
                await jobs.advance(fresh, req)
            assert (await next_request(fresh.interactions(), tool)).call_id == original.call_id
            assert len(responses.requests) == 1
            row.result = approved
            responses.respond(assistant_output("done"))
            turn = await jobs.advance(fresh, req)
            assert isinstance(turn, Turn) and (await turn.get_result()).final_output == "done"
    assert tool_output(responses.requests[-1], original.call_id) == approved
    assert backend.calls == [("start", 2)] and journal.log.count("project") == 1


async def test_projector_and_store_exception_context_stays_private(responses):
    journal, backend = Journal(), Provider([done()])

    def project(value: object) -> Output:
        try:
            raise KeyError("private-artifact")
        except KeyError as cause:
            raise RuntimeError("private-business-credential") from cause

    jobs, tool = declared(journal, backend, project=project)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(5):
        state, _ = await prepare_one(agent, jobs, tool, responses)
        for denied in (True, False):
            if denied:
                journal.forbidden.add("record")
            else:
                journal.forbidden.clear()
                # Reconcile the same target key after uncertain receipt persistence.
                backend.updates.append(done())
            async with agent.session(state=state) as session:
                req = await next_request(session.interactions(), tool)
                with pytest.raises((PermissionError, ValueError)) as error:
                    await jobs.advance(session, req)
                assert error.value.__context__ is None and error.value.__cause__ is None
                assert "private" not in repr(error.value)
    assert backend.calls == [("start", 2), ("reconcile", 2)] and len(responses.requests) == 1
    assert not next(iter(journal.rows.values())).result


async def test_uncertain_input_and_cancel_do_not_send_a_create(responses):
    journal, backend = Journal(), Provider([done()])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    backend.updates = [
        JobUpdate("input_required", "secret-task-receipt", "private-input"),
        ConnectionError("private-response-ack"),
        ConnectionError("private-cancel-ack"),
        JobUpdate("cancelled"),
    ]
    async with timeout(5):
        state, _ = await prepare_one(agent, jobs, tool, responses)
        for operation, expected in [
            ("advance", "input_required"),
            ("respond", "unknown"),
            ("respond", "unknown"),
            ("cancel", "unknown"),
            ("cancel", "unknown"),
            ("advance", "cancelled"),
        ]:
            async with agent.session(state=state) as session:
                req = await next_request(session.interactions(), tool)
                assert (
                    await jobs.advance(
                        session,
                        req,
                        operation=operation,
                        response="private-approved-input" if operation == "respond" else None,
                    )
                    == expected
                )
    assert [op for op, _ in backend.calls] == ["start", "respond", "cancel", "observe"]
    assert len(responses.requests) == 1


async def test_caller_cancellation_does_not_change_remote_job_or_leave_polling(responses):
    journal, backend = Journal(), Provider([done()])
    backend.block = "start"
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    async with timeout(5):
        state, _ = await prepare_one(agent, jobs, tool, responses)
        async with agent.session(state=state) as session:
            req = await next_request(session.interactions(), tool)
            driving = asyncio.create_task(jobs.advance(session, req))
            await backend.entered.wait()
            driving.cancel()
            with pytest.raises(asyncio.CancelledError):
                await driving
            assert backend.active == 0
        async with agent.session(state=state) as session:
            current = await next_request(session.interactions(), tool)
            responses.respond(assistant_output("reconciled"))
            result = await jobs.advance(session, current)
            assert (
                isinstance(result, Turn)
                and (await result.get_result()).final_output == "reconciled"
            )
    assert backend.calls == [("start", 2), ("reconcile", 2)]


@pytest.mark.parametrize("arguments", [{"pages": "2"}, {"pages": 2, "private": "unexpected"}])
async def test_invalid_raw_manual_arguments_never_prepare_or_start(responses, arguments):
    journal, backend = Journal(), Provider([])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    responses.respond(function_call("render", arguments, "bad"))
    async with timeout(5), agent.session() as session:
        turn = await session.submit("Render.")
        with pytest.raises(NativeError, match="arguments could not be delivered"):
            await turn.get_result()
    assert not backend.calls and not journal.rows


async def test_subsequent_model_step_can_prepare_a_new_call_on_the_same_turn(responses):
    journal, backend = Journal(), Provider([done("first"), done("second")])
    jobs, tool = declared(journal, backend)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    responses.respond(function_call("render", {"pages": 1}, "first"))
    responses.respond(function_call("render", {"pages": 2}, "second"))
    responses.respond(assistant_output("both rendered"))
    async with timeout(5), agent.session() as first:
        original = await first.submit("Render twice.")
        req = await next_request(first.interactions(), tool)
        async with jobs.start(first, req) as (second, first_reply):
            assert isinstance(first_reply, Turn)
            next_req = await next_request(second.interactions(), tool)
            async with jobs.start(second, next_req) as (last, second_reply):
                assert isinstance(second_reply, Turn) and second_reply.turn_id == original.turn_id
                assert (await second_reply.get_result()).final_output == "both rendered"
                assert (await last.save_state()).format_version == 4
    assert backend.calls == [("start", 1), ("start", 2)] and len(responses.requests) == 3
    assert json.loads(tool_output(responses.requests[-1], "second")) == {"report": "second"}


def test_ordinary_import_and_generic_deferred_have_no_optional_mcp_dependency():
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys, agents; assert 'agents.experimental.deferred' not in sys.modules; "
            "from agents.experimental.deferred import DeferredTools; "
            "assert 'mcp' not in sys.modules",
        ],
        check=True,
    )


async def test_original_raw_fields_cannot_disappear_behind_misdeclared_nested_schema(responses):
    class Misdeclared(BaseModel):
        model_config = ConfigDict(strict=True, json_schema_extra={"additionalProperties": False})
        pages: int

    class Wrapper(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        inner: Misdeclared

    from agents.experimental.deferred import DeferredTools

    journal, backend = Journal(), Provider([])
    jobs = DeferredTools(journal=journal, tenant="tenant-a", run_id="run")
    tool = FunctionTool(
        name="render",
        description="Render.",
        input_type=Wrapper,
        output_type=Output,
    )
    jobs.register(
        tool,
        provider_id="v1",
        provider=backend,
        project=lambda value: Output(report="unused"),
    )
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    responses.respond(
        function_call("render", {"inner": {"pages": 1, "unexpected": "private"}}, "raw")
    )
    async with timeout(5), agent.session() as session:
        await session.submit("Render.")
        req = await next_request(session.interactions(), tool)
        assert req.arguments.inner.model_dump() == {"pages": 1}
        with pytest.raises(ValueError, match="could not be prepared"):
            await jobs.prepare(session, req)
    assert not backend.calls and not journal.rows
