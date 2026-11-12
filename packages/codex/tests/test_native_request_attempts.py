"""Public attempt snapshots have been counting native sends through settlement."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import asdict

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from openai import AsyncOpenAI

from agents import (
    Agent,
    ContinueTurn,
    FunctionTool,
    NativeError,
    NativeProvider,
    PromptedOutput,
    ResponsesModel,
    SessionState,
    TurnStoppedError,
)
from agents.observation import OutputAccepted
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call, responses as responses
from tests.test_native_agent import provider as fixture_provider
from tests.test_native_provider_websockets import peer as websocket_peer


@pytest.mark.parametrize("capture", [False, True])
@pytest.mark.parametrize("failure", ["validation", "rejected", "uncertain"])
async def test_issued_admission_failure_retains_opted_in_logical_report(
    responses, monkeypatch, capture, failure
):
    import _agents_codex.session as session_module

    async with timeout(30), fixture_provider(responses) as provider:
        async with Agent(instructions="Answer.", provider=provider).session(
            include_request_attempts=capture
        ) as session:
            native = session._native

            class Admission:
                def __getattr__(self, name):
                    return getattr(native, name)

                async def _submit(self, encoded, **kwargs):
                    if failure == "rejected":
                        return json.dumps(
                            {"status": "not_submitted", "reason": {"type": "no_active_turn"}}
                        )
                    raise NativeError("Synthetic uncertain admission")

            if failure == "validation":

                def reject(request):
                    raise ValueError("Synthetic pre-admission validation")

                monkeypatch.setattr(session_module, "_check_audio_json", reject)
            else:
                monkeypatch.setattr(session, "_native", Admission())
            turn = await session.submit("Answer.")
            with pytest.raises(ValueError if failure == "validation" else NativeError):
                await turn.get_result()
            assert turn.turn_id is None
            assert responses.requests == []
            assert turn.request_attempts is None
            report = turn.submission_request_attempts
            if capture:
                assert report is not None
                assert report.counts == ()
                assert report.availability == ("partial" if failure == "uncertain" else "complete")
                assert not report.borrowed_requests_unavailable
            else:
                assert report is None


@asynccontextmanager
async def attempt_peer(*, mode="streaming", failures=0, hold=None, borrowed=False):
    requests = []
    entered = asyncio.Event()

    async def respond(request):
        requests.append(await request.json())
        entered.set()
        if hold is not None:
            await hold.wait()
        if len(requests) <= failures:
            return web.json_response(
                {"error": {"message": "synthetic failure", "type": "server_error"}},
                status=503,
                headers={"retry-after": "0"},
            )
        item = assistant_output("synthetic-private-answer")["item"]
        response = {
            "id": "resp_attempts",
            "object": "response",
            "status": "completed",
            "output": [item],
        }
        if mode == "buffered" or borrowed:
            return web.json_response(response)
        frames = [
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": response},
        ]
        return web.Response(
            text="".join(f"data: {json.dumps(frame)}\n\n" for frame in frames),
            content_type="text/event-stream",
        )

    app = web.Application()
    app.router.add_post("/v1/responses", respond)
    async with TestServer(app) as server:
        url = str(server.make_url("/v1"))
        async with AsyncOpenAI(api_key="synthetic", base_url=url, max_retries=0) as client:
            async with NativeProvider(
                base_url=url,
                api_key="synthetic",
                supports_websockets=False,
                response_mode=mode,
                request_max_retries=1,
                stream_max_retries=0,
                model_backend=ResponsesModel(client) if borrowed else None,
            ) as provider:
                yield provider, requests, entered


@pytest.mark.parametrize("capture", [False, True])
async def test_withdrawn_queued_turn_retains_zero_logical_sends(responses, capture):
    entered, release = asyncio.Event(), asyncio.Event()
    responses.respond(assistant_output("First answer."))

    async def hold(event):
        entered.set()
        await release.wait()

    responses.before_event = hold
    async with timeout(30), fixture_provider(responses) as provider:
        async with Agent(instructions="Answer.", provider=provider).session(
            include_request_attempts=capture
        ) as session:
            first = await session.submit("First.")
            try:
                await entered.wait()
                queued = await session.submit("Withdraw.", if_busy="queue")
                await queued.cancel()
                with pytest.raises(TurnStoppedError):
                    await queued.get_result()
                assert queued.turn_id is None and queued.request_attempts is None
                report = queued.submission_request_attempts
                if capture:
                    assert report is not None
                    assert report.availability == "complete" and report.counts == ()
                else:
                    assert report is None
            finally:
                release.set()
            assert (await first.get_result()).final_output == "First answer."
            assert len(responses.requests) == 1
            if capture:
                assert counts(first, submission=True) == {("root", "generation", "http_sse"): 1}


def counts(turn, *, submission=False):
    snapshot = turn.submission_request_attempts if submission else turn.request_attempts
    assert snapshot is not None
    return {(row.owner, row.operation, row.transport): row.count for row in snapshot.counts}


@pytest.mark.parametrize("mode,transport", [("streaming", "http_sse"), ("buffered", "http_json")])
async def test_public_native_retry_counts_physical_sends_and_survives_close(mode, transport):
    async with timeout(30), attempt_peer(mode=mode, failures=1) as (provider, received, _):
        agent = Agent(model="fixture-attempts", instructions="Answer briefly.", provider=provider)
        async with agent.session(include_request_attempts=True) as session:
            turn = await session.submit("synthetic-private-prompt")
            assert (await turn.get_result()).final_output == "synthetic-private-answer"
            saved = SessionState.from_json((await session.save_state()).to_json())
        assert len(received) == 2
        assert counts(turn) == {("root", "generation", transport): 2}
        assert turn.request_attempts.availability == "complete"
        assert turn.request_attempts.borrowed_requests_unavailable is False
        assert "synthetic-private" not in json.dumps(asdict(turn.request_attempts))
        assert "request_attempts" not in saved.to_json()
        async with agent.session(state=saved, include_request_attempts=True) as restored:
            assert restored.active_turn is None
            fresh = await restored.submit("A new native step.")
            await fresh.get_result()
            assert counts(fresh) == {("root", "generation", transport): 1}
        assert counts(turn) == {("root", "generation", transport): 2}


async def test_exhausted_native_request_still_has_settled_counts():
    async with timeout(30), attempt_peer(failures=2) as (provider, received, _):
        async with Agent(
            model="fixture-attempts", instructions="Answer.", provider=provider
        ).session(include_request_attempts=True) as session:
            turn = await session.submit("Fail.")
            with pytest.raises(NativeError):
                await turn.get_result()
            assert len(received) == 2
            assert counts(turn) == {("root", "generation", "http_sse"): 2}
            assert turn.request_attempts.availability == "complete"
            assert counts(turn, submission=True) == counts(turn)


async def test_cancelled_native_request_keeps_counts_without_a_completed_response():
    release = asyncio.Event()
    async with timeout(30), attempt_peer(hold=release) as (provider, received, entered):
        try:
            async with Agent(
                model="fixture-attempts", instructions="Answer.", provider=provider
            ).session(include_request_attempts=True) as session:
                turn = await session.submit("Hold.")
                await entered.wait()
                assert turn.request_attempts is None
                await turn.cancel()
                with pytest.raises(TurnStoppedError):
                    await turn.get_result()
                assert len(received) == 1
                assert counts(turn) == {("root", "generation", "http_sse"): 1}
                assert counts(turn, submission=True) == counts(turn)
        finally:
            release.set()


async def test_default_and_borrowed_request_availability_are_distinct():
    async with timeout(30), attempt_peer(borrowed=True) as (provider, received, _):
        agent = Agent(model="fixture-attempts", instructions="Answer.", provider=provider)
        async with agent.session() as ordinary:
            default = await ordinary.submit("Default.")
            await default.get_result()
            assert default.request_attempts is None
            assert default.submission_request_attempts is None
        async with agent.session(include_request_attempts=True) as observed:
            turn = await observed.submit("Opted in.")
            await turn.get_result()
            assert counts(turn) == {}
            assert turn.request_attempts.availability == "unavailable"
            assert turn.request_attempts.borrowed_requests_unavailable is True
            assert turn.submission_request_attempts == turn.request_attempts
        assert len(received) == 2


async def test_websocket_turn_count_excludes_session_startup_warmup():
    async with timeout(30), websocket_peer() as peer:
        peer.respond(assistant_output("Done."))
        async with Agent(
            model="fixture-attempts", instructions="Answer.", provider=peer.provider()
        ).session(include_request_attempts=True) as session:
            await peer.warmup_received.wait()
            assert peer.warmups == 1
            assert not peer.requests
            turn = await session.submit("Go.")
            await turn.get_result()
            assert len(peer.requests) == peer.warmups == 1
            assert counts(turn) == {("root", "generation", "websocket"): 1}


async def test_capture_option_rejects_non_boolean_before_provider_io():
    async with timeout(10), attempt_peer() as (provider, received, _):
        with pytest.raises(TypeError, match="include_request_attempts"):
            async with Agent(instructions="Answer.", provider=provider).session(
                include_request_attempts=1
            ):
                pytest.fail("Non-boolean capture must fail before session opening")
        assert not received


async def test_restored_pending_turn_has_no_new_capture(responses):
    from tests.test_native_manual_tools import LookupArgs, next_request

    lookup = FunctionTool(
        name="lookup", description="Read a synthetic key.", input_type=LookupArgs, output_type=str
    )
    responses.respond(function_call("lookup", {"key": "A"}, "call-restored"))
    async with timeout(30), fixture_provider(responses) as provider:
        agent = Agent(
            model="fixture-attempts", instructions="Read.", tools=[lookup], provider=provider
        )
        async with agent.session(include_request_attempts=True) as source:
            turn = await source.submit("Read.")
            await next_request(source.interactions(), lookup)
            assert turn.request_attempts is None
            state = await source.suspend()
            assert counts(turn) == {("root", "generation", "http_sse"): 1}
        async with agent.session(
            state=state, include_request_attempts=True, include_output_accepted=True
        ) as restored:
            request = await next_request(restored.interactions(), lookup)
            restored_turn = restored.restored_turn
            assert restored_turn is not None
            assert restored_turn.request_attempts is None
            responses.respond(assistant_output("Restored."))
            await request.respond("A")
            accepted = [
                event async for event in restored_turn.events() if isinstance(event, OutputAccepted)
            ]
            assert len(accepted) == 1
            assert accepted[0].submission_id == restored_turn.submission_id
            assert accepted[0].turn_id == restored_turn.turn_id
            assert (await restored_turn.get_result()).final_output == "Restored."
            assert restored_turn.request_attempts is None
            assert restored_turn.submission_request_attempts is None
        assert len(responses.requests) == 2


@pytest.mark.parametrize(
    "private_repair", [False, True], ids=["native-continuation", "private-repair"]
)
async def test_snapshot_scope_keeps_private_repairs_separate(responses, private_repair):
    responses.respond(assistant_output("invalid"))
    responses.respond(assistant_output("7"))
    async with timeout(30), fixture_provider(responses) as provider:
        agent = Agent(
            model="fixture-attempts",
            instructions="Return a number.",
            provider=provider,
            before_complete=None
            if private_repair
            else lambda request: ContinueTurn("Revise.") if request.text == "invalid" else None,
        )
        async with agent.session(include_request_attempts=True) as session:
            turn = (
                await session.submit("Answer.", output_type=PromptedOutput(int, max_retries=1))
                if private_repair
                else await session.submit("Answer.")
            )
            await turn.get_result()
            assert len(responses.requests) == 2
            assert counts(turn) == {("root", "generation", "http_sse"): 1 if private_repair else 2}
            assert counts(turn, submission=True) == {("root", "generation", "http_sse"): 2}
            assert turn.submission_request_attempts.availability == "complete"


async def test_post_turn_compaction_has_its_own_operation_count(responses, monkeypatch):
    from agents import _native

    create_engine = _native.create_engine

    async def configured(config_json, *args, **kwargs):
        config = json.loads(config_json)
        config.update(
            model_context_window=200_000,
            model_auto_compact_token_limit=10_000,
            model_post_turn_compact_threshold_percent=1,
        )
        return await create_engine(json.dumps(config), *args, **kwargs)

    monkeypatch.setattr(_native, "create_engine", configured)

    async def high_usage(event):
        if event["type"] == "response.completed" and len(responses.requests) == 1:
            event["response"]["usage"].update(
                input_tokens=12_000, output_tokens=1, total_tokens=12_001
            )

    responses.before_event = high_usage
    responses.respond(assistant_output("Answer."))
    responses.respond(assistant_output("Synthetic compaction summary."))
    async with timeout(30), fixture_provider(responses) as provider:
        async with Agent(
            model="fixture-attempts", instructions="Answer.", provider=provider
        ).session(include_request_attempts=True) as session:
            turn = await session.submit("Go.")
            assert (await turn.get_result()).final_output == "Answer."
            assert len(responses.requests) == 2
            assert counts(turn) == {
                ("root", "generation", "http_sse"): 1,
                ("root", "compaction", "http_sse"): 1,
            }


async def test_root_capture_includes_native_child_sends_but_child_has_no_capture(
    tmp_path, monkeypatch
):
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import agent_for, child_peer, handles, trust
    from tests.test_native_executor_tools import declaration

    async with (
        timeout(40),
        child_peer(("default", "research")) as peer,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        hold = peer.hold

        async def observe(event):
            if (
                event.get("item", {}).get("role") == "assistant"
                and event["item"].get("content", [{}])[0].get("text") == "root complete"
            ):
                await asyncio.gather(*(entered.wait() for entered in peer.entered.values()))
                for release in peer.release.values():
                    release.set()
            await hold(event)

        peer.before_event = observe
        async with agent_for(peer, executor).session(
            environment=declaration(executor), include_request_attempts=True
        ) as session:
            turn = await session.submit("Use the native children.")
            await turn.get_result()
            children = await handles(session, 2)
            await asyncio.gather(*(child.get_result() for child in children))
            report = counts(turn)
            assert report[("child", "generation", "http_sse")] == sum(
                len(requests) for requests in peer.child_requests.values()
            )
            assert report[("root", "generation", "http_sse")] + report[
                ("child", "generation", "http_sse")
            ] == len(peer.requests)
            assert all(child.request_attempts is None for child in children)


async def test_concurrent_logical_reports_keep_private_repairs_with_their_owner(responses):
    responses.respond(assistant_output("invalid"))
    responses.respond(assistant_output("invalid"))
    responses.respond(assistant_output("7"))
    responses.respond(assistant_output("7"))
    entered = asyncio.Event()

    async def overlap(event):
        if event["type"] == "response.created" and len(responses.requests) <= 2:
            if len(responses.requests) == 2:
                entered.set()
            await entered.wait()

    responses.before_event = overlap
    async with timeout(30), fixture_provider(responses) as provider:
        agent = Agent(model="fixture", instructions="Return a number.", provider=provider)
        async with (
            agent.session(include_request_attempts=True) as left,
            agent.session(include_request_attempts=True) as right,
        ):
            turns = await asyncio.gather(
                left.submit("Left.", output_type=PromptedOutput(int, max_retries=1)),
                right.submit("Right.", output_type=PromptedOutput(int, max_retries=1)),
            )
            await asyncio.gather(*(turn.get_result() for turn in turns))
            assert len(responses.requests) == 4
            for turn in turns:
                assert counts(turn) == {("root", "generation", "http_sse"): 1}
                assert counts(turn, submission=True) == {("root", "generation", "http_sse"): 2}


async def test_cancelled_private_repair_keeps_its_logical_send(responses):
    responses.respond(assistant_output("invalid"))
    responses.respond(assistant_output("7"))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_repair(event):
        if len(responses.requests) == 2:
            entered.set()
            await release.wait()

    responses.before_event = hold_repair
    try:
        async with timeout(30), fixture_provider(responses) as provider:
            async with Agent(
                model="fixture", instructions="Return a number.", provider=provider
            ).session(include_request_attempts=True) as session:
                turn = await session.submit(
                    "Answer.", output_type=PromptedOutput(int, max_retries=1)
                )
                await entered.wait()
                assert turn.submission_request_attempts is None
                await turn.cancel()
                with pytest.raises(TurnStoppedError):
                    await turn.get_result()
                assert counts(turn) == {("root", "generation", "http_sse"): 1}
                assert counts(turn, submission=True) == {("root", "generation", "http_sse"): 2}
    finally:
        release.set()


async def test_failed_private_repair_keeps_its_logical_send(responses):
    from agents import OutputValidationError

    responses.respond(assistant_output("invalid"))
    responses.respond(assistant_output("still invalid"))
    async with timeout(30), fixture_provider(responses) as provider:
        async with Agent(
            model="fixture", instructions="Return a number.", provider=provider
        ).session(include_request_attempts=True) as session:
            turn = await session.submit("Answer.", output_type=PromptedOutput(int, max_retries=1))
            with pytest.raises(OutputValidationError):
                await turn.get_result()
            assert counts(turn) == {("root", "generation", "http_sse"): 1}
            assert counts(turn, submission=True) == {("root", "generation", "http_sse"): 2}
