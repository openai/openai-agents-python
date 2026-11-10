"""Application acceptance keeps native roots and children in their owning turn."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from threading import Event

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import agents
from agents import (
    Agent,
    CompletionContext,
    ContinueTurn,
    NativeError,
    PromptedOutput,
    RunBudget,
    StopTurn,
    TurnStoppedError,
    function_tool,
)
from agents.completion import CompletionContext as PublicCompletionContext
from agents.observation import TurnFinished
from agents.tools import ToolOutput, ToolOutputText
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider
from tests.test_native_child_agents import ChildPeer, agent_for, handles, metadata, trust
from tests.test_native_executor_tools import declaration
from tests.test_native_provider_websockets import peer as websocket_peer


def test_public_declaration_preserves_callback() -> None:
    assert agents.CompletionContext is PublicCompletionContext is CompletionContext
    assert {"CompletionContext", "ContinueTurn"} <= set(agents.__all__)
    assert CompletionContext.__module__ == "agents.completion"
    assert ContinueTurn.__module__ == "agents.completion"

    def complete(request):
        return None

    agent = Agent(instructions="Answer.", before_complete=complete)
    assert replace(agent, name="Other").before_complete is complete
    assert agent.with_output_type(PromptedOutput(int)).before_complete is complete
    assert "private feedback" not in repr(ContinueTurn("private feedback"))


@pytest.mark.parametrize("async_callback", [False, True])
async def test_missing_artifact_continues_same_turn_with_full_tool_result(
    responses, tmp_path, async_callback
):
    artifact = tmp_path / "report.md"
    fact = "Revenue increased from 100 to 125 units [source-17]."
    archive = "irrelevant-archive " * 20_000
    calls, steps, projections = [], [], []

    def reduce_result(context, output):
        full_text = "".join(
            part.text if isinstance(part, ToolOutputText) else part.get("text", "")
            for part in output.content_items
        )
        assert fact in full_text
        assert len(full_text) > 500_000
        return ToolOutput.text(fact)

    @function_tool(after=reduce_result)
    def research() -> str:
        return archive + fact + archive

    @function_tool
    def write_report() -> str:
        artifact.write_text(fact)
        return "Report written."

    def project(request):
        projections.append(request)
        assert "irrelevant-archive" not in json.dumps(request.items)
        return None

    def validate(request):
        calls.append(request)
        assert request.context is artifact
        if not artifact.exists():
            return ContinueTurn("Write report.md with the cited research before finishing.")
        assert artifact.read_text() == fact
        return None

    async def validate_async(request):
        await asyncio.sleep(0)
        return validate(request)

    def before_step(turn, *, step_index):
        steps.append(step_index)

    responses.respond(function_call("research", {}, "research"))
    responses.respond(assistant_output("Done."))
    responses.respond(function_call("write_report", {}, "write"))
    responses.respond(assistant_output("Report ready."))
    agent = Agent(
        instructions="Research and write the requested report.",
        tools=[research, write_report],
        provider=provider(responses),
        transform_context=project,
        before_complete=validate_async if async_callback else validate,
    )
    async with timeout(20), agent.session(context=artifact) as session:
        turn = await session.submit(
            "Prepare the report.", before_step=before_step, budget=RunBudget(max_requests=4)
        )
        result = await turn.get_result()
        saved = await session.save_state()
        events = [event async for event in turn.events()]
        assert all(call.session_id == session.id for call in calls)
    assert result.final_output == "Report ready."
    assert result.turn is turn
    assert artifact.read_text() == fact
    assert [call.text for call in calls] == ["Done.", "Report ready."]
    assert [call.continuation_count for call in calls] == [0, 1]
    assert {call.turn_id for call in calls} == {turn.turn_id}
    assert all(call.subagent_id is None for call in calls)
    assert steps == [0, 1, 2, 3]
    assert len(projections) == len(responses.requests) == 4
    assert len([event for event in events if isinstance(event, TurnFinished)]) == 1
    assert "Write report.md" in json.dumps(responses.requests[2]["input"])
    assert fact in json.dumps(responses.requests[-1]["input"])
    assert "irrelevant-archive" not in saved.to_json()
    assert "Write report.md" in saved.to_json()
    assert "Done." not in repr(calls)


async def test_run_sync_and_restore_use_current_callback(responses):
    calls = []

    def first(request):
        calls.append(("first", request.context, request.continuation_count))
        return None

    async def current(request):
        calls.append(("current", request.context, request.continuation_count))
        return ContinueTurn("Check the source.") if request.continuation_count == 0 else None

    responses.respond(assistant_output("Initial."))
    agent = Agent(instructions="Answer.", provider=provider(responses), before_complete=first)
    async with timeout(20):
        initial = await asyncio.to_thread(
            agent.run_sync, "Start.", context="old", return_state=True
        )
        assert initial.state is not None
        responses.respond(assistant_output("Unchecked."))
        responses.respond(assistant_output("Checked."))
        resumed = await replace(agent, before_complete=current).run(
            "Continue.", context="new", state=initial.state
        )
        responses.respond(assistant_output("No gate."))
        plain = await replace(agent, before_complete=None).run("Continue.", state=initial.state)
    assert resumed.final_output == "Checked."
    assert plain.final_output == "No gate."
    assert calls == [("first", "old", 0), ("current", "new", 0), ("current", "new", 1)]


async def test_websocket_continuation_preserves_feedback_and_turn():
    calls = []

    def complete(request):
        calls.append(request)
        return ContinueTurn("Verify the source.") if request.continuation_count == 0 else None

    async with timeout(20), websocket_peer() as server:
        server.respond(assistant_output("Unverified."))
        server.respond(assistant_output("Verified."))
        agent = Agent(instructions="Answer.", provider=server.provider(), before_complete=complete)
        result = await agent.run("Check the source.")
        assert result.final_output == "Verified."
        assert len(server.requests) == 2
        assert "Verify the source." in json.dumps(server.requests[-1]["input"])
    assert {call.turn_id for call in calls} == {result.turn.turn_id}
    assert [call.continuation_count for call in calls] == [0, 1]


async def test_provider_failure_does_not_run_completion_callback():
    calls = []
    async with (
        timeout(20),
        websocket_peer(http_failure=(401, "invalid_api_key", "synthetic-auth-rejection")) as server,
    ):
        agent = Agent(
            instructions="Answer.",
            provider=server.provider(websocket=False),
            before_complete=calls.append,
        )
        with pytest.raises(NativeError) as failure:
            await agent.run("Answer.")
        assert failure.value.code == "invalid_api_key"
    assert not calls


async def test_continuation_obeys_existing_request_budget(responses):
    calls = []

    def complete(request):
        calls.append(request.continuation_count)
        return ContinueTurn("The artifact is still missing.")

    responses.respond(assistant_output("Done."))
    responses.respond(assistant_output("Done again."))
    agent = Agent(instructions="Finish.", provider=provider(responses), before_complete=complete)
    async with timeout(15):
        with pytest.raises(NativeError) as failure:
            await agent.run("Prepare the artifact.", budget=RunBudget(max_requests=2))
    assert failure.value.code == "run_budget_exceeded"
    assert calls == [0, 1]
    assert len(responses.requests) == 2


async def test_continuation_obeys_before_step_stop(responses):
    calls = []

    def complete(request):
        calls.append(request)
        return ContinueTurn("Verify the source.")

    def before_step(turn, *, step_index):
        return StopTurn("permission revoked") if step_index == 1 else None

    responses.respond(assistant_output("Unverified."))
    agent = Agent(instructions="Answer.", provider=provider(responses), before_complete=complete)
    async with timeout(15), agent.session() as session:
        turn = await session.submit("Check.", before_step=before_step)
        with pytest.raises(TurnStoppedError, match="Turn stopped by policy"):
            await turn.get_result()
    assert len(responses.requests) == len(calls) == 1


async def test_completion_callback_cannot_cancel_own_session(responses):
    attempted = False

    async def complete(request):
        nonlocal attempted
        if not attempted:
            attempted = True
            await session.cancel_all()

    responses.respond(assistant_output("First."))
    responses.respond(assistant_output("Survivor."))
    agent = Agent(instructions="Answer.", provider=provider(responses), before_complete=complete)
    async with timeout(15), agent.session() as session:
        turn = await session.submit("First.")
        with pytest.raises(RuntimeError, match="callback"):
            await turn.get_result()
        survivor = await session.submit("Next.")
        assert (await survivor.get_result()).final_output == "Survivor."


async def test_policy_stop_rejects_candidate_without_exposing_reason(responses):
    stop = True

    def complete(request):
        return StopTurn("synthetic-private-reason") if stop else None

    responses.respond(assistant_output("Unapproved."))
    responses.respond(assistant_output("Authorized."))
    agent = Agent(instructions="Answer.", provider=provider(responses), before_complete=complete)
    async with timeout(15), agent.session() as session:
        turn = await session.submit("Start.")
        with pytest.raises(TurnStoppedError) as failure:
            await turn.get_result()
        assert "synthetic-private-reason" not in str(failure.value)
        stop = False
        result = await (await session.submit("Start a new turn.")).get_result()
        saved = await session.save_state()
    assert result.final_output == "Authorized."
    assert "synthetic-private-reason" not in saved.to_json()
    assert "synthetic-private-reason" not in json.dumps(responses.requests)
    assert len(responses.requests) == 2


async def test_callback_error_fails_without_retry_and_session_recovers(responses):
    problem = LookupError("synthetic application failure")
    failed = False

    def complete(request):
        nonlocal failed
        if not failed:
            failed = True
            raise problem
        return None

    responses.respond(assistant_output("Not accepted."))
    responses.respond(assistant_output("Recovered."))
    agent = Agent(instructions="Answer.", provider=provider(responses), before_complete=complete)
    async with timeout(15), agent.session() as session:
        with pytest.raises(LookupError) as failure:
            await (await session.submit("Start.")).get_result()
        assert failure.value is problem
        assert len(responses.requests) == 1
        result = await (await session.submit("Try another turn.")).get_result()
    assert result.final_output == "Recovered."


async def test_empty_feedback_fails_before_another_request(responses):
    responses.respond(assistant_output("Not accepted."))
    agent = Agent(
        instructions="Answer.",
        provider=provider(responses),
        before_complete=lambda request: ContinueTurn(" \n"),
    )
    async with timeout(15), agent.session() as session:
        with pytest.raises(ValueError, match="instructions must not be empty"):
            await (await session.submit("Start.")).get_result()
    assert len(responses.requests) == 1


async def test_cancel_joins_sync_callback_and_discards_late_feedback(responses):
    entered, release, exited = Event(), Event(), Event()
    calls = 0

    def complete(request):
        nonlocal calls
        calls += 1
        if calls > 1:
            return None
        entered.set()
        assert release.wait(15)
        exited.set()
        return ContinueTurn("Late feedback must not survive.")

    responses.respond(assistant_output("Candidate."))
    responses.respond(assistant_output("Successor."))
    agent = Agent(instructions="Answer.", provider=provider(responses), before_complete=complete)
    async with timeout(20), agent.session() as session:
        turn = await session.submit("Start.")
        assert await asyncio.to_thread(entered.wait, 10)
        result = asyncio.create_task(turn.get_result())
        stopping = asyncio.create_task(turn.cancel())
        try:
            while not session._active.stopping:
                await asyncio.sleep(0)
            assert not stopping.done()
            assert not result.done()
            assert not exited.is_set()
        finally:
            release.set()
        await stopping
        with pytest.raises(TurnStoppedError):
            await result
        assert exited.is_set()
        successor = await (await session.submit("Start another turn.")).get_result()
        saved = await session.save_state()
    assert successor.final_output == "Successor."
    assert len(responses.requests) == 2
    assert "Late feedback" not in saved.to_json()
    assert "Late feedback" not in json.dumps(responses.requests)


async def test_cancel_joins_async_cleanup_that_suppresses_cancellation(responses):
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0

    async def complete(request):
        nonlocal calls
        calls += 1
        if calls > 1:
            return None
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleaning.set()
            await release.wait()
            return ContinueTurn("Late async feedback.")

    responses.respond(assistant_output("Candidate."))
    responses.respond(assistant_output("Successor."))
    agent = Agent(instructions="Answer.", provider=provider(responses), before_complete=complete)
    async with timeout(20), agent.session() as session:
        turn = await session.submit("Start.")
        await entered.wait()
        stopping = asyncio.create_task(turn.cancel())
        try:
            await cleaning.wait()
            assert not stopping.done()
        finally:
            release.set()
        await stopping
        with pytest.raises(TurnStoppedError):
            await turn.get_result()
        successor = await (await session.submit("Start another turn.")).get_result()
        saved = await session.save_state()
    assert successor.final_output == "Successor."
    assert "Late async feedback" not in saved.to_json()
    assert len(responses.requests) == 2


async def test_completion_rejection_runs_automatic_compaction_before_next_request(responses):
    order = []

    async def high_usage(event):
        if event["type"] == "response.completed" and len(responses.requests) == 1:
            event["response"]["usage"] = {
                "input_tokens": 260_000,
                "output_tokens": 1_000,
                "total_tokens": 261_000,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            }

    def complete(request):
        order.append("complete")
        return (
            ContinueTurn("Verify source-17 before finishing.")
            if not request.continuation_count
            else None
        )

    def compact(request):
        order.append("compact")
        assert request.trigger == "auto"
        assert "Verify source-17" in json.dumps(request.items)
        return "Pending: verify source-17 before finishing."

    responses.before_event = high_usage
    responses.respond(assistant_output("Unchecked."))
    responses.respond(assistant_output("Verified."))
    agent = Agent(
        instructions="Answer with evidence.",
        model="gpt-5.5",
        provider=provider(responses),
        before_complete=complete,
        before_compact=compact,
    )
    async with timeout(20):
        result = await agent.run("Verify the source.")
    assert result.final_output == "Verified."
    assert order == ["complete", "compact", "complete"]
    assert len(responses.requests) == 2
    assert "Pending: verify source-17" in json.dumps(responses.requests[1]["input"])


async def test_output_repair_reenters_original_completion_gate(responses):
    calls = []

    def complete(request):
        calls.append(request)

    responses.respond(assistant_output("not an integer"))
    responses.respond(assistant_output("42"))
    agent = Agent(
        instructions="Return an integer.",
        provider=provider(responses),
        output_type=PromptedOutput(int, max_retries=1),
        before_complete=complete,
    )
    async with timeout(20):
        result = await agent.run("Answer.")
    assert result.final_output == 42
    assert [call.text for call in calls] == ["not an integer", "42"]
    assert {call.turn_id for call in calls} == {result.turn.turn_id}
    assert {call.continuation_count for call in calls} == {0}
    assert len(responses.requests) == 2


class CompletionChildPeer(ChildPeer):
    def response(self):
        identity = metadata(self.requests[-1], self.request_headers[-1])
        if identity["agent_name"] == "/root":
            return super().response()
        name = identity["agent_name"].rsplit("/", 1)[-1]
        requests = self.child_requests.setdefault(name, [])
        requests.append(self.requests[-1])
        return [assistant_output("Child candidate." if len(requests) == 1 else "Child verified.")]


@pytest.mark.parametrize("stop_child", [False, True])
async def test_native_child_completion_is_gated_after_root_finishes(
    tmp_path, monkeypatch, stop_child
):
    calls = []
    entered, release = asyncio.Event(), asyncio.Event()

    async def complete(request):
        calls.append(request)
        if request.subagent_id is None:
            return None
        if request.continuation_count == 0:
            entered.set()
            await release.wait()
            if stop_child:
                return StopTurn("private-child-policy")
            return ContinueTurn("Verify the child deliverable.")
        return None

    peer = CompletionChildPeer(("default",))
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    async with (
        timeout(30),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        peer.url = str(server.make_url("/v1"))
        agent = replace(agent_for(peer, executor), before_complete=complete)
        async with agent.session(environment=declaration(executor)) as session:
            root = await session.submit("Delegate the question.")
            await entered.wait()
            assert (await root.get_result()).final_output == "root complete"
            (child,) = await handles(session, 1)
            pending = asyncio.create_task(child.get_result())
            await asyncio.sleep(0)
            try:
                assert not pending.done()
            finally:
                release.set()
            if stop_child:
                with pytest.raises(NativeError, match="^Turn stopped by policy$"):
                    await pending
            else:
                assert (await pending).final_output == "Child verified."
            child_calls = [call for call in calls if call.subagent_id is not None]
            assert all(call.session_id == session.id for call in child_calls)
            assert {call.turn_id for call in child_calls} == {child.turn_id}
            assert {call.subagent_id for call in child_calls} == {child.child_id}
    assert [call.continuation_count for call in child_calls] == ([0] if stop_child else [0, 1])
    assert "private-child-policy" not in json.dumps(peer.requests)
    if not stop_child:
        assert "Verify the child deliverable." in json.dumps(peer.child_requests["alpha"][-1])
