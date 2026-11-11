"""Named guardrails on real native acceptance boundaries."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from pydantic import BaseModel, model_validator

from agents import (
    Agent,
    GuardrailResult,
    GuardrailTripwire,
    InputGuardrail,
    NativeOutput,
    OutputGuardrail,
    ToolContext,
)
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses
from tests.test_native_agent import provider


@pytest.mark.parametrize("profiled", [False, True])
async def test_converted_output_guards_share_one_parse_and_source_owner(responses, profiled):
    parsed = []
    observed = []
    raw = []
    input_seen = []

    class Answer(BaseModel):
        count: int

        @model_validator(mode="after")
        def counted(self):
            parsed.append(self.count)
            return self

    def check(request, agent):
        observed.append((request, agent))
        assert request.final_output.count == 7
        request.final_output.count = 99
        return GuardrailResult(False, {"checked": 7})

    def check_input(turn, context, source, value):
        assert source is agent
        input_seen.append(value)
        return GuardrailResult(False)

    responses.respond(assistant_output('{"count":7}'))
    agent = Agent(
        instructions="Answer.",
        provider=provider(responses),
        output_type=NativeOutput(Answer),
        handoffs=[Agent(name="Unused", instructions="Unused", output_type=NativeOutput(Answer))]
        if profiled
        else [],
        before_complete=lambda request: raw.append(request.text),
        output_guardrails=[OutputGuardrail("agent-check", check)],
        input_guardrails=[InputGuardrail("agent-input", check_input, run_in_parallel=False)],
    )
    async with (
        timeout(20),
        agent.session(output_guardrails=[OutputGuardrail("run-check", check)]) as session,
    ):
        turn = await session.submit("begin")
        result = await turn.get_result()
        assert result.final_output.count == 7
        assert parsed == [7]
        assert sorted(r.name for r in result.guardrail_results) == [
            "agent-check",
            "agent-input",
            "run-check",
        ]
        assert input_seen == ["begin"]
        assert turn.guardrail_results == result.guardrail_results
        assert all(record.agent is agent for record in result.guardrail_results)
        assert all(
            r.turn_id == turn.turn_id and r.session_id == session.id
            for r in result.guardrail_results
        )
    assert raw == ['{"count":7}']
    assert len(observed) == 2
    assert all(source is agent for _, source in observed)
    assert replace(agent, name="copy").output_guardrails == agent.output_guardrails


async def test_tripwire_keeps_completed_siblings_and_arbitrary_host_diagnostics(responses):
    completed = asyncio.Event()
    marker = object()
    joined = asyncio.Event()

    async def passing(request, agent):
        completed.set()
        return GuardrailResult(False, marker)

    async def tripping(request, agent):
        await completed.wait()
        await asyncio.sleep(0)
        return GuardrailResult(True, "local diagnostic")

    async def pending(request, agent):
        try:
            await asyncio.Future()
        finally:
            joined.set()

    responses.respond(assistant_output("candidate"))
    agent = Agent(
        instructions="Answer.",
        provider=provider(responses),
        output_guardrails=[
            OutputGuardrail("passed", passing),
            OutputGuardrail("tripped", tripping),
            OutputGuardrail("pending", pending),
        ],
    )
    async with timeout(20), agent.session() as session:
        turn = await session.submit("begin")
        with pytest.raises(GuardrailTripwire) as caught:
            await turn.get_result()
        assert joined.is_set()
        assert caught.value.record.name == "tripped"
        assert [r.name for r in caught.value.results] == ["passed", "tripped"]
        assert caught.value.results[0].result.output_info is marker
        assert turn.guardrail_results == caught.value.results
        assert "local diagnostic" not in str(caught.value)
        assert "local diagnostic" not in json.dumps([item async for item in session.items()])


async def test_blocked_terminal_output_placeholder_precedes_history_and_stream_publication(
    responses,
):
    from agents import function_tool
    from tests.native_fixtures import function_call

    @function_tool(finish_turn=True)
    def done() -> str:
        return "sensitive-terminal-output"

    def block(request, agent):
        assert request.final_output == "sensitive-terminal-output"
        assert request.tool_call.call_id == "terminal-call"
        return GuardrailResult(True)

    responses.respond(function_call("done", {}, "terminal-call"))
    agent = Agent(
        instructions="Use done.",
        tools=[done],
        provider=provider(responses),
        output_guardrails=[OutputGuardrail("block", block)],
    )
    async with timeout(20), agent.session() as session:
        turn = await session.submit("begin")
        with pytest.raises(GuardrailTripwire):
            await turn.get_result()
        public_history = [item async for item in session.items()]
        public_events = [event async for event in turn.events(include_execution_details=True)]
        saved = await session.save_state()
        assert "sensitive-terminal-output" not in json.dumps(public_history)
        assert "sensitive-terminal-output" not in repr(public_events)
        assert "sensitive-terminal-output" not in json.dumps(saved.history)
        assert "withheld" in json.dumps(public_history).lower()


async def test_python_diagnostics_follow_reversed_completion_order(responses):
    second_finished = asyncio.Event()

    async def first(request, agent):
        await second_finished.wait()
        await asyncio.sleep(0)
        return GuardrailResult(False)

    async def second(request, agent):
        second_finished.set()
        return GuardrailResult(False)

    responses.respond(assistant_output("accepted"))
    agent = Agent(
        instructions="Answer.",
        provider=provider(responses),
        output_guardrails=[OutputGuardrail("first", first), OutputGuardrail("second", second)],
    )
    async with timeout(20), agent.session() as session:
        result = await (await session.submit("begin")).get_result()
        assert [record.name for record in result.guardrail_results] == ["second", "first"]


async def test_blocking_input_siblings_overlap_before_model_and_keep_original_input(responses):
    from agents import InputGuardrail

    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    seen = []

    async def first(turn, context, agent, original):
        first_entered.set()
        await second_entered.wait()
        assert not responses.requests
        seen.append((turn.turn_id, agent.name, original))
        return GuardrailResult(False)

    async def second(turn, context, agent, original):
        second_entered.set()
        await first_entered.wait()
        assert not responses.requests
        return GuardrailResult(False)

    responses.respond(assistant_output("accepted"))
    agent = Agent(
        instructions="Answer.",
        provider=provider(responses),
        input_guardrails=[InputGuardrail("first", first, run_in_parallel=False)],
    )
    async with (
        timeout(20),
        agent.session(
            input_guardrails=[InputGuardrail("second", second, run_in_parallel=False)]
        ) as session,
    ):
        turn = await session.submit("original")
        result = await turn.get_result()
        assert seen == [(turn.turn_id, agent.name, "original")]
        assert {record.name for record in result.guardrail_results} == {"first", "second"}
        assert all(record.kind == "input" for record in result.guardrail_results)
        assert len(responses.requests) == 1


async def test_blocking_input_trip_prevents_model_request(responses):
    from agents import InputGuardrail

    agent = Agent(
        instructions="Answer.",
        provider=provider(responses),
        input_guardrails=[
            InputGuardrail("blocked", lambda *args: GuardrailResult(True), run_in_parallel=False)
        ],
    )
    async with timeout(20), agent.session() as session:
        turn = await session.submit("original")
        with pytest.raises(GuardrailTripwire) as caught:
            await turn.get_result()
        assert caught.value.record.kind == "input"
        assert turn.guardrail_results == caught.value.results
        assert not responses.requests


async def test_parallel_input_overlaps_model_and_gates_completion(responses):
    from agents import InputGuardrail

    overlapping = asyncio.Event()
    release = asyncio.Event()

    async def check(turn, context, agent, original):
        while not responses.requests:
            await asyncio.sleep(0)
        overlapping.set()
        await release.wait()
        return GuardrailResult(False)

    responses.respond(assistant_output("accepted"))
    agent = Agent(
        instructions="Answer.",
        provider=provider(responses),
        input_guardrails=[InputGuardrail("overlap", check)],
    )
    async with timeout(20), agent.session() as session:
        turn = await session.submit("original")
        result = asyncio.create_task(turn.get_result())
        await overlapping.wait()
        assert not result.done()
        release.set()
        assert [record.name for record in (await result).guardrail_results] == ["overlap"]


@pytest.mark.parametrize("parallel", [False, True])
async def test_explicit_child_input_guards_block_or_overlap_then_trip(
    tmp_path, monkeypatch, parallel
):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from agents import ChildStepContext, InputGuardrail, NativeAgents, Turn
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import ChildPeer, handles, metadata, trust
    from tests.test_native_executor_tools import declaration

    class Peer(ChildPeer):
        async def hold(self, event):
            pass

        def response(self):
            body = self.requests[-1]
            path = metadata(body, self.request_headers[-1])["agent_name"]
            if path == "/root":
                return super().response()
            self.child_requests.setdefault(path, []).append(body)
            return [assistant_output("child final before input verdict")]

    entered = asyncio.Event()
    release = asyncio.Event()
    children_seen = set()
    roots = []

    def root_check(turn, context, agent, value):
        assert isinstance(turn, Turn)
        assert agent is original_agent
        roots.append(value)
        return GuardrailResult(False)

    async def child_check(context, agent, value):
        assert isinstance(context, ChildStepContext)
        assert agent is original_agent
        assert context.step_index == 0
        encoded = json.dumps(value)
        assert "Execute the requested native work." in encoded
        assert "root-only-sentinel" not in encoded
        children_seen.add(context.child.child_id)
        if len(children_seen) == 2:
            entered.set()
        await release.wait()
        return GuardrailResult(True, {"child": context.child.child_id})

    peer = Peer(("default", "default"))
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    async with (
        timeout(30),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        guard = InputGuardrail(
            "initial-input", root_check, run_in_parallel=parallel, child_callback=child_check
        )
        original_agent = Agent(
            instructions="Spawn alpha and beta.",
            model="gpt-5.5",
            tools=[NativeAgents(max_children=2)],
            provider=peer.provider(websocket=False),
            input_guardrails=[guard],
        )
        async with original_agent.session(environment=declaration(executor)) as session:
            try:
                root = await session.submit("root-only-sentinel")
                await entered.wait()
                assert (await root.get_result()).final_output == "root complete"
                await handles(session, 2)
                children = [child async for child in session.subagents()]
                if parallel:
                    while len(peer.child_requests) != 2:
                        await asyncio.sleep(0)
                else:
                    assert peer.child_requests == {}
                release.set()
                for child in children:
                    assert child.turn is not None
                    with pytest.raises(GuardrailTripwire) as error:
                        await child.turn.get_result()
                    assert error.value.record.agent is original_agent
                    assert error.value.record.subagent_id == child.id
                    assert error.value.record.turn_id == child.turn.turn_id
                assert roots == ["root-only-sentinel"]
            finally:
                release.set()


@pytest.mark.parametrize(
    ("reject", "marker"),
    [
        (False, "keep"),
        (True, "keep"),
        (False, "erase"),
        (True, "erase"),
        (False, "duplicate"),
        (False, "duplicate-caller-cancel"),
        (False, "prior"),
        (False, "manual"),
    ],
)
async def test_held_terminal_resume_checks_before_ordinary_admission(
    responses, monkeypatch, reject, marker
):
    from agents import FunctionTool, function_tool
    from agents.state import SessionState
    from tests.native_fixtures import function_call
    from tests.test_native_manual_profiles import save_at_manual_boundary
    from tests.test_native_manual_tools import LookupArgs, next_request

    effects = []
    checked = []

    @function_tool
    def prior() -> str:
        return "already-accepted-sibling"

    @function_tool(finish_turn=True)
    def finish() -> str:
        effects.append("executed")
        return "unadmitted-terminal-sentinel"

    manual = FunctionTool(
        name="lookup", description="Read.", input_type=LookupArgs, output_type=str
    )
    responses.respond(function_call("prior", {}, "prior-call"))
    responses.respond(
        function_call("finish", {}, "held-call"),
        function_call("lookup", {"key": "pending"}, "manual-call"),
    )
    agent = Agent(
        instructions="Use the declared tools.",
        tools=[prior, finish, manual],
        provider=provider(responses),
    )
    async with timeout(30):
        async with agent.session() as original:
            turn = await original.submit("begin")
            await next_request(original.interactions(), manual)
            saved = await save_at_manual_boundary(original)
            assert effects == ["executed"]
            assert "unadmitted-terminal-sentinel" not in json.dumps(saved.history)
            before = [event async for event in turn.events(include_execution_details=True)]
            assert "unadmitted-terminal-sentinel" not in repr(before)
        raw = json.loads(saved.to_json())
        outcomes = raw["native_continuation"]["tool_outcomes"]
        assert outcomes["held_call_ids"] == ["held-call"]
        if marker == "erase":
            outcomes.pop("held_call_ids")
        elif marker not in ("keep", "erase"):
            outcomes["held_call_ids"] = {
                "duplicate": ["held-call", "held-call"],
                "duplicate-caller-cancel": ["held-call", "held-call"],
                "prior": ["prior-call"],
                "manual": ["manual-call"],
            }[marker]
            failed, release = asyncio.Event(), asyncio.Event()
            if marker == "duplicate-caller-cancel":
                from agents import _native

                original_continue = _native.NativeThread._continue_manual

                async def parked_native_failure(native, *args, **kwargs):
                    try:
                        return await original_continue(native, *args, **kwargs)
                    except RuntimeError:
                        failed.set()
                        await release.wait()
                        raise

                monkeypatch.setattr(_native.NativeThread, "_continue_manual", parked_native_failure)
            with pytest.raises(
                (ValueError, RuntimeError), match="Invalid held terminal reply identities"
            ):
                async with agent.session(state=SessionState.from_json(json.dumps(raw))) as restored:
                    request = await next_request(restored.interactions(), manual)
                    if marker == "duplicate-caller-cancel":
                        responding = asyncio.create_task(request.respond("manual accepted"))
                        try:
                            await failed.wait()
                            responding.cancel("synthetic caller cancellation")
                            release.set()
                            with pytest.raises(asyncio.CancelledError):
                                await responding
                            assert responding.cancelled()
                        finally:
                            release.set()
                            await asyncio.gather(responding, return_exceptions=True)
                    else:
                        await request.respond("manual accepted")
                    assert restored.restored_turn is not None
                    await restored.restored_turn.get_result()
            assert effects == ["executed"]
            assert len(responses.requests) == 2
            return

        def check(request, source):
            if request.tool_call is not None:
                checked.append(request.tool_call.call_id)
                assert request.final_output == "unadmitted-terminal-sentinel"
                return GuardrailResult(reject)
            return GuardrailResult(False)

        restored_agent = replace(agent, output_guardrails=(OutputGuardrail("restored", check),))
        if not reject:
            responses.respond(assistant_output("ordinary-after-restore"))
        async with restored_agent.session(
            state=SessionState.from_json(json.dumps(raw))
        ) as restored:
            request = await next_request(restored.interactions(), manual)
            before_admission = [item async for item in restored.items()]
            assert "unadmitted-terminal-sentinel" not in json.dumps(before_admission)
            await request.respond("manual accepted")
            assert restored.restored_turn is not None
            if reject:
                with pytest.raises(GuardrailTripwire):
                    await restored.restored_turn.get_result()
            else:
                result = await restored.restored_turn.get_result()
                assert result.final_output == "ordinary-after-restore"
            rows = [item async for item in restored.items()]
            encoded = json.dumps(rows)
            assert "already-accepted-sibling" in encoded
            assert ("unadmitted-terminal-sentinel" in encoded) is not reject
            admitted = [
                row
                for row in rows
                if row.get("type") == "function_call_output" and row.get("call_id") == "held-call"
            ]
            assert len(admitted) == 1
            if reject:
                assert "withheld" in json.dumps(admitted).lower()
            events = [
                event
                async for event in restored.restored_turn.events(include_execution_details=True)
            ]
            if reject:
                assert "unadmitted-terminal-sentinel" not in repr(events)
            assert checked == ["held-call"]
            assert effects == ["executed"]
            assert len(responses.requests) == (2 if reject else 3)


@pytest.mark.parametrize("mode", ["approve", "reject", "cancel", "error", "siblings", "duplicate"])
async def test_terminal_observations_wait_for_committed_admission(responses, monkeypatch, mode):
    from agents import NativeError, TurnStoppedError, _native, function_tool
    from agents.observation import ToolFinished
    from tests.native_fixtures import function_call

    if mode == "duplicate":
        original_next = _native.NativeThread._next_session_event
        pending = {}
        duplicated = set()

        async def duplicate_publication(native):
            key = id(native)
            if key in pending:
                return pending.pop(key)
            raw = await original_next(native)
            if raw is None:
                return raw
            event = json.loads(raw)
            item = (event or {}).get("msg", {}).get("item", {})
            if (
                isinstance(item, dict)
                and item.get("type") == "function_call_output"
                and item.get("call_id") == "terminal-call"
                and key not in duplicated
            ):
                pending[key] = raw
                duplicated.add(key)
            return raw

        monkeypatch.setattr(_native.NativeThread, "_next_session_event", duplicate_publication)

    @function_tool
    def prior() -> str:
        return "accepted-prior-sibling"

    @function_tool(finish_turn=True)
    def done() -> str:
        return "terminal-admission-sentinel"

    @function_tool(finish_turn=True)
    def other() -> str:
        return "other-terminal-sibling"

    entered, release = asyncio.Event(), asyncio.Event()

    async def check(request, source):
        assert request.tool_call is not None
        assert request.tool_call.call_id == "terminal-call"
        entered.set()
        await release.wait()
        if mode == "error":
            raise ValueError("synthetic guard error")
        return GuardrailResult(mode == "reject")

    responses.respond(function_call("prior", {}, "prior-call"))
    calls = [function_call("done", {}, "terminal-call")]
    if mode == "siblings":
        calls.append(function_call("other", {}, "sibling-call"))
    responses.respond(*calls)
    async with (
        timeout(30),
        Agent(
            instructions="Use prior then done.",
            tools=[prior, done, other],
            provider=provider(responses),
            output_guardrails=[OutputGuardrail("admission", check)],
        ).session() as session,
    ):
        turn = await session.submit("begin")
        events = []

        async def read_events():
            async for event in turn.events(include_execution_details=True):
                events.append(event)

        reading = asyncio.create_task(read_events())
        try:
            await entered.wait()
            await asyncio.sleep(0)
            assert "terminal-admission-sentinel" not in repr(events)
            assert "terminal-admission-sentinel" not in json.dumps(
                [item async for item in session.items()]
            )
            if mode == "cancel":
                stopping = asyncio.create_task(turn.cancel())
                await asyncio.sleep(0)
                release.set()
                await stopping
            else:
                release.set()
            if mode == "reject":
                with pytest.raises(GuardrailTripwire):
                    await turn.get_result()
            elif mode == "cancel":
                with pytest.raises((asyncio.CancelledError, TurnStoppedError, NativeError)):
                    await turn.get_result()
            elif mode == "error":
                with pytest.raises(ValueError, match="synthetic guard error"):
                    await turn.get_result()
            else:
                assert (await turn.get_result()).final_output == "terminal-admission-sentinel"
            await reading
            finished = [event for event in events if isinstance(event, ToolFinished)]
            selected = [event for event in finished if event.call_id == "terminal-call"]
            assert len(selected) == 1
            assert "accepted-prior-sibling" in repr(
                [event for event in finished if event.call_id == "prior-call"]
            )
            if mode in {"approve", "duplicate", "siblings"}:
                assert "terminal-admission-sentinel" in repr(selected)
            else:
                assert "terminal-admission-sentinel" not in repr(events)
            if mode == "siblings":
                sibling = [event for event in finished if event.call_id == "sibling-call"]
                assert len(sibling) == 1
                assert "other-terminal-sibling" in repr(sibling)
        finally:
            release.set()
            await asyncio.gather(reading, return_exceptions=True)


@pytest.mark.parametrize("reject_one", [False, True])
async def test_child_terminal_observations_keep_equal_call_ids_separate(
    tmp_path, monkeypatch, reject_one
):
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    from agents import NativeAgents, function_tool
    from agents.observation import ToolFinished
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.native_fixtures import function_call
    from tests.test_native_child_agents import ChildPeer, handles, metadata, trust
    from tests.test_native_executor_tools import declaration

    class Peer(ChildPeer):
        async def hold(self, event):
            pass

        def response(self):
            body = self.requests[-1]
            path = metadata(body, self.request_headers[-1])["agent_name"]
            if path == "/root":
                return super().response()
            self.child_requests.setdefault(path, []).append(body)
            return [function_call("done", {}, "same-child-call")]

    @function_tool(finish_turn=True)
    def done(context: ToolContext[None]) -> str:
        return f"child-output:{context.call.child_id}"

    blocked = []

    def check(request, source):
        if request.tool_call is None:
            return GuardrailResult(False)
        child_id = request.tool_call.child_id
        assert request.final_output == f"child-output:{child_id}"
        if reject_one and not blocked:
            blocked.append(child_id)
        return GuardrailResult(child_id in blocked)

    peer = Peer(("default", "default"))
    app = web.Application()
    app.router.add_post("/v1/responses", peer.handle)
    app.router.add_get("/v1/responses", peer.websocket)
    app.on_shutdown.append(peer.close_sockets)
    async with (
        timeout(30),
        TestServer(app, shutdown_timeout=1) as server,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        peer.url = str(server.make_url("/v1"))
        trust(executor, tmp_path, monkeypatch)
        async with Agent(
            instructions="Spawn alpha and beta.",
            model="gpt-5.5",
            tools=[NativeAgents(max_children=2), done],
            provider=peer.provider(websocket=False),
            output_guardrails=[OutputGuardrail("children", check)],
        ).session(environment=declaration(executor)) as session:
            await (await session.submit("begin")).get_result()
            children = await handles(session, 2)
            assert len(children) == 2
            for child in children:
                try:
                    completed = await child.get_result()
                except GuardrailTripwire:
                    assert child.child_id in blocked
                else:
                    assert child.child_id not in blocked
                    assert completed.final_output == f"child-output:{child.child_id}"
                events = [event async for event in child.events(include_execution_details=True)]
                finished = [
                    event
                    for event in events
                    if isinstance(event, ToolFinished) and event.call_id == "same-child-call"
                ]
                assert len(finished) == 1
                if child.child_id in blocked:
                    assert f"child-output:{child.child_id}" not in repr(events)
                else:
                    assert f"child-output:{child.child_id}" in repr(finished)
                for sibling in children:
                    if sibling.child_id != child.child_id:
                        assert f"child-output:{sibling.child_id}" not in repr(finished)
