"""Session hooks can validate a checkpoint before yielding the restored Session."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import copy_context
from dataclasses import replace

import pytest

from agents import Agent, NativeProvider
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_deferred_jobs import Journal, Output, Provider, declared, done
from tests.test_deferred_opening_outcome import callback, pending
from tests.test_native_manual_tools import next_request, provider


async def test_session_hook_can_validate_then_reopen_without_changing_authority(responses):
    calls = []
    jobs, tool = declared(Journal(), Provider([done()]))

    class ValidatingAgent(Agent):
        @asynccontextmanager
        async def session(self, **kwargs):
            if kwargs.get("state") is not None:
                async with super().session(**kwargs) as probe:
                    assert (
                        await next_request(probe.interactions(), tool)
                    ).call_id == request.call_id
            async with super().session(**kwargs) as session:
                yield session

    agent = ValidatingAgent(
        instructions="Select.",
        tools=[tool, callback("opening", calls)],
        provider=provider(responses),
    )
    async with timeout(15), agent.session() as live:
        request = await pending(live, tool, responses)
        agent = replace(agent, tools=(tool, callback("future", calls)))
        responses.respond(function_call("select", {}, "restored"))
        responses.respond(assistant_output("finished"))
        async with jobs.start(live, request) as (_, turn):
            assert (await turn.get_result()).final_output == "finished"
    assert calls == ["opening"]


async def select_after_manual_reply(session, tool, responses):
    grant = await next_request(session.interactions(), tool)
    responses.respond(function_call("select", {}, "select-after-reply"))
    responses.respond(assistant_output("finished"))
    await grant.respond(Output(report="approved"))
    assert (await session.restored_turn.get_result()).final_output == "finished"


async def test_opening_declaration_requires_both_the_original_agent_and_checkpoint(
    responses, monkeypatch
):
    calls = []
    jobs, tool = declared(Journal(), Provider([done()]))
    other = Agent(
        instructions="Select.", tools=[tool, callback("other", calls)], provider=provider(responses)
    )

    class CheckingAgent(Agent):
        @asynccontextmanager
        async def session(self, **kwargs):
            if kwargs.get("state") is not None:
                first_request = len(responses.request_headers)
                async with other.session(**kwargs) as same_checkpoint:
                    await select_after_manual_reply(same_checkpoint, tool, responses)
                assert [
                    row["authorization"] for row in responses.request_headers[first_request:]
                ] == [
                    "Bearer synthetic-manual-test",
                    "Bearer synthetic-manual-test",
                ]
                first_request = len(responses.request_headers)
                async with super().session(state=earlier) as same_agent:
                    responses.respond(function_call("select", {}, "ordinary"))
                    responses.respond(assistant_output("earlier checkpoint"))
                    turn = await same_agent.submit("Select.")
                    assert (await turn.get_result()).final_output == "earlier checkpoint"
                assert [
                    row["authorization"] for row in responses.request_headers[first_request:]
                ] == [
                    "Bearer synthetic-future",
                    "Bearer synthetic-future",
                ]
                responses.respond(function_call("select", {}, "restored"))
                responses.respond(assistant_output("restored checkpoint"))
            async with super().session(**kwargs) as session:
                yield session

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-opening")
    agent = CheckingAgent(
        instructions="Select.",
        tools=[tool, callback("opening", calls)],
        provider=NativeProvider(base_url=responses.url),
    )
    async with timeout(15), agent.session() as live:
        earlier = await live.save_state()
        request = await pending(live, tool, responses)
        # Credential rotation distinguishes the original owner on a different checkpoint
        # without mutating the immutable Agent declaration.
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-future")
        async with jobs.start(live, request) as (_, turn):
            assert (await turn.get_result()).final_output == "restored checkpoint"
    assert calls == ["other", "opening", "opening"]
    assert [row["authorization"] for row in responses.request_headers[-2:]] == [
        "Bearer synthetic-opening",
        "Bearer synthetic-opening",
    ]


@pytest.mark.parametrize("denied", [False, True], ids=["public-body", "entry-error"])
async def test_copied_hook_context_expires_when_entry_finishes(responses, denied, monkeypatch):
    calls = []
    jobs, tool = declared(Journal(), Provider([done()]))
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-opening")
    agent = Agent(
        instructions="Select.",
        tools=[tool, callback("opening", calls)],
        provider=NativeProvider(base_url=responses.url),
    )
    open_session = agent.session

    @asynccontextmanager
    async def checked_session(**kwargs):
        nonlocal captured, state
        captured, state = copy_context(), kwargs["state"]
        if denied:
            raise PermissionError("Restore denied")
        async with open_session(**kwargs) as session:
            yield session

    async def explicit_restore():
        async with open_session(state=state) as restored:
            await select_after_manual_reply(restored, tool, responses)

    captured, state = None, None
    async with timeout(15), open_session() as live:
        request = await pending(live, tool, responses)
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-future")
        # A live Session's original owner supplies its current restore hook.
        agent.session = checked_session
        if denied:
            with pytest.raises(PermissionError, match="Restore denied"):
                async with jobs.start(live, request):
                    pytest.fail("Restore policy was bypassed")
            await captured.run(asyncio.create_task, explicit_restore())
        else:
            responses.respond(assistant_output("restored"))
            async with jobs.start(live, request) as (_, turn):
                assert (await turn.get_result()).final_output == "restored"
                await captured.run(asyncio.create_task, explicit_restore())
    assert calls == ["opening"]
    # The same Agent and checkpoint are no longer sufficient after hook entry.
    # A copied ContextVar must not reuse the opening credential in an explicit restore.
    assert [row["authorization"] for row in responses.request_headers[-2:]] == [
        "Bearer synthetic-future",
        "Bearer synthetic-future",
    ]
