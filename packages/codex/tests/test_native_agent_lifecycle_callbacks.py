import asyncio
import json

import pytest
from pydantic import BaseModel

from agents import (
    Agent,
    AgentLifecycleContext,
    FunctionTool,
    NativeOutput,
    NativeProvider,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_manual_tools import next_request


class Answer(BaseModel):
    answer: str


@pytest.mark.parametrize("phase", ["start", "handoff", "end"])
async def test_agent_lifecycle_awaits_the_exact_native_owner(responses, phase):
    events = []
    entered, release = asyncio.Event(), asyncio.Event()
    context = {"owner": "application"}

    async def lifecycle(event: AgentLifecycleContext):
        assert event.context is context
        events.append((event.kind, event.agent.name))
        if event.kind == "handoff":
            assert event.agent is source
            assert event.target is target
        if event.kind == "end":
            assert event.agent is target
            assert isinstance(event.output, Answer)
            assert event.output.answer == "done"
        if event.kind == phase and (phase != "start" or event.agent is source):
            entered.set()
            await release.wait()

    target = Agent(
        name="B", instructions="Complete.", output_type=NativeOutput(Answer), on_lifecycle=lifecycle
    )
    responses.respond(function_call("transfer_to_b", {}, "lifecycle-transfer"))
    responses.respond(assistant_output('{"answer":"done"}'))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-lifecycle", supports_websockets=False
    ) as provider:
        source = Agent(
            name="A",
            instructions="Transfer.",
            model="gpt-5.5",
            provider=provider,
            handoffs=[target],
            on_lifecycle=lifecycle,
        )
        async with timeout(20), source.session(context=context) as session:

            async def drive():
                return await (await session.submit("Transfer.")).get_result()

            task = asyncio.create_task(drive())
            try:
                await entered.wait()
                assert not task.done()
                assert len(responses.requests) == {"start": 0, "handoff": 1, "end": 2}[phase]
                release.set()
                assert (await task).final_output.answer == "done"
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert events == [("start", "A"), ("handoff", "A"), ("start", "B"), ("end", "B")]


async def test_same_agent_handoff_starts_a_fresh_activation(responses):
    events = []

    async def lifecycle(event):
        events.append((event.kind, event.agent.name))
        assert event.agent is source
        if event.kind == "handoff":
            assert event.target is source

    responses.respond(function_call("transfer_to_a", {}, "self-transfer"))
    responses.respond(assistant_output("done"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-self", supports_websockets=False
    ) as provider:
        source = Agent(
            name="A",
            instructions="Transfer to yourself once.",
            model="gpt-5.5",
            provider=provider,
            handoffs=[lambda: source],
            on_lifecycle=lifecycle,
        )
        result = await source.run("Go.")
        assert result.final_output == "done"
    assert events == [("start", "A"), ("handoff", "A"), ("start", "A"), ("end", "A")]


async def test_rejected_handoff_keeps_source_and_never_starts_destination(responses):
    events = []
    failure = ValueError("source handoff rejected")

    async def lifecycle(event):
        events.append((event.kind, event.agent.name))
        if event.kind == "handoff":
            raise failure

    target = Agent(name="B", instructions="Complete.", on_lifecycle=lifecycle)
    responses.respond(function_call("transfer_to_b", {}, "rejected-transfer"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-rejected", supports_websockets=False
    ) as provider:
        source = Agent(
            name="A",
            instructions="Transfer.",
            model="gpt-5.5",
            provider=provider,
            handoffs=[target],
            on_lifecycle=lifecycle,
        )
        async with source.session() as session:
            with pytest.raises(ValueError, match="source handoff rejected") as caught:
                await (await session.submit("Go.")).get_result()
            assert caught.value is failure
            assert await session.get_active_agent() is source
    assert events == [("start", "A"), ("handoff", "A")]
    assert len(responses.requests) == 1


async def test_cancelled_start_joins_callback_cleanup_before_return(responses):
    entered, cleanup, release, joined = (asyncio.Event() for _ in range(4))

    async def lifecycle(event):
        assert event.kind == "start"
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup.set()
            await release.wait()
            joined.set()

    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-cancelled", supports_websockets=False
    ) as provider:
        source = Agent(
            name="A",
            instructions="Complete.",
            model="gpt-5.5",
            provider=provider,
            on_lifecycle=lifecycle,
        )
        async with timeout(20):
            task = asyncio.create_task(source.run("Go."))
            await entered.wait()
            task.cancel()
            try:
                await cleanup.wait()
                assert not task.done()
                assert responses.requests == []
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert joined.is_set()


async def test_cold_resume_restarts_owner_without_repeating_settled_effect(responses):
    events, effects = [], []

    async def lifecycle(event):
        events.append((event.kind, event.agent.name))

    @function_tool
    def effect() -> str:
        effects.append("effect")
        return "settled-once"

    class Arguments(BaseModel):
        key: str

    decision = FunctionTool(
        name="decision", description="Ask.", input_type=Arguments, output_type=str
    )
    responses.respond(
        function_call("effect", {}, "settled-effect"),
        function_call("decision", {"key": "one"}, "manual-gate"),
    )
    responses.respond(assistant_output("done"))
    async with NativeProvider(
        base_url=responses.url, api_key="synthetic-resume", supports_websockets=False
    ) as provider:
        source = Agent(
            name="A",
            instructions="Use both tools.",
            model="gpt-5.5",
            provider=provider,
            tools=[effect, decision],
            on_lifecycle=lifecycle,
        )
        async with timeout(20):
            async with source.session() as session:
                await session.submit("Go.")
                await next_request(session.interactions(), decision)
                saved = await session.suspend()
            assert effects == ["effect"]
            async with source.session(state=saved) as restored:
                request = await next_request(restored.interactions(), decision)
                await request.respond("approved-once")
                result = await restored.restored_turn.get_result()
                assert result.final_output == "done"
    assert effects == ["effect"]
    assert events == [("start", "A"), ("start", "A"), ("end", "A")]
    assert "settled-once" in json.dumps(responses.requests[-1]["input"])
