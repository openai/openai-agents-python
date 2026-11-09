"""Accepted MCP replacements publish their own captured callback policy."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agents import Agent, Allow, HttpMCP, NativeError, ReplaceArguments, StopTurn, TurnStoppedError
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, assistant_output, responses as responses
from tests.test_native_agent import provider
from tests.test_native_mcp_tools import Peer, mcp_call


@pytest.mark.parametrize("activated_leaf", [False, True], ids=["root", "leaf"])
@pytest.mark.parametrize("policy", ["before", "approval"])
async def test_added_mcp_callback_blocks_same_ref_replacement(
    responses: ResponsesFixture, activated_leaf: bool, policy: str
) -> None:
    peer = Peer(names=("search",))
    observed: list[str] = []

    def before(context, call):
        assert context.submission is turn
        observed.append("before")
        return StopTurn("synthetic-replacement-stop")

    def approval(context, call):
        assert context.submission is turn
        observed.append("approval")
        return False

    responses.respond(mcp_call("search", "mcp__records", "replaced-call"))
    responses.respond(assistant_output("Session remains reusable."))
    async with timeout(20), peer.serve() as url:
        opening = HttpMCP(name="records", url=url, tools=["search"], approval_mode="preapproved")
        leaf = Agent(name="Leaf", instructions="Use records.", tools=[opening])
        root = Agent(
            name="Root",
            instructions="Use records.",
            provider=provider(responses),
            tools=[opening],
            handoffs=[leaf],
        )
        async with root.session() as session:
            if activated_leaf:
                await session.update(agent=leaf)
            replacement = replace(
                opening,
                before=before if policy == "before" else None,
                approval=approval if policy == "approval" else None,
            )
            await session.update(tools=[replacement])
            turn = await session.submit("Use the replacement MCP declaration.")
            failure: NativeError | TurnStoppedError | None = None
            try:
                await turn.get_result()
            except (NativeError, TurnStoppedError) as error:
                failure = error
            assert peer.calls == []
            assert observed == [policy]
            assert isinstance(failure, TurnStoppedError if policy == "before" else NativeError)
            followup = await session.submit("Continue without calling tools.")
            assert (await followup.get_result()).final_output == "Session remains reusable."


@pytest.mark.parametrize("policy", ["before", "approval"])
@pytest.mark.parametrize("transition", ["remove", "change"])
async def test_replacement_removes_or_changes_existing_callback_identity(
    responses: ResponsesFixture, policy: str, transition: str
) -> None:
    peer = Peer(names=("search",))
    observed: list[str] = []

    def old_before(context, call):
        observed.append("old-before")
        return StopTurn("obsolete-before-must-not-run")

    def new_before(context, call):
        observed.append("new-before")
        return ReplaceArguments({"key": "replacement-effective"})

    def old_approval(context, call):
        observed.append("old-approval")
        return False

    def new_approval(context, call):
        observed.append("new-approval")
        return True

    responses.respond(mcp_call("search", "mcp__records", "current-callback"))
    responses.respond(assistant_output("Current callback accepted."))
    async with timeout(20), peer.serve() as url:
        opening = HttpMCP(
            name="records",
            url=url,
            tools=["search"],
            approval_mode="preapproved",
            before=old_before if policy == "before" else None,
            approval=old_approval if policy == "approval" else None,
        )
        root = Agent(
            name="Root",
            instructions="Use records.",
            tools=[opening],
            handoffs=[Agent(name="Leaf", instructions="No tools.")],
            provider=provider(responses),
        )
        async with root.session() as session:
            replacement = replace(
                opening,
                before=new_before if policy == "before" and transition == "change" else None,
                approval=new_approval if policy == "approval" and transition == "change" else None,
            )
            await session.update(tools=[replacement])
            turn = await session.submit("Use the current callback generation.")
            assert (await turn.get_result()).final_output == "Current callback accepted."
            assert observed == ([f"new-{policy}"] if transition == "change" else [])
            assert [call["arguments"] for call in peer.calls] == [
                {
                    "key": "replacement-effective"
                    if policy == "before" and transition == "change"
                    else "43"
                }
            ]


@pytest.mark.parametrize("policy", ["before", "approval"])
async def test_opening_mcp_selector_uses_current_replacement_policy(
    responses: ResponsesFixture, policy: str
) -> None:
    peer = Peer(names=("search",))
    observed: list[str] = []

    def opening_before(context, call):
        observed.append("opening-before")
        return Allow()

    def replacement_before(context, call):
        observed.append("replacement-before")
        return StopTurn("current-before-denied")

    def opening_approval(context, call):
        observed.append("opening-approval")
        return True

    def replacement_approval(context, call):
        observed.append("replacement-approval")
        return False

    responses.respond(mcp_call("search", "mcp__records", "old-object-selection"))
    responses.respond(assistant_output("Session remains reusable."))
    async with timeout(20), peer.serve() as url:
        opening = HttpMCP(
            name="records",
            url=url,
            tools=["search"],
            approval_mode="preapproved",
            before=opening_before if policy == "before" else None,
            approval=opening_approval if policy == "approval" else None,
        )
        root = Agent(
            name="Root",
            instructions="Use records.",
            tools=[opening],
            handoffs=[Agent(name="Leaf", instructions="No tools.")],
            provider=provider(responses),
        )
        async with root.session() as session:
            replacement = replace(
                opening,
                before=replacement_before if policy == "before" else None,
                approval=replacement_approval if policy == "approval" else None,
            )
            await session.update(tools=[replacement])
            # An opening object selects the admitted pair, not its superseded policy.
            selected = await session.submit("Select with the opening object.", tools=[opening])
            with pytest.raises(TurnStoppedError if policy == "before" else NativeError):
                await selected.get_result()
            assert observed == [f"replacement-{policy}"]
            assert peer.calls == []
            assert len(responses.requests) == 1
            followup = await session.submit("Continue without calling tools.")
            assert (await followup.get_result()).final_output == "Session remains reusable."
