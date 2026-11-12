"""Private checkpoint outcomes stay out of direct and root asyncio debug handlers."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, ConfigDict

from agents import Agent
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_deferred_jobs import Journal, Output, Provider, declared
from tests.test_deferred_opening_outcome import debug_logs
from tests.test_native_manual_tools import next_request, provider


async def test_prepare_rejects_raw_nested_input_without_logging_or_sealing(responses, caplog):
    class Nested(BaseModel):
        model_config = ConfigDict(strict=True, json_schema_extra={"additionalProperties": False})
        pages: int

    class Input(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        inner: Nested

    private = "z7Q"
    journal, backend = Journal(), Provider([])
    jobs, tool = declared(journal, backend, input_type=Input)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    responses.respond(function_call(tool.name, {"inner": {"pages": 1, "extra": private}}, "raw"))
    async with timeout(10), agent.session() as session:
        turn = await session.submit("Render.")
        request = await next_request(session.interactions(), tool)
        assert request.arguments.inner.model_dump() == {"pages": 1}
        async with debug_logs(caplog, private):
            with pytest.raises(ValueError, match="could not be prepared") as caught:
                await jobs.prepare(session, request)
            assert caught.value.__context__ is caught.value.__cause__ is None
            assert not journal.log and not backend.calls
        responses.respond(assistant_output("continued"))
        await request.respond(Output(report="manual"))
        assert (await turn.get_result()).final_output == "continued"


async def test_successful_prepare_keeps_checkpoint_private_and_restorable(responses, caplog):
    class Input(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")
        value: str

    private = "k4Z"
    journal, backend = Journal(), Provider([])
    jobs, tool = declared(journal, backend, input_type=Input)
    agent = Agent(instructions="Render.", tools=[tool], provider=provider(responses))
    responses.respond(function_call(tool.name, {"value": private}, "capture"))
    async with timeout(10), agent.session() as session:
        await session.submit("Render.")
        request = await next_request(session.interactions(), tool)
        native = session._native
        owners = []

        class CaptureOwner:
            def __getattr__(self, name):
                return getattr(native, name)

            async def _seal_manual(self, *args, **kwargs):
                owners.append(asyncio.current_task())
                return await native._seal_manual(*args, **kwargs)

        session._native = CaptureOwner()
        try:
            async with debug_logs(caplog, private):
                state = await jobs.prepare(session, request)
            assert len(owners) == 1 and owners[0].result() is None
        finally:
            session._native = native
        assert state.to_json() == journal.state_json and private in journal.state_json
        assert journal.log == ["prepare"] and not backend.calls
        async with agent.session(state=state) as restored:
            fresh = await next_request(restored.interactions(), tool)
            assert fresh.call_id == request.call_id and fresh.arguments.value == private


async def test_failed_idle_export_preserves_error_without_logging_its_value(responses, caplog):
    private = "f8X"
    failure = OSError(private)
    agent = Agent(instructions="Work.", provider=provider(responses))
    async with timeout(10), agent.session() as session:
        native = session._native

        owners = []

        class FailedExport:
            def __getattr__(self, name):
                return getattr(native, name)

            async def _export_checkpoint(self):
                owners.append(asyncio.current_task())
                await native._export_checkpoint()
                raise failure

        session._native = FailedExport()
        try:
            async with debug_logs(caplog, private):
                with pytest.raises(OSError) as caught:
                    await session.save_state()
                assert caught.value is failure
            assert len(owners) == 1 and owners[0].exception() is None
            assert owners[0].result() is None
        finally:
            session._native = native
        assert (await session.save_state()).source_session_id == session.id
