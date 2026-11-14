"""Configured extractors receive the whole logical child turn after cold restore."""

from __future__ import annotations

import json
from hashlib import sha256

import pytest

from agents import Agent, NativeProvider, PendingRequests, SessionState, function_tool
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, function_call
from tests.test_native_configured_agent_tool import MODEL, peer


@pytest.mark.asyncio
@pytest.mark.parametrize("restore", ["live", "trusted", "untrusted", "reseal"])
async def test_configured_extractor_retains_suspended_child_prefix(restore):
    async with timeout(20), peer() as parent_peer, peer() as child_peer:
        parent_peer.respond(function_call("delegate", {"input": "Check."}, "outer-call"))
        parent_peer.respond(assistant_output("parent done"))
        child_peer.respond(function_call("settled", {}, "settled-call"))
        child_peer.respond(function_call("pending", {}, "pending-call"))
        child_peer.respond(assistant_output("child done"))
        effects = []
        extracted = []

        @function_tool
        def settled() -> str:
            effects.append("settled")
            return "accepted automatic result"

        @function_tool(needs_approval=True)
        def pending() -> str:
            effects.append("pending")
            return "accepted manual result"

        def extract(result):
            extracted.append(result)
            return "child item summary"

        async with (
            NativeProvider(base_url=parent_peer.url, api_key="synthetic-parent") as pp,
            NativeProvider(base_url=child_peer.url, api_key="synthetic-child") as cp,
        ):
            child = Agent(
                name="Child",
                instructions="Use both tools.",
                model=MODEL,
                provider=cp,
                tools=[settled, pending],
            )
            parent = Agent(
                name="Parent",
                instructions="Delegate.",
                model=MODEL,
                provider=pp,
                tools=[
                    child.as_tool("delegate", custom_output_extractor=extract, return_items=True)
                ],
            )
            async with parent.session() as session:
                turn = await session.submit("Start.")
                boundary = await turn.wait_for_request_or_result()
                assert isinstance(boundary, PendingRequests)
                assert effects == ["settled"]
                if restore != "live":
                    wire = (await session.suspend()).to_json()
                    if restore == "untrusted":
                        raw = json.loads(wire)
                        (pending_entry,) = raw["native_continuation"]["pending_manual"]
                        child_options = pending_entry["continuation"]["state"]["agent_options"]
                        prefix = child_options["_sdk_configured_produced_prefix"]
                        prefix["items"] = "invalid caller-injected data"
                        state = SessionState.from_json(json.dumps(raw))
                    else:
                        state = SessionState.from_json(
                            wire, expected_sha256=sha256(wire.encode()).hexdigest()
                        )
                else:
                    await session.respond(boundary.requests[0].reply(True))
                    assert (await turn.get_result()).final_output == "parent done"
            if restore != "live":
                async with parent.session(state=state) as session:
                    turn = session.restored_turn
                    assert turn is not None
                    boundary = await turn.wait_for_request_or_result()
                    assert isinstance(boundary, PendingRequests)
                    if restore == "reseal":
                        wire = (await session.suspend()).to_json()
                        state = SessionState.from_json(
                            wire, expected_sha256=sha256(wire.encode()).hexdigest()
                        )
                    else:
                        await session.respond(boundary.requests[0].reply(True))
                        assert (await turn.get_result()).final_output == "parent done"
                if restore == "reseal":
                    async with parent.session(state=state) as session:
                        turn = session.restored_turn
                        assert turn is not None
                        boundary = await turn.wait_for_request_or_result()
                        assert isinstance(boundary, PendingRequests)
                        await session.respond(boundary.requests[0].reply(True))
                        assert (await turn.get_result()).final_output == "parent done"

    assert effects == ["settled", "pending"]
    assert len(parent_peer.requests) == 2 and len(child_peer.requests) == 3
    [result] = extracted
    assert result.items is not None
    assert all(entry.agent is child for entry in result.items)
    accepted = [
        ("function_call", "settled-call"),
        ("function_call_output", "settled-call"),
        ("function_call", "pending-call"),
        ("function_call_output", "pending-call"),
        ("message", None),
    ]
    if restore == "untrusted":
        accepted = accepted[-2:]
    assert [(entry.item["type"], entry.item.get("call_id")) for entry in result.items] == accepted
    for entry in result.items:
        if entry.item["type"] == "function_call_output":
            assert entry.item["error"] is None
