"""Native profile transitions keep one history and exact source declarations."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import pytest

from agents import Agent, FunctionTool, NativeProvider, ToolContext, ToolRef, function_tool
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import function_declarations
from tests.test_native_manual_tools import LookupArgs, next_request


def provider(responses: ResponsesFixture) -> NativeProvider:
    return NativeProvider(base_url=responses.url, api_key="synthetic-handoff-key")


async def test_handoff_uses_destination_callback_and_retains_agent_context_and_history(responses):
    calls = []
    context = {"tenant": "one"}

    @function_tool(name="lookup")
    def lookup_a(ctx: ToolContext[dict[str, str]], key: str) -> str:
        calls.append(("A", ctx.context, key))
        return "A result"

    @function_tool(name="lookup")
    def lookup_b(ctx: ToolContext[dict[str, str]], key: str) -> str:
        calls.append(("B", ctx.context, key))
        return "B result"

    target = Agent(instructions="You are B.", name="B", tools=[lookup_b])
    source = Agent(
        instructions="You are A.",
        name="A",
        tools=[lookup_a],
        handoffs=[target],
        provider=provider(responses),
    )
    responses.respond(function_call("transfer_to_b", {}, "handoff-B"))
    responses.respond(function_call("lookup", {"key": "record"}, "lookup-B"))
    responses.respond(assistant_output("B accepted"))
    responses.respond(assistant_output("B remembers"))
    async with timeout(20), source.session(context=context) as session:
        first = await session.submit("Route to B and read the record")
        assert (await first.get_result()).final_output == "B accepted"
        assert await session.get_active_agent() is target
        assert calls == [("B", context, "record")]
        assert calls[0][1] is context
        assert (await (await session.submit("Continue")).get_result()).final_output == "B remembers"
        assert await session.get_active_agent() is target
        assert tool_output(responses.requests[2], "lookup-B") == "B result"
        assert any(item.get("call_id") == "handoff-B" for item in responses.requests[3]["input"])
        assert [tool["name"] for tool in function_declarations(responses.requests[3])] == ["lookup"]


async def test_idle_activation_reapplies_declared_defaults_and_keeps_same_history(responses):
    target = Agent(instructions="Declared B.", name="B")
    source = Agent(
        instructions="Declared A.", name="A", handoffs=[target], provider=provider(responses)
    )
    responses.respond(assistant_output("A first"))
    responses.respond(assistant_output("A again"))
    async with timeout(20), source.session() as session:
        await (await session.submit("Remember this")).get_result()
        await session.update(instructions="Temporary A.")
        await session.update(agent=target)
        assert await session.get_active_agent() is target
        await session.update(agent=source)
        assert await session.get_active_agent() is source
        assert (await (await session.submit("Continue")).get_result()).final_output == "A again"
        encoded = json.dumps(responses.requests[1]["input"])
        assert "Declared A." in encoded
        assert "A first" in encoded


def manual_graph(responses: ResponsesFixture, *, extra_calls: list[str] | None = None):
    left = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    right = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)

    @function_tool
    def extra() -> str:
        assert extra_calls is not None
        extra_calls.append("extra")
        return "extra result"

    target = Agent(
        instructions="B owns lookup.",
        name="B",
        tools=[extra, right] if extra_calls is not None else [right],
    )
    source = Agent(
        instructions="A owns lookup.",
        name="A",
        tools=[left],
        handoffs=[target],
        provider=provider(responses),
    )
    return source, target, left, right


@pytest.mark.parametrize("intermediate_graph", ["original", "repeated", "wrong"])
async def test_pending_b_manual_restore_rebinds_only_fresh_b_owner(responses, intermediate_graph):
    source, target, left, right = manual_graph(responses)
    responses.respond(function_call("transfer_to_b", {}, "handoff-B"))
    responses.respond(function_call("lookup", {"key": "B"}, "pending-B"))
    async with timeout(20), source.session() as session:
        await session.submit("Route and read")
        request = await next_request(session.interactions(), right)
        assert left.match(request) is None
        saved = await session.suspend()
    fresh_source, fresh_target, fresh_left, fresh_right = manual_graph(responses)
    if intermediate_graph != "original":
        raw = json.loads(saved.to_json())
        first_b = next(
            index
            for index, item in enumerate(raw["history"])
            if item.get("metadata", {}).get("sdk_agent_profile", {}).get("profile_id") == "agent_1"
        )
        accepted_b = deepcopy(raw["history"][first_b])
        # Repeating a profile requires a new epoch and accepted instruction revision.
        accepted_b["metadata"]["sdk_agent_profile"]["epoch"] += 1
        repeated_message = accepted_b["payload"]
        original_id = repeated_message["id"]
        repeated_message["id"] = f"{original_id}_repeated"
        original_revision = f"Native instruction revision: {original_id}\n"
        assert original_revision in repeated_message["content"][0]["text"]
        repeated_message["content"][0]["text"] = repeated_message["content"][0]["text"].replace(
            original_revision, f"Native instruction revision: {repeated_message['id']}\n", 1
        )
        if intermediate_graph == "wrong":
            raw["history"][first_b]["metadata"]["sdk_agent_graph"] = "different-graph"
        # Keep the latest/current source valid: only the transition immediately
        # after the handoff has the forged graph. The matching repeated receipt
        # is a positive control for the otherwise identical restored history.
        raw["history"].insert(first_b + 1, accepted_b)
        saved = SessionState.from_json(json.dumps(raw))
        if intermediate_graph == "wrong":
            with pytest.raises(
                RuntimeError, match="Handoff does not match its next accepted Agent transition"
            ):
                async with fresh_source.session(state=saved):
                    pytest.fail("Restore accepted an intermediate Agent from another graph")
            assert len(responses.requests) == 2
            return
    responses.respond(assistant_output("B resumed"))
    async with (
        timeout(20),
        fresh_source.session(state=SessionState.from_json(saved.to_json())) as restored,
    ):
        assert await restored.get_active_agent() is fresh_target
        request = await next_request(restored.interactions(), fresh_right)
        assert fresh_left.match(request) is None
        assert request.call_id == "pending-B"
        with pytest.raises((ValueError, RuntimeError)):
            await restored.update(agent=fresh_source)
        await request.respond("fresh B reply")
        assert restored.restored_turn is not None
        assert (await restored.restored_turn.get_result()).final_output == "B resumed"
        assert tool_output(responses.requests[-1], "pending-B") == "fresh B reply"
        assert len(responses.requests) == 3


async def test_restore_preserves_b_subset_slots_then_reactivation_uses_full_b_template(responses):
    original_calls: list[str] = []
    source, target, left, right = manual_graph(responses, extra_calls=original_calls)
    responses.respond(function_call("lookup", {"key": "B"}, "pending-subset-B"))
    async with timeout(20), source.session() as session:
        await session.update(agent=target)
        await session.update(tools=[right])
        await session.update(instructions="Temporary accepted B instructions.")
        await session.submit("Read with the B subset")
        request = await next_request(session.interactions(), right)
        assert left.match(request) is None
        saved = await session.suspend()
    fresh_calls: list[str] = []
    fresh_source, fresh_target, fresh_left, fresh_right = manual_graph(
        responses, extra_calls=fresh_calls
    )
    responses.respond(assistant_output("B restored"))
    responses.respond(function_call("extra", {}, "fresh-extra-B"))
    responses.respond(assistant_output("B full declaration"))
    async with timeout(20), fresh_source.session(state=saved) as restored:
        request = await next_request(restored.interactions(), fresh_right)
        assert fresh_left.match(request) is None
        await request.respond("fresh B subset reply")
        assert restored.restored_turn is not None
        assert (await restored.restored_turn.get_result()).final_output == "B restored"
        assert tool_output(responses.requests[1], "pending-subset-B") == "fresh B subset reply"
        assert "Temporary accepted B instructions." in json.dumps(responses.requests[1]["input"])
        assert {tool["name"] for tool in function_declarations(responses.requests[1])} == {"lookup"}
        await restored.update(agent=fresh_source)
        await restored.update(agent=fresh_target)
        assert (
            await (await restored.submit("Use all declared B tools")).get_result()
        ).final_output == "B full declaration"
        assert {tool["name"] for tool in function_declarations(responses.requests[2])} == {
            "extra",
            "lookup",
        }
        assert tool_output(responses.requests[3], "fresh-extra-B") == "extra result"
        assert fresh_calls == ["extra"]
        assert original_calls == []


async def test_queued_a_ceiling_survives_b_admission_and_b_manual_restore(responses):
    def graph(entered: asyncio.Event, release: asyncio.Event):
        common = FunctionTool(
            name="common", description="Shared read", input_type=LookupArgs, output_type=str
        )

        @function_tool(name="private")
        def private_a() -> str:
            pytest.fail("The original private A callback must not run")

        @function_tool(name="private")
        def private_b() -> str:
            pytest.fail("The same-name B callback is outside the queued A ceiling")

        @function_tool
        async def hold_a() -> str:
            entered.set()
            await release.wait()
            return "A released"

        target = Agent(
            instructions="B owns this conversation.", name="B", tools=[private_b, common]
        )
        source = Agent(
            instructions="A can delegate to B.",
            name="A",
            tools=[hold_a, private_a, common],
            handoffs=[target],
            provider=provider(responses),
        )
        return source, target, common, private_a

    entered, release = asyncio.Event(), asyncio.Event()
    source, target, common, private_a = graph(entered, release)
    responses.respond(function_call("hold_a", {}, "hold-A"))
    responses.respond(function_call("transfer_to_b", {}, "handoff-before-queue"))
    responses.respond(assistant_output("B first turn"))
    responses.respond(function_call("common", {"key": "B"}, "pending-queued-B"))
    async with timeout(20), source.session() as session:
        first = await session.submit("Hold A before routing")
        await entered.wait()
        queued = await session.submit("Read later", if_busy="queue", tools=[common, private_a])
        assert queued.turn_id is None
        release.set()
        assert (await first.get_result()).final_output == "B first turn"
        assert await session.get_active_agent() is target
        request = await next_request(session.interactions(), common)
        assert request.call_id == "pending-queued-B"
        saved = await session.suspend()
    fresh_source, fresh_target, fresh_common, _ = graph(asyncio.Event(), asyncio.Event())
    responses.respond(assistant_output("B restored under A ceiling"))
    async with timeout(20), fresh_source.session(state=saved) as restored:
        assert await restored.get_active_agent() is fresh_target
        request = await next_request(restored.interactions(), fresh_common)
        await request.respond("shared B reply")
        assert restored.restored_turn is not None
        assert (
            await restored.restored_turn.get_result()
        ).final_output == "B restored under A ceiling"
        assert {tool["name"] for tool in function_declarations(responses.requests[-1])} == {
            "common"
        }
        assert tool_output(responses.requests[-1], "pending-queued-B") == "shared B reply"


@pytest.mark.parametrize("manual_first", [False, True])
async def test_manual_sibling_delays_handoff_until_settled_and_resumes_once(
    responses, manual_first
):
    source, target, left, right = manual_graph(responses)
    calls = [
        function_call("transfer_to_b", {}, "handoff-B"),
        function_call("lookup", {"key": "A"}, "pending-A"),
    ]
    responses.respond(*(reversed(calls) if manual_first else calls))
    async with timeout(20), source.session() as session:
        await session.submit("Read A and route to B")
        request = await next_request(session.interactions(), left)
        assert await session.get_active_agent() is source
        saved = await session.suspend()
    fresh_source, fresh_target, fresh_left, fresh_right = manual_graph(responses)
    responses.respond(assistant_output("B after the batch"))
    async with timeout(20), fresh_source.session(state=saved) as restored:
        request = await next_request(restored.interactions(), fresh_left)
        assert fresh_right.match(request) is None
        assert await restored.get_active_agent() is fresh_source
        await request.respond("A reply")
        assert restored.restored_turn is not None
        assert (await restored.restored_turn.get_result()).final_output == "B after the batch"
        assert await restored.get_active_agent() is fresh_target
        assert len(responses.requests) == 2


async def test_changed_graph_rejects_before_pending_callback_replay(responses):
    source, target, left, right = manual_graph(responses)
    responses.respond(function_call("transfer_to_b", {}, "handoff-B"))
    responses.respond(function_call("lookup", {"key": "B"}, "pending-B"))
    async with timeout(20), source.session() as session:
        await session.submit("Route and read")
        await next_request(session.interactions(), right)
        saved = await session.suspend()
    changed = Agent(instructions="A different B.", name="B", tools=[right])
    invalid = Agent(
        instructions=source.instructions,
        name="A",
        tools=[left],
        handoffs=[changed],
        provider=source.provider,
    )
    with pytest.raises((ValueError, RuntimeError), match="graph|declaration|profile"):
        async with invalid.session(state=saved):
            pytest.fail("A different graph replayed the manual continuation")
    assert len(responses.requests) == 2


@pytest.mark.parametrize("initial_policy", [False, True])
async def test_mcp_result_policy_uses_actual_source_after_handoff(responses, initial_policy):
    from agents import HttpMCP
    from tests.test_native_mcp_tools import Peer, mcp_call

    first = Peer(names=("lookup",), label="raw-A")
    second = Peer(names=("lookup",), label="raw-B")
    policies = []
    context = {"tenant": "shared"}

    async def accept_a(ctx, value):
        policies.append(("A", ctx.context))
        return {"content": [{"type": "text", "text": "accepted-A"}], "isError": False}

    async def accept_b(ctx, value):
        policies.append(("B", ctx.context))
        return {"content": [{"type": "text", "text": "accepted-B"}], "isError": False}

    responses.respond(mcp_call("lookup", "mcp__records", "mcp-A"))
    responses.respond(function_call("transfer_to_b", {}, "handoff-B"))
    responses.respond(mcp_call("lookup", "mcp__records", "mcp-B"))
    responses.respond(assistant_output("Both accepted"))
    async with first.serve() as first_url, second.serve() as second_url:
        target = Agent[dict[str, str], str](
            instructions="B uses records.",
            name="B",
            tools=[
                HttpMCP(name="records", url=second_url, tools=["lookup"], transform_result=accept_b)
            ],
        )
        source = Agent[dict[str, str], str](
            instructions="A uses records.",
            name="A",
            handoffs=[target],
            model="gpt-5.5",
            provider=provider(responses),
            tools=[
                HttpMCP(
                    name="records",
                    url=first_url,
                    tools=["lookup"],
                    transform_result=accept_a if initial_policy else None,
                )
            ],
        )
        async with timeout(20), source.session(context=context) as session:
            assert (
                await (await session.submit("Read both")).get_result()
            ).final_output == "Both accepted"
            assert await session.get_active_agent() is target
    assert [item[0] for item in policies] == (["A", "B"] if initial_policy else ["B"])
    assert all(item[1] is context for item in policies)
    assert "accepted-B" in json.dumps(responses.requests[3])
    assert "raw-B" not in json.dumps(responses.requests[3])


async def test_restored_output_override_does_not_replace_graph_or_future_default(responses):
    source, target, left, right = manual_graph(responses)
    responses.respond(function_call("transfer_to_b", {}, "handoff-B"))
    responses.respond(function_call("lookup", {"key": "B"}, "pending-B"))
    async with timeout(20), source.session() as session:
        await session.submit("Route and read", output_type=int)
        await next_request(session.interactions(), right)
        saved = await session.suspend()
    with pytest.raises(ValueError, match="restored_output_type"):
        async with source.session(state=saved):
            pytest.fail("An override was restored with the default contract")
    with pytest.raises(ValueError, match="output contract|schema"):
        async with source.session(state=saved, restored_output_type=str):
            pytest.fail("An incompatible override reached replay")
    responses.respond(assistant_output('{"response":7}'))
    responses.respond(assistant_output("Default B text"))
    async with timeout(20), source.session(state=saved, restored_output_type=int) as restored:
        request = await next_request(restored.interactions(), right)
        await request.respond("B value")
        assert restored.restored_turn is not None
        assert (await restored.restored_turn.get_result()).final_output == 7
        assert await restored.get_active_agent() is target
        assert (
            await (await restored.submit("Continue")).get_result()
        ).final_output == "Default B text"
        assert "_sdk_output_override" not in (await restored.save_state()).agent_options


async def test_graph_replaced_function_rejects_stale_object_but_preserves_name_selection(responses):
    calls: list[str] = []

    @function_tool(name="lookup")
    def original() -> str:
        calls.append("original")
        return "original result"

    @function_tool(name="lookup")
    def replacement() -> str:
        calls.append("replacement")
        return "replacement result"

    target = Agent(instructions="B", name="B")
    source = Agent(
        instructions="A",
        name="A",
        tools=[original],
        handoffs=[target],
        provider=provider(responses),
    )
    async with timeout(20), source.session() as session:
        await session.update(tools=[replacement])
        with pytest.raises(ValueError, match="current captured FunctionTool"):
            await session.submit("Must reject before model work", tools=[original])
        assert responses.requests == []
        assert calls == []
        responses.respond(function_call("lookup", {}, "replacement-call"))
        responses.respond(assistant_output("Accepted replacement"))
        turn = await session.submit("Use the current named tool", tools=[ToolRef("lookup")])
        assert (await turn.get_result()).final_output == "Accepted replacement"
        assert calls == ["replacement"]
        assert tool_output(responses.requests[-1], "replacement-call") == "replacement result"


async def test_omitted_operation_tools_inherits_a_subset_then_b_declared_defaults(responses):
    calls: list[str] = []

    @function_tool
    def allowed_a() -> str:
        calls.append("allowed A")
        return "A"

    @function_tool
    def excluded_a() -> str:
        pytest.fail("Idle A defaults excluded this function")

    @function_tool
    def distinct_b() -> str:
        calls.append("B")
        return "B tool accepted"

    target = Agent(instructions="B", name="B", tools=[distinct_b])
    source = Agent(
        instructions="A",
        name="A",
        tools=[allowed_a, excluded_a],
        handoffs=[target],
        provider=provider(responses),
    )
    responses.respond(function_call("transfer_to_b", {}, "subset-handoff"))
    responses.respond(function_call("distinct_b", {}, "distinct-B"))
    responses.respond(assistant_output("B accepted"))
    async with timeout(20), source.session() as session:
        await session.update(tools=[allowed_a, ToolRef("transfer_to_b")])
        turn = await session.submit("Continue with B")
        assert (await turn.get_result()).final_output == "B accepted"
        assert calls == ["B"]
        initial = {tool["name"] for tool in function_declarations(responses.requests[0])}
        assert "allowed_a" in initial
        assert "transfer_to_b" in initial
        assert "excluded_a" not in initial
        assert "distinct_b" in {
            tool["name"] for tool in function_declarations(responses.requests[1])
        }
        assert tool_output(responses.requests[-1], "distinct-B") == "B tool accepted"


@pytest.mark.parametrize("activation", ["update", "handoff"])
@pytest.mark.parametrize(
    ("source_deferred", "target_deferred"),
    [(None, False), (None, True), (False, True), (True, False)],
)
async def test_graph_mcp_exposure_tracks_active_source(
    responses, activation, source_deferred, target_deferred
):
    from agents import HttpMCP
    from tests.test_native_mcp_tools import Peer, declarations, mcp_call
    from tests.test_native_schema_discovery import discovered, search

    source_peer = Peer(names=("lookup",), label="source-record")
    target_peer = Peer(names=("lookup",), label="target-record")
    async with timeout(25), source_peer.serve() as source_url, target_peer.serve() as target_url:
        target = Agent(
            name="B",
            instructions="Use target records.",
            tools=[
                HttpMCP(
                    name="records",
                    url=target_url,
                    tools=["lookup"],
                    defer_loading=target_deferred,
                )
            ],
        )
        source = Agent(
            name="A",
            instructions="Use source records.",
            handoffs=[target],
            tools=[]
            if source_deferred is None
            else [
                HttpMCP(
                    name="records",
                    url=source_url,
                    tools=["lookup"],
                    defer_loading=source_deferred,
                )
            ],
            model="gpt-5.5",
            provider=provider(responses),
        )
        if activation == "handoff":
            responses.respond(function_call("transfer_to_b", {}, "transfer-B"))
        if target_deferred:
            responses.respond(search("synthetic record lookup", "target-search"))
        responses.respond(mcp_call("lookup", "mcp__records", "target-call"))
        responses.respond(assistant_output("target done"))
        async with source.session() as session:
            if activation == "update":
                await session.update(agent=target)
            assert (
                await (await session.submit("Read target")).get_result()
            ).final_output == "target done"
            assert await session.get_active_agent() is target
            request = responses.requests[activation == "handoff"]
            assert (("mcp__records", "lookup") in declarations(request)) is not target_deferred
            assert (
                any(tool["type"] == "tool_search" for tool in request["tools"]) is target_deferred
            )
            if target_deferred:
                assert "lookup" in json.dumps(discovered(responses, "target-search"))
            assert len(target_peer.calls) == 1
            assert not source_peer.calls

            await session.update(agent=source)
            responses.respond(assistant_output("source revisited"))
            assert (
                await (await session.submit("Return to source")).get_result()
            ).final_output == "source revisited"
            request = responses.requests[-1]
            assert (("mcp__records", "lookup") in declarations(request)) is (
                source_deferred is False
            )
            assert any(tool["type"] == "tool_search" for tool in request["tools"]) is (
                source_deferred is True
            )
            assert not source_peer.calls
            assert len(target_peer.calls) == 1


@pytest.mark.parametrize("opening_manual", [False, True])
async def test_live_handoff_manual_reply_uses_issuing_declaration(responses, opening_manual):
    source, target, left, right = manual_graph(responses)
    if not opening_manual:
        source = Agent(
            instructions=source.instructions,
            name=source.name,
            tools=[],
            handoffs=[target],
            provider=source.provider,
        )
    responses.respond(function_call("transfer_to_b", {}, "handoff-B"))
    responses.respond(function_call("lookup", {"key": "B"}, "manual-B"))
    responses.respond(assistant_output("B accepted"))
    async with timeout(20), source.session() as session:
        turn = await session.submit("Route to B and read")
        request = await next_request(session.interactions(), right)
        assert left.match(request) is None
        # Reply before any API that refreshes the active profile bindings.
        await request.respond("B reply")
        assert (await turn.get_result()).final_output == "B accepted"
        assert tool_output(responses.requests[-1], "manual-B") == "B reply"


async def test_run_captures_and_resumes_destination_only_manual_tool(responses):
    from agents import InteractionRequired

    _, target, _, right = manual_graph(responses)
    source = Agent(
        name="A",
        instructions="Route to B.",
        handoffs=[target],
        provider=provider(responses),
    )
    responses.respond(function_call("transfer_to_b", {}, "handoff-B"))
    responses.respond(function_call("lookup", {"key": "B"}, "manual-B"))
    async with timeout(20):
        with pytest.raises(InteractionRequired) as captured:
            await source.run("Route and read")
        saved = captured.value.state
        assert saved is not None
        assert saved.native_continuation is not None
        assert saved.native_continuation.pending_manual[0].tool == "lookup"
        responses.respond(assistant_output("B resumed"))

        def reply(request):
            assert right.match(request) is not None
            return request.reply("B reply")

        completed = await source.run(None, state=saved, on_interaction=reply)
        assert completed.final_output == "B resumed"
        assert tool_output(responses.requests[-1], "manual-B") == "B reply"


async def test_deferred_preparation_uses_handoff_issuing_catalog(responses):
    from tests.test_deferred_jobs import Input, Journal, Output, Provider, declared

    journal, backend = Journal(), Provider([])
    jobs, right = declared(journal, backend)
    left = FunctionTool(name="render", description="Render A", input_type=Input, output_type=Output)
    target = Agent(name="B", instructions="Render B.", tools=[right])
    source = Agent(
        name="A",
        instructions="Route to B.",
        tools=[left],
        handoffs=[target],
        provider=provider(responses),
    )
    responses.respond(function_call("transfer_to_b", {}, "handoff-B"))
    responses.respond(function_call("render", {"pages": 2}, "manual-B"))
    async with timeout(20), source.session() as session:
        await session.submit("Route and render")
        request = await next_request(session.interactions(), right)
        saved = await jobs.prepare(session, request)
        assert saved.native_continuation is not None
        assert journal.log == ["prepare"]
        assert backend.calls == []
