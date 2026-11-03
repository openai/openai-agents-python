"""Dormant restored HTTP rosters use only new opening authority and native receipts."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from dataclasses import replace
from typing import Any

import pytest

from agents import (
    Agent,
    FunctionTool,
    HttpMCP,
    InteractionRequired,
    NativeError,
    PromptedOutput,
    ToolRef,
    TurnStoppedError,
)
from agents.mcp import MCPResultContext
from agents.state import SessionState
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_manual_tools import LookupArgs, next_request, provider
from tests.test_native_mcp_tools import Peer, declarations, mcp_call


@pytest.mark.parametrize("checkpoint", ["idle", "plain", "prompted"])
async def test_http_restore_reserves_destination_until_selected(
    responses: ResponsesFixture, checkpoint: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agents.session as session_module

    source, destination, foreign = Peer(label="completed-source"), Peer(label="destination"), Peer()
    manual = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    policy_calls: list[tuple[str, str]] = []

    def source_policy(ctx: MCPResultContext[list[str]], value: dict[str, Any]) -> dict[str, Any]:
        policy_calls.append(("source", ctx.context[0]))
        return value

    def destination_policy(
        ctx: MCPResultContext[list[str]], value: dict[str, Any]
    ) -> dict[str, Any]:
        policy_calls.append(("destination", ctx.context[0]))
        return value

    prompted = checkpoint == "prompted"
    agent = Agent(
        instructions="Use the tools.",
        output_type=PromptedOutput(LookupArgs) if prompted else str,
        provider=provider(responses),
        model="gpt-5.5",
    )
    # Observe real receipt bytes before the normal validator runs, never supply a fake success.
    receipts: list[dict] = []
    check = session_module._check_mcp_receipt

    def record(raw, servers, available, generation):
        receipts.append(json.loads(raw))
        return check(raw, servers, available, generation)

    monkeypatch.setattr(session_module, "_check_mcp_receipt", record)
    async with (
        timeout(35),
        source.serve() as old_url,
        destination.serve() as new_url,
        foreign.serve() as other_url,
    ):
        old = HttpMCP(
            name="docs",
            url=old_url,
            tools=["search"],
            bearer_token="synthetic-source",
            transform_result=source_policy,
        )
        new = HttpMCP(
            name="docs",
            url=new_url,
            tools=["search"],
            bearer_token="synthetic-destination",
            transform_result=destination_policy,
        )
        responses.respond(mcp_call("search", "mcp__docs", "done"))
        responses.respond(assistant_output('{"key":"saved"}' if prompted else "saved"))
        async with agent.session(tools=[manual, old], context=["old opening"]) as original:
            await (await original.submit("read", tools=[old])).get_result()
            if checkpoint == "idle":
                responses.respond(assistant_output("saved"))
            else:
                responses.respond(function_call("lookup", {"key": "A"}, "manual"))
            turn = await original.submit("park", tools=[manual])
            if checkpoint == "idle":
                assert (await turn.get_result()).final_output == "saved"
                saved = await original.save_state()
                request = None
            else:
                request = await next_request(original.interactions(), manual)
                assert original._active is not None
                assert await original._wait_manual_boundary(original._active, require_request=True)
                saved = await original.suspend()
                with pytest.raises((RuntimeError, ValueError)):
                    await request.respond("old grant")
                with pytest.raises(InteractionRequired):
                    await turn.get_result()
        assert len(source.calls) == 1
        assert policy_calls == [("source", "old opening")]
        assert set(source.auth) == {"Bearer synthetic-source"}
        old_count = len(source.requests)
        # Serialized launch details must not replace the new opening's credentials or roster.
        raw = json.loads(saved.to_json())
        raw["agent_options"] = {"config": {"mcp_servers": {"docs": old._config()}}}
        saved = SessionState.from_json(json.dumps(raw))
        roster = [manual, new]
        previous = len(responses.requests)
        async with agent.session(tools=roster, state=saved, context=["new opening"]) as restored:
            disabled = receipts[-1]["result"]
            assert disabled["ready"] is True
            assert disabled["policy_generation"] == 0
            assert disabled["servers"]["docs"]["status"] == "disabled"
            assert disabled["servers"]["docs"]["tools"] == []
            assert destination.requests == [] and foreign.requests == []
            assert policy_calls == [("source", "old opening")]
            assert len(source.requests) == old_count
            if checkpoint != "idle":
                fresh = await next_request(restored.interactions(), manual)
                assert fresh.call_id == "manual" and len(responses.requests) == previous
                assert request is not None
                with pytest.raises((RuntimeError, ValueError)):
                    await request.respond("still old")
                final = '{"key":"restored"}' if prompted else "restored"
                responses.respond(assistant_output(final))
                await fresh.respond("current reply")
                result = await restored.restored_turn.get_result()
                assert (result.final_output.key if prompted else result.final_output) == "restored"
                assert tool_output(responses.requests[-1], "manual") == "current reply"
                assert "completed-source" in json.dumps(responses.requests[-1]["input"])
                assert destination.requests == []
            with pytest.raises(ValueError, match="captured|allowed"):
                await restored.submit("invalid", tools=[ToolRef("excluded", "mcp__docs")])
            with pytest.raises(ValueError, match="captured"):
                await restored.submit("invalid", tools=[manual, replace(new, url=other_url)])
            assert destination.requests == [] and foreign.requests == []
            responses.respond(mcp_call("search", "mcp__docs", "current"))
            responses.respond(assistant_output('{"key":"selected"}' if prompted else "selected"))
            result = await (
                await restored.submit("selected", tools=[manual, new.tool("search")])
            ).get_result()
            assert receipts[-1]["result"]["servers"]["docs"]["status"] == "connected"
            assert (result.final_output.key if prompted else result.final_output) == "selected"
            assert len(destination.calls) == 1
            assert policy_calls == [("source", "old opening"), ("destination", "new opening")]
            assert set(destination.auth) == {"Bearer synthetic-destination"}
            assert len(source.requests) == old_count and len(source.calls) == 1
            assert ("mcp__docs", "search") in declarations(responses.requests[-2])
            destination_count = len(destination.requests)
            responses.respond(mcp_call("search", "mcp__docs", "after-disable"))
            responses.respond(assistant_output('{"key":"disabled"}' if prompted else "disabled"))
            await (await restored.submit("no current MCP", tools=[manual])).get_result()
            assert ("mcp__docs", "search") not in declarations(responses.requests[-2])
            assert len(destination.requests) == destination_count and len(destination.calls) == 1
            assert len(source.requests) == old_count and foreign.requests == []
        assert agent.tools == ()


@pytest.mark.parametrize("fault", ["previous_connected", "wrong_generation"])
async def test_restore_rejects_unconfirmed_disabled_receipt_before_manual_grants(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    import agents.session as session_module

    source, destination = Peer(), Peer()
    manual = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    agent = Agent(instructions="Read.", provider=provider(responses))
    original_check = session_module._check_mcp_receipt
    source_receipts: list[str] = []

    def record(raw, servers, available, generation):
        source_receipts.append(raw)
        return original_check(raw, servers, available, generation)

    monkeypatch.setattr(session_module, "_check_mcp_receipt", record)
    async with timeout(25), source.serve() as old_url, destination.serve() as new_url:
        first = HttpMCP(name="docs", url=old_url, tools=["search"])
        current = HttpMCP(name="docs", url=new_url, tools=["search"])
        responses.respond(function_call("lookup", {"key": "A"}, "pending"))
        async with agent.session(tools=[manual, first]) as session:
            await session.submit("park", tools=[manual])
            old_grant = await next_request(session.interactions(), manual)
            assert session._active is not None
            assert await session._wait_manual_boundary(session._active, require_request=True)
            state = await session.suspend()
        disabled = json.loads(source_receipts[-1])
        disabled["result"]["servers"]["docs"]["status"] = "connected"
        disabled["result"]["servers"]["docs"]["tools"] = ["search"]
        previous = json.dumps(disabled)
        old_count = len(source.requests)
        roster = [manual, current]
        seen: list[dict] = []

        def invalidate(raw, servers, available, generation):
            receipt = json.loads(raw)
            seen.append(receipt)
            assert receipt["result"]["servers"]["docs"]["status"] == "disabled"
            if fault == "previous_connected":
                candidate = previous
            else:
                receipt["result"]["policy_generation"] = generation + 1
                candidate = json.dumps(receipt)
            return original_check(candidate, servers, available, generation)

        monkeypatch.setattr(session_module, "_check_mcp_receipt", invalidate)
        with pytest.raises(NativeError, match="MCP publication"):
            async with agent.session(state=state, tools=roster):
                pytest.fail("unconfirmed restore issued fresh grants")
        assert len(seen) == 1
        assert destination.requests == [] and len(source.requests) == old_count
        assert len(responses.requests) == 1
        with pytest.raises((RuntimeError, ValueError)):
            await old_grant.respond("stale")
        # The failed import is retired; a new valid owner can take the same checkpoint.
        monkeypatch.setattr(session_module, "_check_mcp_receipt", original_check)
        async with agent.session(state=state, tools=roster) as restored:
            fresh = await next_request(restored.interactions(), manual)
            responses.respond(assistant_output("recovered"))
            await fresh.respond("current")
            assert (await restored.restored_turn.get_result()).final_output == "recovered"
        assert len(source.requests) == old_count and not destination.requests


@pytest.mark.parametrize("fault", ["denied", "cancelled_confirmed", "cancelled_bad_receipt"])
async def test_restored_http_activation_failure_fences_and_joins(
    responses: ResponsesFixture, fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agents.session as session_module

    gate = None if fault == "denied" else asyncio.Event()
    destination = Peer(denied=fault == "denied", release_ready=gate)
    source = Peer()
    agent = Agent(instructions="Read.", provider=provider(responses))
    async with timeout(25), source.serve() as old_url, destination.serve() as new_url:
        first = HttpMCP(name="docs", url=old_url, tools=["search"])
        current = HttpMCP(name="docs", url=new_url, tools=["search"], startup_timeout=3)
        async with agent.session(tools=[first]) as original:
            saved = await original.save_state()
        roster = [current]
        old_count = len(source.requests)
        # Exact cancellation retains a failed interruption for Session cleanup.
        closing = (
            pytest.raises(NativeError, match="MCP publication generation")
            if fault == "cancelled_bad_receipt"
            else nullcontext()
        )
        with closing:
            async with agent.session(state=saved, tools=roster) as restored:
                assert destination.requests == []
                original_check = session_module._check_mcp_receipt

                def check(raw, servers, available, generation):
                    if fault == "cancelled_bad_receipt":
                        receipt = json.loads(raw)
                        receipt["result"]["policy_generation"] = generation + 1
                        raw = json.dumps(receipt)
                    return original_check(raw, servers, available, generation)

                monkeypatch.setattr(session_module, "_check_mcp_receipt", check)
                if gate is None:
                    with pytest.raises(NativeError, match="connection not ready"):
                        issued = await restored.submit("activate", tools=[current])
                        await issued.get_result()
                else:
                    issued = await restored.submit("activate", tools=[current])
                    await destination.ready_entered.wait()
                    operation = asyncio.create_task(issued.cancel())
                    await asyncio.sleep(0)
                    assert not operation.done()
                    gate.set()
                    if fault == "cancelled_bad_receipt":
                        with pytest.raises(NativeError, match="MCP publication generation"):
                            await operation
                    else:
                        await operation
                        with pytest.raises(TurnStoppedError):
                            await issued.get_result()
                assert destination.calls == [] and len(source.requests) == old_count
                assert responses.requests == []
                if fault == "cancelled_confirmed":
                    # Known publication is retained. Cancellation scopes only
                    # the acknowledged Turn and does not retire the Session.
                    responses.respond(mcp_call("search", "mcp__docs", "current-after-cancel"))
                    responses.respond(assistant_output("still usable"))
                    later = await restored.submit("read", tools=[current])
                    assert (await later.get_result()).final_output == "still usable"
                    assert len(destination.calls) == 1
                    assert len(source.requests) == old_count
                else:
                    with pytest.raises(RuntimeError, match="unconfirmed|closed"):
                        await restored.submit("must not run")
                    assert not destination.calls and not responses.requests
