"""Deferred restore keeps the selected root output and the opening default."""

from contextlib import asynccontextmanager

import pytest

from agents import Agent, InteractionRequired, NativeOutput, PromptedOutput
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_deferred_jobs import Journal, Output, Provider, declared, done
from tests.test_native_manual_tools import next_request, provider
from tests.test_native_output_modes import output_format


@pytest.mark.parametrize(
    "default, later_reply, later_value",
    [
        pytest.param(None, "plain default", "plain default", id="plain"),
        pytest.param(
            NativeOutput(str), '{"response":"native default"}', "native default", id="native-str"
        ),
        pytest.param(
            PromptedOutput(str), '"prompted default"', "prompted default", id="prompted-str"
        ),
    ],
)
async def test_deferred_plain_override_resumes_once_then_restores_opening_default(
    responses, default, later_reply, later_value
):
    journal, backend = Journal(), Provider([done("synthetic-report")])
    jobs, tool = declared(journal, backend)
    lifecycle = []

    class ScopedAgent(Agent):
        @asynccontextmanager
        async def session(self, **kwargs):
            restoring = kwargs.get("state") is not None
            lifecycle.append(("enter", restoring, self.output_type))
            try:
                async with super().session(**kwargs) as session:
                    yield session
            finally:
                lifecycle.append(("exit", restoring, self.output_type))

    connection = provider(responses)
    agent = ScopedAgent(
        instructions="Render.", tools=[tool], provider=connection, output_type=default
    )
    responses.respond(function_call("render", {"pages": 2}, "synthetic-render"))
    responses.respond(assistant_output("synthetic plain result"))
    responses.respond(assistant_output(later_reply))
    try:
        async with timeout(20), agent.session() as live:
            original = await live.submit("Render a synthetic report.", output_type=str)
            request = await next_request(live.interactions(), tool)
            async with jobs.start(live, request) as (restored, resumed):
                assert resumed is restored.restored_turn
                assert resumed.turn_id == original.turn_id
                assert resumed.submission_id != original.submission_id
                with pytest.raises(
                    (RuntimeError, ValueError), match="foreign|expired|delivered|open"
                ):
                    await request.respond(Output(report="synthetic recovery"))
                assert (await resumed.get_result()).final_output == "synthetic plain result"
                state = SessionState.from_json(journal.state_json)
                assert state.format_version == 5 and state.native_continuation is not None
                assert state.native_continuation.output_schema is None
                later = await restored.submit("Now use the opening default.")
                assert (await later.get_result()).final_output == later_value
                assert (await resumed.get_result()).final_output == "synthetic plain result"
                assert (await restored.save_state()).format_version == 4
            assert backend.active == 0
            assert backend.calls == [("start", 2)]
            assert journal.log.count("prepare") == journal.log.count("claim-start") == 1
            assert journal.log.count("save_result") == 1
            with pytest.raises(InteractionRequired):
                await original.get_result()
        assert lifecycle == [
            ("enter", False, default),
            ("enter", True, default),
            ("exit", True, default),
            ("exit", False, default),
        ]
        first, continuation, later_request = responses.requests
        assert output_format(first) is None and output_format(continuation) is None
        assert "synthetic-report" in tool_output(continuation, "synthetic-render")
        if isinstance(default, NativeOutput):
            assert (
                output_format(later_request)["schema"]["properties"]["response"]["type"] == "string"
            )
        else:
            assert output_format(later_request) is None
        if isinstance(default, PromptedOutput):
            assert "does not apply to later requests" in str(later_request["input"])
    finally:
        await connection.aclose()


@pytest.mark.parametrize(
    "default", [NativeOutput(str), PromptedOutput(str)], ids=["native", "prompted"]
)
async def test_deferred_restore_denial_still_expires_original_grant_and_starts_no_job(
    responses, default
):
    journal, backend = Journal(), Provider([done()])
    jobs, tool = declared(journal, backend)
    exits = []

    class DenyingAgent(Agent):
        @asynccontextmanager
        async def session(self, **kwargs):
            if kwargs.get("state") is not None:
                raise PermissionError("synthetic restore denied")
            try:
                async with super().session(**kwargs) as session:
                    yield session
            finally:
                exits.append("joined")

    connection = provider(responses)
    agent = DenyingAgent(
        instructions="Render.", tools=[tool], provider=connection, output_type=default
    )
    responses.respond(function_call("render", {"pages": 2}, "synthetic-denied"))
    try:
        async with timeout(20), agent.session() as live:
            original = await live.submit("Render.", output_type=str)
            request = await next_request(live.interactions(), tool)
            with pytest.raises(PermissionError, match="synthetic restore denied"):
                async with jobs.start(live, request):
                    pytest.fail("The original subclass must own restore admission.")
            assert journal.log == ["prepare"] and journal.state_json
            assert backend.calls == [] and backend.active == 0
            with pytest.raises((RuntimeError, ValueError), match="foreign|expired|delivered|open"):
                await request.respond(Output(report="synthetic recovery"))
        with pytest.raises(InteractionRequired) as paused:
            await original.get_result()
        assert paused.value.state is not None
        assert paused.value.state.to_json() == journal.state_json
        assert len(responses.requests) == 1 and exits == ["joined"]
    finally:
        await connection.aclose()
