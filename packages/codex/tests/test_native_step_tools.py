"""Real provider rosters and dispatch from the immutable native step plan."""

from __future__ import annotations

import asyncio

import pytest

from agents import (
    Agent,
    CapabilityError,
    Checklist,
    Completed,
    FunctionTool,
    HttpMCP,
    InteractionExpiredError,
    NativeError,
    SessionBusyError,
    ToolRef,
    TurnStoppedError,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_mcp_tools import Peer, declarations, mcp_call


async def test_selected_slots_and_accepted_identity_in_one_turn(
    responses: ResponsesFixture,
) -> None:
    effects = []
    steps = []

    @function_tool
    def first() -> str:
        effects.append("first")
        return "result A"

    @function_tool
    def second() -> str:
        effects.append("second")
        return "result B"

    async def choose(turn, *, step_index):
        steps.append((turn, step_index))
        await asyncio.sleep(0)
        return ([first], [second.ref], [])[step_index]

    responses.respond(function_call("first", {}, "fst"))
    responses.respond(function_call("second", {}, "snd"))
    responses.respond(assistant_output("complete"))
    async with (
        timeout(15),
        Agent(
            instructions="Follow supplied tools.",
            tools=[first, second],
            provider=provider(responses),
        ).session() as session,
    ):
        turn = await session.submit("Use the tools", before_step=choose)
        result = await turn.get_result()
        assert result.final_output == "complete"
        assert [(h is turn, i) for h, i in steps] == [(True, 0), (True, 1), (True, 2)]
        assert effects == ["first", "second"]
        assert [set(declarations(r)) for r in responses.requests] == [
            {("functions", "first")},
            {("functions", "second")},
            set(),
        ]
        assert tool_output(responses.requests[1], "fst") == "result A"
        assert tool_output(responses.requests[2], "snd") == "result B"


async def test_failed_selector_cannot_sample_or_dispatch(responses: ResponsesFixture) -> None:
    effects = []

    @function_tool
    def forbidden() -> str:
        effects.append("effect")
        return "no"

    async def refuse(turn, *, step_index):
        raise LookupError("selection failed")

    # Would cause an effect if the request escaped the failure boundary.
    responses.respond(function_call("forbidden", {}, "unwanted"))
    responses.respond(assistant_output("wrong"))
    async with (
        timeout(15),
        Agent(
            instructions="Use tools", tools=[forbidden], provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("Try", before_step=refuse)
        with pytest.raises(LookupError, match="selection failed"):
            await turn.get_result()
        assert not responses.requests and not effects


async def test_stop_pending_selector_never_advertises(responses: ResponsesFixture) -> None:
    entered = asyncio.Event()
    finished = asyncio.Event()

    async def hold(turn, *, step_index):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    responses.respond(assistant_output("wrong"))
    async with (
        timeout(15),
        Agent(instructions="No effect", provider=provider(responses)).session() as session,
    ):
        turn = await session.submit("Pause", before_step=hold)
        await entered.wait()
        await turn.cancel()
        with pytest.raises(TurnStoppedError):
            await turn.get_result()
        assert finished.is_set()
        assert not responses.requests


async def test_model_cannot_dispatch_unselected_slot(responses: ResponsesFixture) -> None:
    effects = []

    @function_tool
    def visible() -> str:
        effects.append("visible")
        return "allowed"

    @function_tool
    def hidden() -> str:
        effects.append("hidden")
        return "forbidden"

    async def choose(turn, *, step_index):
        return [visible]

    responses.respond(function_call("hidden", {}, "off-roster"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(15),
        Agent(
            instructions="Use selected tools", tools=[visible, hidden], provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("Select", before_step=choose)
        try:
            await turn.get_result()
        except NativeError:
            pass
        assert not effects
        assert all(("functions", "hidden") not in declarations(r) for r in responses.requests)


async def test_step_callback_cannot_widen_the_operation(responses: ResponsesFixture) -> None:
    effects = []

    @function_tool
    def allowed() -> str:
        return "allowed"

    @function_tool
    def other() -> str:
        effects.append("other")
        return "forbidden"

    async def choose(turn, *, step_index):
        return [other]

    responses.respond(function_call("other", {}, "off-op"))
    async with (
        timeout(15),
        Agent(
            instructions="Narrow", tools=[allowed, other], provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("Select", tools=[allowed], before_step=choose)
        with pytest.raises(CapabilityError, match="distinct admitted"):
            await turn.get_result()
        assert not effects and not responses.requests


async def test_parallel_manual_slots_survive_until_their_result_and_then_expire(
    responses: ResponsesFixture,
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Look up input", input_type=LookupArgs, output_type=str
    )
    effects = []

    @function_tool
    def publish() -> str:
        effects.append("published")
        return "recorded"

    async def choose(turn, *, step_index):
        return ([lookup], [publish], [])[step_index]

    responses.respond(
        function_call("lookup", {"key": "A"}, "call-A"),
        function_call("lookup", {"key": "B"}, "call-B"),
    )
    responses.respond(function_call("publish", {}, "publish"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(20),
        Agent(
            instructions="Parallel replies.", tools=[lookup, publish], provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("Look up", before_step=choose)
        stream = session.interactions()
        a = await next_request(stream, lookup)
        b = await next_request(stream, lookup)
        assert {a.arguments.key, b.arguments.key} == {"A", "B"}
        assert len(responses.requests) == 1 and not effects
        await a.respond("first receipt")
        assert not effects
        await b.respond("second receipt")
        assert (await turn.get_result()).final_output == "done"
        assert effects == ["published"]
        assert [set(declarations(r)) for r in responses.requests] == [
            {("functions", "lookup")},
            {("functions", "publish")},
            set(),
        ]
        assert {tool_output(responses.requests[1], key) for key in ("call-A", "call-B")} == {
            "first receipt",
            "second receipt",
        }
        with pytest.raises(InteractionExpiredError):
            await a.respond("late duplicate")


async def test_step_and_operation_scope_end_at_the_completed_turn(
    responses: ResponsesFixture,
) -> None:
    effects = []

    @function_tool
    def one() -> str:
        effects.append("one")
        return "a"

    @function_tool
    def two() -> str:
        effects.append("two")
        return "b"

    seen = []

    def choose(turn, *, step_index):
        seen.append(turn)
        return []

    responses.respond(assistant_output("first"))
    responses.respond(function_call("two", {}, "second-op"))
    responses.respond(assistant_output("second"))
    async with (
        timeout(15),
        Agent(
            instructions="Two operations", tools=[one, two], provider=provider(responses)
        ).session() as session,
    ):
        assert (
            await (await session.submit("No effects", tools=[one], before_step=choose)).get_result()
        ).final_output == "first"
        assert (await (await session.submit("Now default")).get_result()).final_output == "second"
        assert len(seen) == 1 and effects == ["two"]
        assert [set(declarations(r)) for r in responses.requests] == [
            set(),
            {("functions", "one"), ("functions", "two")},
            {("functions", "one"), ("functions", "two")},
        ]


async def test_http_dormant_operation_and_default_reset(responses: ResponsesFixture) -> None:
    peer = Peer()
    effects = []

    @function_tool
    def local() -> str:
        effects.append("local")
        return "local receipt"

    async with timeout(25), peer.serve() as url:
        remote = HttpMCP(name="docs", url=url, tools=["search"])
        agent = Agent(
            instructions="Use selected tools.", tools=[local, remote], provider=provider(responses)
        )
        async with agent.session() as session:
            assert not peer.requests
            responses.respond(function_call("local", {}, "local-only"))
            responses.respond(assistant_output("local done"))
            assert (
                await (await session.submit("Local", tools=[local])).get_result()
            ).final_output == "local done"
            assert not peer.requests and effects == ["local"]
            assert set(declarations(responses.requests[0])) == {("functions", "local")}

            responses.respond(mcp_call("search", "mcp__docs", "mcp-only"))
            responses.respond(assistant_output("remote done"))
            assert (
                await (await session.submit("Remote", tools=[remote.tool("search")])).get_result()
            ).final_output == "remote done"
            assert [call["_meta"]["callId"] for call in peer.calls] == ["mcp-only"]
            assert set(declarations(responses.requests[2])) == {("mcp__docs", "search")}

            responses.respond(assistant_output("none"))
            assert (
                await (await session.submit("Empty", tools=[])).get_result()
            ).final_output == "none"
            assert not declarations(responses.requests[-1])

            responses.respond(assistant_output("default"))
            assert (await (await session.submit("Defaults")).get_result()).final_output == "default"
            assert set(declarations(responses.requests[-1])) == {
                ("functions", "local"),
                ("mcp__docs", "search"),
            }


async def test_admission_and_override_before_remote_acquisition(
    responses: ResponsesFixture,
) -> None:
    peer = Peer()

    @function_tool
    def local() -> str:
        return "good"

    @function_tool(name="local")
    def replaced() -> str:
        raise AssertionError("new binding was not admitted")

    async with timeout(20), peer.serve() as url:
        remote = HttpMCP(name="docs", url=url, tools=["search"])
        agent = Agent(instructions="Use selected tools.", provider=provider(responses))
        async with agent.session(tools=[local, remote]) as session:
            with pytest.raises(CapabilityError):
                await session.submit("Invalid", tools=[replaced, remote])
            assert not peer.requests and not responses.requests
            responses.respond(assistant_output("safe"))
            assert (
                await (await session.submit("No tools", tools=[])).get_result()
            ).final_output == "safe"
            assert not declarations(responses.requests[-1])
            assert not peer.requests
        assert agent.tools == ()


async def test_admission_is_reserved_until_http_publication_and_submit(
    responses: ResponsesFixture,
) -> None:
    class PausingPeer(Peer):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def handle(self, request):
            if (await request.json()).get("method") == "tools/list":
                self.entered.set()
                await self.release.wait()
            return await super().handle(request)

    peer = PausingPeer()
    async with timeout(20), peer.serve() as url:
        remote = HttpMCP(name="docs", url=url, tools=["search"])
        responses.respond(assistant_output("accepted"))
        async with Agent(
            instructions="Use HTTP", tools=[remote], provider=provider(responses)
        ).session() as session:
            assert not peer.entered.is_set()
            opening = asyncio.create_task(session.submit("Remote", tools=[remote]))
            try:
                await peer.entered.wait()
                assert not responses.requests
                with pytest.raises(SessionBusyError):
                    await session.submit("Must not race publication", tools=[])
            finally:
                peer.release.set()
            turn = await opening
            assert (await turn.get_result()).final_output == "accepted"
            assert set(declarations(responses.requests[0])) == {("mcp__docs", "search")}


async def test_fixed_native_capability_is_not_silently_left_on_an_empty_operation(
    responses: ResponsesFixture,
) -> None:

    async with (
        timeout(15),
        Agent(
            instructions="Plan", tools=[Checklist()], provider=provider(responses)
        ).session() as session,
    ):
        responses.respond(assistant_output("No tools"))
        assert (
            await (await session.submit("No tools", tools=[])).get_result()
        ).final_output == "No tools"
        assert set(declarations(responses.requests[-1])) == set()
        responses.respond(assistant_output("Plan available"))
        assert (
            await (await session.submit("Default")).get_result()
        ).final_output == "Plan available"
        assert ("functions", "update_plan") in declarations(responses.requests[-1])


async def test_http_step_removes_confirmed_operation_roster(
    responses: ResponsesFixture,
) -> None:
    peer = Peer()
    async with timeout(15), peer.serve() as url:
        remote = HttpMCP(name="docs", url=url, tools=["search"])
        async with Agent(
            instructions="Use HTTP", tools=[remote], provider=provider(responses)
        ).session() as session:
            responses.respond(mcp_call("search", "mcp__docs", "forged"))
            responses.respond(assistant_output("No remote effect"))
            turn = await session.submit("Dynamic", before_step=lambda turn, step_index: [])
            assert (await turn.get_result()).final_output == "No remote effect"
            assert peer.requests and not peer.calls
            assert all(not declarations(request) for request in responses.requests)


async def test_pending_dynamic_operation_cannot_discard_its_callback_at_restore(
    responses: ResponsesFixture,
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Look up input", input_type=LookupArgs, output_type=str
    )
    effects = []

    @function_tool
    def prohibited() -> str:
        effects.append("effect")
        return "effect"

    def choose(turn, *, step_index):
        return [lookup]

    responses.respond(function_call("lookup", {"key": "A"}, "restore-A"))
    responses.respond(assistant_output("reply received"))
    async with (
        timeout(20),
        Agent(
            instructions="Wait for reply", tools=[lookup, prohibited], provider=provider(responses)
        ).session() as session,
    ):
        turn = await session.submit("Look up", before_step=choose)
        request = await next_request(session.interactions(), lookup)
        # A new Session cannot recover an operation callback from portable
        # native history. Refuse before spending the current live grant.
        with pytest.raises(CapabilityError, match="operation"):
            await session.suspend()
        assert (await session.checkpoint_status(kind="manual_wait")).status == "not_ready"
        await request.respond("still owned by the original")
        assert (await turn.get_result()).final_output == "reply received"
        assert not effects
        assert [set(declarations(r)) for r in responses.requests] == [
            {("functions", "lookup")},
            {("functions", "lookup")},
        ]


async def test_pending_static_subset_cannot_widen_on_manual_restore(
    responses: ResponsesFixture,
) -> None:
    lookup = FunctionTool(
        name="lookup", description="Look up input", input_type=LookupArgs, output_type=str
    )

    effects = []

    @function_tool
    def other() -> str:
        effects.append("other")
        return "new default receipt"

    responses.respond(function_call("lookup", {"key": "A"}, "static-A"))
    agent = Agent(
        instructions="Wait for reply",
        tools=[lookup, other, Checklist()],
        provider=provider(responses),
    )
    async with timeout(20):
        async with agent.session() as source:
            turn = await source.submit("Look up", tools=[lookup])
            request = await next_request(source.interactions(), lookup)
            saved = await source.suspend()
            assert [ref.name for ref in saved.operation_tools] == ["lookup"]
            with pytest.raises(InteractionExpiredError):
                await request.respond("expired on the old writer")
        responses.respond(function_call("other", {}, "out-of-scope"))
        responses.respond(assistant_output("same restricted turn"))
        async with agent.session(state=saved) as restored:
            recovered = restored.restored_turn
            assert recovered is not None and recovered.turn_id == turn.turn_id
            current = await next_request(restored.interactions(), lookup)
            await current.respond("current grant receipt")
            assert (await recovered.get_result()).final_output == "same restricted turn"
            assert effects == []
            assert [set(declarations(r)) for r in responses.requests] == [
                {("functions", "lookup")},
                {("functions", "lookup")},
                {("functions", "lookup")},
            ]
            responses.respond(function_call("other", {}, "new-operation"))
            responses.respond(assistant_output("now default"))
            next_turn = await restored.submit("Begin anew")
            assert (await next_turn.get_result()).final_output == "now default"
            assert effects == ["other"]
            assert (await restored.save_state()).operation_tools is None


async def test_static_manual_http_restore_uses_selected_current_bindings_only(
    responses: ResponsesFixture,
) -> None:
    old, current, dormant = Peer(), Peer(), Peer()
    lookup = FunctionTool(
        name="lookup", description="Look up input", input_type=LookupArgs, output_type=str
    )
    async with (
        timeout(25),
        old.serve() as old_url,
        current.serve() as current_url,
        dormant.serve() as dormant_url,
    ):
        agent = Agent(instructions="Wait", provider=provider(responses))
        old_remote = HttpMCP(name="docs", url=old_url, tools=["search", "excluded"])
        unused = HttpMCP(name="dormant", url=dormant_url, tools=["search"])
        current_remote = HttpMCP(name="docs", url=current_url, tools=["search", "excluded"])
        responses.respond(function_call("lookup", {"key": "A"}, "manual-A"))
        async with agent.session(tools=[lookup, old_remote, unused]) as source:
            original = await source.submit("Wait", tools=[lookup, old_remote.tool("search")])
            stale = await next_request(source.interactions(), lookup)
            saved = await source.suspend()
        assert old_url not in saved.to_json()
        assert not dormant.requests
        count = len(old.requests)
        responses.respond(mcp_call("search", "mcp__docs", "actual-current"))
        responses.respond(mcp_call("excluded", "mcp__docs", "not-allowed"))
        responses.respond(assistant_output("received"))
        async with agent.session(state=saved, tools=[lookup, current_remote, unused]) as restored:
            live = await next_request(restored.interactions(), lookup)
            assert restored.restored_turn is not None
            assert restored.restored_turn.turn_id == original.turn_id
            with pytest.raises(InteractionExpiredError):
                await stale.respond("no old authority")
            await live.respond("ack")
            assert (await restored.restored_turn.get_result()).final_output == "received"
            assert [call["_meta"]["callId"] for call in current.calls] == ["actual-current"]
            assert len(old.requests) == count and not dormant.requests
            assert all(
                set(declarations(r)) == {("functions", "lookup"), ("mcp__docs", "search")}
                for r in responses.requests
            )


async def test_restored_subset_cannot_mint_a_tool_or_issue_an_outside_pending_grant(
    responses: ResponsesFixture,
) -> None:
    from dataclasses import replace

    lookup = FunctionTool(
        name="lookup", description="Look up input", input_type=LookupArgs, output_type=str
    )
    other = FunctionTool(
        name="other", description="Not selected", input_type=LookupArgs, output_type=str
    )
    agent = Agent(instructions="Wait", tools=[lookup, other], provider=provider(responses))
    responses.respond(function_call("lookup", {"key": "A"}, "manual-A"))
    async with timeout(20), agent.session() as source:
        await source.submit("Wait", tools=[lookup])
        await next_request(source.interactions(), lookup)
        saved = await source.suspend()
    assert saved.operation_tools is not None
    for invalid in ([replace(lookup.ref, name="not-bound")], [other.ref]):
        malformed = saved.model_copy(update={"operation_tools": invalid})
        with pytest.raises((ValueError, CapabilityError), match="opened|pending manual"):
            async with agent.session(state=malformed):
                pytest.fail("An unbound selection or outside pending grant was admitted")
    assert len(responses.requests) == 1


async def test_owned_deferred_operation_restore_keeps_remote_subset_and_claims_once(
    responses: ResponsesFixture,
) -> None:
    from tests.test_deferred_jobs import Journal, Provider, declared, done

    journal, backend = Journal(), Provider([done("current render")])
    jobs, render = declared(journal, backend)
    selected, dormant = Peer(), Peer()
    async with timeout(25), selected.serve() as url, dormant.serve() as dormant_url:
        remote = HttpMCP(name="docs", url=url, tools=["search", "excluded"])
        unused = HttpMCP(name="dormant", url=dormant_url, tools=["search"])
        agent = Agent(instructions="Render", provider=provider(responses))
        responses.respond(function_call("render", {"pages": 2}, "render-current"))
        responses.respond(mcp_call("search", "mcp__docs", "current-search"))
        responses.respond(assistant_output("done"))
        async with agent.session(tools=[render, remote, unused]) as live:
            original = await live.submit("Render", tools=[render, remote.tool("search")])
            request = await next_request(live.interactions(), render)
            async with jobs.start(live, request) as (restored, resumed):
                assert resumed is restored.restored_turn
                assert resumed.turn_id == original.turn_id
                assert (await resumed.get_result()).final_output == "done"
            assert not dormant.requests
        assert backend.calls == [("start", 2)]
        assert journal.log.count("claim-start") == 1
        assert "current render" in tool_output(responses.requests[1], "render-current")
        assert [call["_meta"]["callId"] for call in selected.calls] == ["current-search"]
        assert all(
            set(declarations(r)) == {("functions", "render"), ("mcp__docs", "search")}
            for r in responses.requests
        )


async def test_failed_remote_publication_settles_submit_and_fences_before_sampling(
    responses: ResponsesFixture,
) -> None:
    peer = Peer(names=("present",))
    async with timeout(15), peer.serve() as url:
        remote = HttpMCP(name="docs", url=url, tools=["missing"])
        async with Agent(
            instructions="Do not proceed until tools are ready",
            tools=[remote],
            provider=provider(responses),
        ).session() as session:
            assert not peer.requests
            with pytest.raises(NativeError, match="selected tools unavailable|not.*ready"):
                async with timeout(5):
                    await (await session.submit("Use missing", tools=[remote])).get_result()
            assert not responses.requests and not peer.calls
            with pytest.raises(RuntimeError, match="unconfirmed|closed"):
                await session.submit("Cannot pass an unconfirmed publication", tools=[])


async def test_cancel_during_remote_publication_joins_owned_admission(
    responses: ResponsesFixture,
) -> None:
    release = asyncio.Event()
    peer = Peer(release_ready=release)
    async with timeout(15), peer.serve() as url:
        remote = HttpMCP(name="docs", url=url, tools=["search"])
        async with Agent(
            instructions="Wait for confirmed tools", tools=[remote], provider=provider(responses)
        ).session() as session:
            responses.respond(assistant_output("race completed"))
            pending = await session.submit("Use remote", tools=[remote])
            try:
                await peer.ready_entered.wait()
                assert not responses.requests and pending.turn_id is None
                stopping = asyncio.create_task(pending.cancel())
                await asyncio.sleep(0)
                assert not stopping.done()
                with pytest.raises((SessionBusyError, RuntimeError)):
                    await session.submit("Do not race cancellation", tools=[])
            finally:
                release.set()
            await stopping
            # Once promoted cancellation joins the real admission; a fast native
            # answer may have won. No zero-effects promise at this boundary.
            try:
                completed = await pending.get_result()
                assert completed.turn is pending
            except TurnStoppedError:
                pass
            assert not session._root_interrupts


@pytest.mark.parametrize("entry", ["agent", "agent_sync"])
async def test_one_shot_run_keeps_completed_and_selects_from_its_own_opening(
    responses: ResponsesFixture, entry: str
) -> None:
    peer = Peer()
    effects = []
    steps = []

    @function_tool
    def lookup() -> str:
        effects.append("local")
        return "local receipt"

    def choose(turn, *, step_index):
        steps.append(turn)
        return [lookup] if step_index == 0 else []

    async with timeout(15), peer.serve() as url:
        remote = HttpMCP(name="docs", url=url, tools=["search"])
        agent = Agent(instructions="Read", tools=[lookup, remote], provider=provider(responses))
        responses.respond(function_call("lookup", {}, "only-local"))
        responses.respond(assistant_output("complete"))
        if entry == "agent":
            result = await agent.run("read", tools=[lookup], before_step=choose)
        else:
            result = await asyncio.to_thread(
                agent.run_sync, "read", tools=[lookup], before_step=choose
            )
        assert isinstance(result, Completed)
        assert result.final_output == "complete"
        assert steps == [result.turn, result.turn]
        assert effects == ["local"] and not peer.requests
        assert [set(declarations(r)) for r in responses.requests] == [
            {("functions", "lookup")},
            set(),
        ]


@pytest.mark.parametrize("entry", ["agent", "agent_sync"])
async def test_one_shot_empty_selection_is_not_the_opening_default(
    responses: ResponsesFixture, entry: str
) -> None:
    effects = []

    @function_tool
    def forbidden() -> str:
        effects.append("not admitted")
        return "forbidden"

    agent = Agent(instructions="Do not use tools", tools=[forbidden], provider=provider(responses))
    responses.respond(assistant_output("done"))
    async with timeout(15):
        if entry == "agent":
            result = await agent.run("read", tools=[])
        else:
            result = await asyncio.to_thread(agent.run_sync, "read", tools=[])
        assert isinstance(result, Completed) and result.final_output == "done"
        assert not declarations(responses.requests[0]) and not effects


async def test_one_native_plan_alternates_families_empty_and_operation_reset(
    responses: ResponsesFixture,
) -> None:
    effects = []

    @function_tool
    def local() -> str:
        effects.append("local")
        return "local accepted"

    checklist = Checklist()
    peer = Peer()
    async with timeout(25), peer.serve() as url:
        remote = HttpMCP(name="docs", url=url, tools=["search"])
        selections = ([local], [remote.tool("search")], [checklist], [], None)
        responses.respond(function_call("local", {}, "python"))
        responses.respond(mcp_call("search", "mcp__docs", "remote"))
        responses.respond(
            function_call(
                "update_plan", {"plan": [{"step": "selected", "status": "completed"}]}, "plan"
            )
        )
        responses.respond(
            function_call(
                "update_plan", {"plan": [{"step": "forged", "status": "in_progress"}]}, "denied"
            )
        )
        responses.respond(assistant_output("done"))
        async with Agent(
            instructions="Use selected tools",
            tools=[local, remote, checklist],
            provider=provider(responses),
        ).session() as session:
            turn = await session.submit(
                "Alternate", before_step=lambda turn, step_index: selections[step_index]
            )
            assert (await turn.get_result()).final_output == "done"
            assert effects == ["local"]
            assert [call["_meta"]["callId"] for call in peer.calls] == ["remote"]
            assert [set(declarations(request)) for request in responses.requests] == [
                {("functions", "local")},
                {("mcp__docs", "search")},
                {("functions", "update_plan")},
                set(),
                {("functions", "local"), ("mcp__docs", "search"), ("functions", "update_plan")},
            ]
            plan = await session.checklist()
            assert plan is not None and [item.step for item in plan.plan] == ["selected"]


async def test_manual_restore_keeps_selected_fixed_identity_and_rejects_missing_destination(
    responses: ResponsesFixture,
) -> None:
    lookup = FunctionTool(name="lookup", description="Wait", input_type=LookupArgs, output_type=str)
    checklist = Checklist()
    agent = Agent(instructions="Wait", tools=[lookup, checklist], provider=provider(responses))
    responses.respond(function_call("lookup", {"key": "A"}, "manual"))
    async with timeout(25):
        async with agent.session() as source:
            await source.submit("Wait", tools=[lookup, checklist])
            await next_request(source.interactions(), lookup)
            saved = await source.suspend()
            assert set(saved.operation_tools) == {lookup.ref, ToolRef("update_plan", "functions")}
        destination = Agent(instructions="Wait", tools=[lookup], provider=agent.provider)
        with pytest.raises(CapabilityError, match="allowed"):
            async with destination.session(state=saved):
                pytest.fail("A missing fixed declaration must fail before enabling tools")
        responses.respond(assistant_output("restored"))
        async with agent.session(state=saved) as restored:
            request = await next_request(restored.interactions(), lookup)
            await request.respond("accepted")
            assert (await restored.restored_turn.get_result()).final_output == "restored"
            assert set(declarations(responses.requests[-1])) == {
                ("functions", "lookup"),
                ("functions", "update_plan"),
            }


async def test_incremental_configuration_uses_exact_replacement_only_for_selected_steps(
    responses: ResponsesFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from _agents_codex import telemetry as runtime_telemetry

    monkeypatch.setattr(
        runtime_telemetry, "_engine_options", json.dumps({"features.incremental_tools": True})
    )
    effects = []

    @function_tool
    def local() -> str:
        effects.append("local")
        return "accepted"

    @function_tool
    def other() -> str:
        pytest.fail("An excluded runtime executed")

    agent = Agent(
        instructions="Use selected tools",
        model="gpt-6.1-sol",
        tools=[local, other],
        provider=provider(responses),
    )
    async with timeout(30):
        responses.respond(assistant_output("unfiltered"))
        async with agent.session() as selected:
            await (await selected.submit("Ordinary")).get_result()
            unfiltered = responses.requests[0]
            assert not unfiltered.get("tools")
            assert any(item.get("type") == "additional_tools" for item in unfiltered["input"])
            original_history = (await selected.save_state()).history
            original_definitions = [
                item
                for item in original_history
                if item.get("payload", {}).get("type") == "additional_tools"
            ]
            assert original_definitions
            responses.respond(function_call("local", {}, "selected"))
            responses.respond(function_call("local", {}, "forged-after-removal"))
            responses.respond(assistant_output("reset"))
            choices = ([local], [], None)
            await (
                await selected.submit(
                    "Select",
                    before_step=lambda turn, step_index: choices[step_index],
                )
            ).get_result()
            # Stored history still contains the earlier accepted declarations.
            assert [
                item
                for item in (await selected.save_state()).history
                if item.get("payload", {}).get("type") == "additional_tools"
            ] == original_definitions
        assert effects == ["local"]
        assert [set(declarations(request)) for request in responses.requests[1:]] == [
            {("functions", "local")},
            set(),
            {("functions", "local"), ("functions", "other")},
        ]
        # Responses Lite puts the complete replacement roster in one fresh
        # AdditionalTools prefix. Historical append-only rosters must be gone.
        assert [
            sum(item.get("type") == "additional_tools" for item in request["input"])
            for request in responses.requests[1:]
        ] == [1, 0, 1]


@pytest.mark.requires_native_linux_sandbox
@pytest.mark.parametrize("nested", [False, True])
async def test_unselected_executor_function_and_freeform_have_no_approval_or_effect(
    responses: ResponsesFixture,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    nested: bool,
) -> None:
    from tests.native_executor_fixtures import ExecutorPeer
    from tests.test_native_child_agents import trust
    from tests.test_native_code_mode import code_call, code_output
    from tests.test_native_executor_tools import declaration

    approvals = []

    @function_tool
    def local() -> str:
        return "allowed leaf"

    async with timeout(40), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        environment = declaration(
            executor, approval=lambda ctx, args: approvals.append(args) or True
        )
        if nested:
            responses.respond(
                code_call(
                    "text([typeof tools.exec_command, typeof tools.apply_patch]); "
                    "try { await tools.exec_command({cmd:'touch forbidden'}); } "
                    "catch (e) { text('denied'); }",
                    "nested",
                )
            )
        else:
            responses.respond(function_call("exec_command", {"cmd": "touch forbidden"}, "command"))
            responses.respond(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "custom_tool_call",
                        "call_id": "patch",
                        "name": "apply_patch",
                        "input": (
                            "*** Begin Patch\n*** Add File: forbidden\n+effect\n*** End Patch\n"
                        ),
                    },
                }
            )
        responses.respond(assistant_output("done"))
        async with Agent(
            instructions="Use only local",
            tools=[local],
            programmatic_tool_calling=nested,
            provider=provider(responses),
        ).session(environment=environment) as session:
            await (
                await session.submit(
                    "No remote effects", before_step=lambda turn, step_index: [local]
                )
            ).get_result()
            assert not approvals and not (executor.root / "forbidden").exists()
            assert not any(
                request.get("method") in {"process/start", "fs/writeFile"}
                for request in executor.requests
            )
            assert all(
                not (
                    {("functions", "exec_command"), ("functions", "apply_patch")}
                    & set(declarations(request))
                )
                for request in responses.requests
            )
            if nested:
                output = code_output(responses.requests[-1], "nested")
                assert "undefined" in output and "denied" in output


async def test_deferred_search_cannot_reintroduce_a_leaf_removed_in_the_next_step(
    responses: ResponsesFixture,
) -> None:
    import json

    from tests.test_native_schema_discovery import discovered, search

    effects = []

    @function_tool(defer_loading=True, description="Find the skylark record")
    def allowed() -> str:
        effects.append("allowed")
        return "accepted"

    @function_tool(defer_loading=True, description="Delete the skylark record")
    def excluded() -> str:
        pytest.fail("Search reintroduced an excluded tool")

    responses.respond(search("skylark", "first-search"))
    responses.respond(function_call("allowed", {}, "allowed-effect"))
    responses.respond(search("skylark", "second-search"))
    responses.respond(function_call("allowed", {}, "stale-search-leaf"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(30),
        Agent(
            instructions="Use selected tools",
            model="gpt-5.5",
            tools=[allowed, excluded],
            provider=provider(responses),
        ).session() as session,
    ):
        await (
            await session.submit(
                "Search",
                before_step=lambda turn, step_index: [allowed] if step_index < 2 else [excluded],
            )
        ).get_result()
        assert effects == ["allowed"]
        first = json.dumps(
            next(
                item
                for item in responses.requests[1]["input"]
                if item.get("type") == "tool_search_output"
                and item.get("call_id") == "first-search"
            )
        )
        second = json.dumps(discovered(responses, "second-search"))
        assert "allowed" in first and "excluded" not in first
        assert "excluded" in second and "allowed" not in second


async def test_yielded_cell_keeps_issuing_step_router_after_successor_removes_leaf(
    responses: ResponsesFixture,
) -> None:
    import json

    from tests.test_native_code_mode import code_call, code_output

    entered = asyncio.Event()
    release = asyncio.Event()
    effects = []

    @function_tool
    async def first() -> str:
        entered.set()
        await release.wait()
        effects.append("first")
        return "first accepted"

    @function_tool
    def second() -> str:
        effects.append("second")
        return "issuing step accepted"

    async def choose(turn, *, step_index):
        if not step_index:
            return [first, second]
        return [first]

    async def supply_wait(event):
        if event.get("item", {}).get("call_id") == "join-cell":
            # The nested dispatcher runs during exec/wait, not while Core
            # awaits the next selector. Release only after step 1 selected.
            release.set()
            output = code_output(responses.requests[-1], "old-cell")
            prefix = "Script running with cell ID "
            assert prefix in output
            cell = output.split(prefix, 1)[1].split()[0].rstrip(".")
            event["item"]["arguments"] = json.dumps({"cell_id": cell, "yield_time_ms": 1000})

    responses.before_event = supply_wait
    responses.respond(
        code_call(
            "const pending = tools.first({}); yield_control(); "
            "await pending; text(await tools.second({}));",
            "old-cell",
        )
    )
    responses.respond(function_call("wait", {}, "join-cell"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(40),
        Agent(
            instructions="Keep the issuing roster",
            tools=[first, second],
            programmatic_tool_calling=True,
            provider=provider(responses),
        ).session() as session,
    ):
        await (await session.submit("Yield then continue", before_step=choose)).get_result()
        assert entered.is_set()
        assert effects == ["first", "second"]
        assert "issuing step accepted" in tool_output(responses.requests[-1], "join-cell")
        # Code mode advertises nested tools in exec's description rather than
        # as direct declarations. The successor must lose that leaf while the
        # already-yielded cell retains its issuing step's dispatcher.
        assert (
            "### `second`"
            in declarations(responses.requests[0])[("functions", "exec")]["description"]
        )
        assert all(
            "### `second`" not in declarations(request)[("functions", "exec")]["description"]
            for request in responses.requests[1:]
        )


async def test_cancelled_selector_reply_cannot_publish_into_queued_successor(
    responses: ResponsesFixture,
) -> None:
    cancelled = asyncio.Event()
    release = asyncio.Event()
    entered = asyncio.Event()

    @function_tool
    def forbidden() -> str:
        pytest.fail("A retired selector admitted a tool")

    async def late(turn, *, step_index):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            return [forbidden]

    responses.respond(assistant_output("successor"))
    async with (
        timeout(20),
        Agent(
            instructions="No stale decisions",
            tools=[forbidden],
            provider=provider(responses),
        ).session() as session,
    ):
        original = await session.submit("Wait", before_step=late)
        await entered.wait()
        # Queue before interruption starts: new root admission is fenced while
        # cancel() joins the old root's callbacks and cells.
        successor = await session.submit("Next", tools=[], if_busy="queue")
        stopping = asyncio.create_task(original.cancel())
        try:
            await cancelled.wait()
            assert not responses.requests and not stopping.done()
        finally:
            # Always let cancellation-resistant user code exit, including when
            # an assertion fails, so Session teardown cannot hide the failure.
            release.set()
        await stopping
        assert (await successor.get_result()).final_output == "successor"
        assert len(responses.requests) == 1 and not declarations(responses.requests[0])


async def test_legacy_missing_operation_subset_keeps_native_defaults(
    responses: ResponsesFixture,
) -> None:
    lookup = FunctionTool(name="lookup", description="Wait", input_type=LookupArgs, output_type=str)
    agent = Agent(instructions="Wait", tools=[lookup, Checklist()], provider=provider(responses))
    responses.respond(function_call("lookup", {"key": "A"}, "legacy-manual"))
    async with timeout(25):
        async with agent.session() as original:
            await original.submit("Wait", tools=[lookup])
            await next_request(original.interactions(), lookup)
            saved = await original.suspend()
        legacy = saved.model_copy(update={"operation_tools": None})
        responses.respond(assistant_output("legacy defaults"))
        async with agent.session(state=legacy) as restored:
            request = await next_request(restored.interactions(), lookup)
            await request.respond("accepted")
            assert (await restored.restored_turn.get_result()).final_output == "legacy defaults"
            assert {("functions", "lookup"), ("functions", "update_plan")} <= set(
                declarations(responses.requests[-1])
            )


async def test_function_enablement_rechecks_each_advertisement(responses):
    application = {"enabled": False}
    seen = []

    async def enabled(context, step):
        seen.append((context, step))
        await asyncio.sleep(0)
        return context["enabled"]

    @function_tool
    def activate() -> str:
        application["enabled"] = True
        return "enabled"

    @function_tool(is_enabled=enabled)
    def gated() -> str:
        return "gated"

    responses.respond(function_call("activate", {}, "activate"))
    responses.respond(assistant_output("done"))
    async with Agent(
        instructions="Select tools.", provider=provider(responses), tools=[activate, gated]
    ).session(context=application) as session:
        turn = await session.submit("Run")
        assert (await turn.get_result()).final_output == "done"
    assert all(context is application and step is turn for context, step in seen)
    assert len(seen) == 2
    assert [set(declarations(r)) for r in responses.requests] == [
        {("functions", "activate")},
        {("functions", "activate"), ("functions", "gated")},
    ]


async def test_function_enablement_intersects_explicit_step_selection(responses):
    @function_tool(is_enabled=False)
    def disabled() -> str:
        pytest.fail("Disabled function must not execute")

    @function_tool
    def omitted() -> str:
        pytest.fail("Omitted function must not execute")

    responses.respond(assistant_output("done"))
    async with Agent(
        instructions="Select tools.", provider=provider(responses), tools=[disabled, omitted]
    ).session() as session:
        turn = await session.submit("Run", before_step=lambda turn, step_index: [disabled.ref])
        assert (await turn.get_result()).final_output == "done"
    assert set(declarations(responses.requests[0])) == set()


async def test_function_enablement_failure_precedes_model_effects(responses):
    async def enabled(context, step):
        await asyncio.sleep(0)
        raise ValueError("Enablement rejected")

    @function_tool(is_enabled=enabled)
    def gated() -> str:
        pytest.fail("Failed predicate must not execute")

    async with Agent(
        instructions="Select tools.", provider=provider(responses), tools=[gated]
    ).session() as session:
        turn = await session.submit("Run")
        with pytest.raises(ValueError, match="Enablement rejected"):
            await turn.get_result()
    assert responses.requests == []


async def test_cancelled_function_enablement_cannot_publish_schema(responses):
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()

    async def enabled(context, step):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return True

    @function_tool(is_enabled=enabled)
    def gated() -> str:
        pytest.fail("Cancelled predicate must not enable execution")

    async with Agent(
        instructions="Select tools.", provider=provider(responses), tools=[gated]
    ).session() as session:
        turn = await session.submit("Run")
        await asyncio.wait_for(entered.wait(), 5)
        stopping = asyncio.create_task(turn.cancel())
        try:
            await asyncio.wait_for(cancelled.wait(), 5)
            assert not stopping.done()
            assert responses.requests == []
        finally:
            release.set()
        await asyncio.wait_for(stopping, 5)
    assert responses.requests == []


async def test_function_enablement_restored_manual_advertisement(responses):
    from tests.test_native_agent import function_declarations
    from tests.test_native_manual_tools import LookupArgs, next_request

    enabled = True
    effects = []
    lookup = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)

    @function_tool(is_enabled=lambda context, step: enabled)
    def metered() -> str:
        effects.append("called")
        return "done"

    agent = Agent(instructions="Read both.", tools=[lookup, metered], provider=provider(responses))
    responses.respond(
        function_call("lookup", {"key": "A"}, "manual"), function_call("metered", {}, "meter")
    )
    async with agent.session() as source:
        await source.submit("Read")
        await next_request(source.interactions(), lookup)
        saved = await source.suspend()
    enabled = False
    responses.respond(assistant_output("continued"))
    async with agent.session(state=saved) as restored:
        request = await next_request(restored.interactions(), lookup)
        await request.respond("known")
        assert (await restored.restored_turn.get_result()).final_output == "continued"
        assert effects == ["called"]
        assert "metered" not in {
            tool["name"] for tool in function_declarations(responses.requests[-1])
        }
