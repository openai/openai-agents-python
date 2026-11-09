"""Persistent defaults and temporary opening-grant selection at native admission."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest

from agents import (
    Agent,
    CapabilityError,
    Checklist,
    HttpMCP,
    MCPResultContext,
    SessionState,
    ToolRef,
    WebSearch,
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
from tests.test_native_code_mode import code_call
from tests.test_native_mcp_tools import Peer, declarations, mcp_call


async def test_default_explicit_and_queued_selections_capture_opening_grants(
    responses: ResponsesFixture,
) -> None:
    effects = []

    @function_tool
    def first() -> str:
        effects.append("first")
        return "default receipt"

    @function_tool
    def second() -> str:
        effects.append("second")
        return "explicit receipt"

    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(turn, *, step_index):
        if step_index == 0:
            entered.set()
            await release.wait()
        return [second]

    responses.respond(function_call("second", {}, "explicit"))
    responses.respond(assistant_output("temporary"))
    responses.respond(function_call("first", {}, "default"))
    responses.respond(assistant_output("persistent"))
    responses.respond(assistant_output("empty"))
    async with (
        timeout(20),
        Agent(
            instructions="Select", tools=[first, second], provider=provider(responses)
        ).session() as session,
    ):
        assert session.capabilities.get("tool_selection").status == "supported"
        defaults = [first]
        await session.update(tools=defaults)
        defaults.clear()
        explicit = [second]
        active = await session.submit("Explicit", tools=explicit, before_step=hold)
        await entered.wait()
        queued = await session.submit("Defaults", if_busy="queue")
        empty = await session.submit("Empty", tools=[], if_busy="queue")
        explicit.clear()
        with pytest.raises(RuntimeError, match="busy"):
            await session.update(tools=[second])
        release.set()
        assert (await active.get_result()).final_output == "temporary"
        assert (await queued.get_result()).final_output == "persistent"
        assert (await empty.get_result()).final_output == "empty"
        assert effects == ["second", "first"]
        assert [set(declarations(r)) for r in responses.requests] == [
            {("functions", "second")},
            {("functions", "second")},
            {("functions", "first")},
            {("functions", "first")},
            set(),
        ]
        assert tool_output(responses.requests[1], "explicit") == "explicit receipt"
        assert tool_output(responses.requests[3], "default") == "default receipt"
        await session.update()
        responses.respond(assistant_output("still default"))
        await (await session.submit("Again")).get_result()
        assert set(declarations(responses.requests[-1])) == {("functions", "first")}


async def test_default_mcp_roster_republished_after_temporary_opening_selection(
    responses: ResponsesFixture,
) -> None:
    peer = Peer()

    @function_tool
    def local() -> str:
        return "local"

    async with timeout(25), peer.serve() as url:
        remote = HttpMCP(name="docs", url=url, tools=["search"])
        async with Agent(
            instructions="Select", tools=[local, remote], provider=provider(responses)
        ).session() as session:
            await session.update(tools=[local])
            responses.respond(mcp_call("search", "mcp__docs", "remote"))
            responses.respond(assistant_output("remote done"))
            assert (
                await (await session.submit("Remote", tools=[remote])).get_result()
            ).final_output == "remote done"
            assert [call["_meta"]["callId"] for call in peer.calls] == ["remote"]
            assert set(declarations(responses.requests[0])) == {("mcp__docs", "search")}
            responses.respond(assistant_output("default"))
            await (await session.submit("Defaults")).get_result()
            assert set(declarations(responses.requests[-1])) == {("functions", "local")}
            await session.update(tools=[remote])
            responses.respond(assistant_output("empty"))
            await (await session.submit("Temporary empty", tools=[])).get_result()
            assert not declarations(responses.requests[-1])
            responses.respond(assistant_output("remote default"))
            await (await session.submit("Defaults")).get_result()
            assert set(declarations(responses.requests[-1])) == {("mcp__docs", "search")}


async def test_code_mode_fixed_refs_survive_persistent_and_temporary_selections(
    responses: ResponsesFixture,
) -> None:
    from tests.test_native_code_mode import code_call, code_output

    effects: list[str] = []

    @function_tool
    def first() -> str:
        effects.append("first")
        return "persistent receipt"

    @function_tool
    def second() -> str:
        effects.append("second")
        return "temporary receipt"

    fixed = [ToolRef("exec", "functions"), ToolRef("wait", "functions")]
    async with (
        timeout(30),
        Agent(
            instructions="Use selected code tools.",
            tools=[first, second],
            programmatic_tool_calling=True,
            provider=provider(responses),
        ).session() as session,
    ):
        assert session.capabilities.get("tool_selection").status == "supported"
        with pytest.raises(CapabilityError, match="distinct fixed"):
            await session.update(tools=[first, *fixed, fixed[0]])
        assert not responses.requests
        # Explicit selectors remain accepted; alpha Tool-only updates retain
        # the CodeMode grant without repeating its exec/wait refs.
        await session.update(tools=[first, *fixed])
        await session.update(tools=[first])
        responses.respond(
            code_call("text([typeof tools.first, await tools.second({})]);", "explicit")
        )
        responses.respond(assistant_output("temporary"))
        assert (
            await (await session.submit("Use second", tools=[second, *fixed])).get_result()
        ).final_output == "temporary"
        explicit = code_output(responses.requests[1], "explicit")
        assert "undefined" in explicit and "temporary receipt" in explicit
        responses.respond(
            code_call("text([await tools.first({}), typeof tools.second]);", "default")
        )
        responses.respond(assistant_output("default"))
        assert (await (await session.submit("Use default")).get_result()).final_output == "default"
        default = code_output(responses.requests[3], "default")
        assert "persistent receipt" in default and "undefined" in default
        assert effects == ["second", "first"]


async def test_defaults_refuse_outside_opening_fixed_and_mixed_categories(
    responses: ResponsesFixture,
) -> None:
    @function_tool
    def first() -> str:
        return "first"

    @function_tool
    def outside() -> str:
        return "outside"

    checklist = Checklist()
    async with (
        timeout(15),
        Agent(
            instructions="Keep grants", tools=[first, checklist], provider=provider(responses)
        ).session() as session,
    ):
        for selection, expected in (([], set()), ([first], {("functions", "first")})):
            await session.update(tools=selection)
            responses.respond(assistant_output("selected"))
            await (await session.submit("Use updated default")).get_result()
            assert set(declarations(responses.requests[-1])) == expected
        with pytest.raises(CapabilityError):
            await session.update(tools=[first, checklist, outside])
        for kwargs in (
            {"tools": [first, checklist], "instructions": ""},
            {"tools": [first, checklist], "model": "gpt-5.2"},
            {"instructions": "", "model_settings": {"reasoning_effort": "low"}},
        ):
            with pytest.raises(ValueError, match="separately"):
                await session.update(**cast(Any, kwargs))
        await session.update(**cast(Any, {"tools": [checklist], "model_settings": {}}))
        with pytest.raises(CapabilityError):
            await session.submit("Outside", tools=[outside, checklist])
        assert len(responses.requests) == 2
        responses.respond(assistant_output("kept"))
        await (await session.submit("Default")).get_result()
        assert ("functions", "first") not in declarations(responses.requests[-1])
        assert ("functions", "update_plan") in declarations(responses.requests[-1])


async def test_step_selector_retains_admitted_route_after_default_update(
    responses: ResponsesFixture,
) -> None:
    @function_tool
    def first() -> str:
        return "a"

    @function_tool
    def second() -> str:
        return "b"

    async with (
        timeout(15),
        Agent(
            instructions="Select", tools=[first, second], provider=provider(responses)
        ).session() as session,
    ):
        await session.update(tools=[first])
        turn = await session.submit("Default", before_step=lambda turn, step_index: [second])
        with pytest.raises(CapabilityError, match="distinct admitted"):
            await turn.get_result()
        assert not responses.requests


async def test_cancelled_update_joins_native_receipt_before_default_publication(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    @function_tool
    def first() -> str:
        return "a"

    @function_tool
    def second() -> str:
        return "b"

    @function_tool(name="first")
    def rebound() -> str:
        return "accepted new binding"

    accepted, release = asyncio.Event(), asyncio.Event()
    async with (
        timeout(20),
        Agent(
            instructions="Select", tools=[first, second], provider=provider(responses)
        ).session() as session,
    ):
        original = type(session._native)._replace_tools

        # The real native generation is accepted first. Only the receipt delivery
        # is held: this boundary cannot be controlled by a provider response.
        async def delayed(native, *args, **kwargs):
            receipt = await original(native, *args, **kwargs)
            accepted.set()
            await release.wait()
            return receipt

        monkeypatch.setattr(type(session._native), "_replace_tools", delayed)
        update = asyncio.create_task(session.update(tools=[rebound]))
        await accepted.wait()
        for operation in (session.submit("Busy"), session.update(), session.save_state()):
            with pytest.raises(RuntimeError):
                await operation
        update.cancel()
        await asyncio.sleep(0)
        assert not update.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await update
        responses.respond(function_call("first", {}, "accepted-call"))
        responses.respond(assistant_output("accepted"))
        await (await session.submit("Defaults")).get_result()
        assert tool_output(responses.requests[-1], "accepted-call") == "accepted new binding"
        assert set(declarations(responses.requests[-1])) == {("functions", "first")}
        responses.respond(assistant_output("opening grant"))
        await (await session.submit("Explicit", tools=[second])).get_result()
        assert set(declarations(responses.requests[-1])) == {("functions", "second")}
        responses.respond(function_call("first", {}, "opening-selector"))
        responses.respond(assistant_output("still current"))
        await (await session.submit("Opening object", tools=[first])).get_result()
        assert tool_output(responses.requests[-1], "opening-selector") == "accepted new binding"


@pytest.mark.parametrize("default", ["unchanged", "first", "empty"])
async def test_updated_default_and_explicit_manual_restore_use_current_opening_bindings(
    responses: ResponsesFixture,
    default: str,
) -> None:
    from agents import FunctionTool
    from tests.test_native_manual_tools import LookupArgs, next_request

    @function_tool
    def first() -> str:
        return "a"

    lookup = FunctionTool(
        name="lookup", description="Look up input", input_type=LookupArgs, output_type=str
    )
    agent = Agent(instructions="Manual", tools=[first, lookup], provider=provider(responses))
    responses.respond(function_call("lookup", {"key": "synthetic"}, "manual"))
    async with timeout(25):
        async with agent.session() as source:
            if default != "unchanged":
                await source.update(tools=[first] if default == "first" else [])
            await source.submit("Explicit manual", tools=[lookup])
            pending = await next_request(source.interactions(), lookup)
            assert pending.name == "lookup"
            saved = await source.suspend()
        responses.respond(assistant_output("restored"))
        async with agent.session(state=saved) as restored:
            request = await next_request(restored.interactions(), lookup)
            await request.respond("answer")
            handle = restored.restored_turn
            assert handle is not None
            assert (await handle.get_result()).final_output == "restored"
            assert set(declarations(responses.requests[-1])) == {("functions", "lookup")}
            responses.respond(assistant_output("next default"))
            assert (await (await restored.submit("Next")).get_result()).final_output == (
                "next default"
            )
            expected = {
                "unchanged": {("functions", "first"), ("functions", "lookup")},
                "first": {("functions", "first")},
                "empty": set(),
            }
            assert set(declarations(responses.requests[-1])) == expected[default]
            # The restored default remains a subset, never a smaller ceiling.
            responses.respond(assistant_output("temporary again"))
            await (await restored.submit("Other opening grant", tools=[lookup.ref])).get_result()
            assert set(declarations(responses.requests[-1])) == {("functions", "lookup")}
            responses.respond(assistant_output("same default"))
            await (await restored.submit("Default again")).get_result()
            assert set(declarations(responses.requests[-1])) == expected[default]


@pytest.mark.parametrize("restore_ceiling", ["saved-default", "full-opening"])
@pytest.mark.parametrize("temporary", [False, True], ids=["idle-update", "after-explicit"])
async def test_idle_checkpoint_keeps_persistent_default_and_current_opening_ceiling(
    responses: ResponsesFixture, restore_ceiling: str, temporary: bool
) -> None:
    effects = []

    @function_tool
    def first() -> str:
        effects.append("first")
        return "first"

    @function_tool
    def other() -> str:
        effects.append("other")
        return "other"

    original = Agent(instructions="Selected", tools=[first, other], provider=provider(responses))
    async with timeout(25):
        async with original.session() as session:
            await session.update(tools=[first])
            if temporary:
                responses.respond(function_call("other", {}, "old-explicit"))
                responses.respond(assistant_output("temporary"))
                assert (
                    await (await session.submit("Other", tools=[other])).get_result()
                ).final_output == "temporary"
                assert effects == ["other"]
            saved = await session.save_state()
            # A checkpoint must not shrink the current Session's ceiling.
            responses.respond(function_call("other", {}, "after-save"))
            responses.respond(assistant_output("still allowed"))
            assert (
                await (await session.submit("Other", tools=[other])).get_result()
            ).final_output == "still allowed"
        effects.clear()
        opening = [first] if restore_ceiling == "saved-default" else [first, other]
        fresh = Agent(instructions="Selected", tools=opening, provider=provider(responses))
        responses.respond(function_call("first", {}, "restored-first"))
        responses.respond(assistant_output("default"))
        async with fresh.session(state=saved) as restored:
            handle = await restored.submit("Default")
            assert (await handle.get_result()).final_output == "default"
            assert effects == ["first"]
            assert set(declarations(responses.requests[-2])) == {("functions", "first")}
            if restore_ceiling == "full-opening":
                responses.respond(function_call("other", {}, "restored-explicit"))
                responses.respond(assistant_output("explicit"))
                handle = await restored.submit("Other", tools=[other.ref])
                assert (await handle.get_result()).final_output == "explicit"
                assert effects == ["first", "other"]
                responses.respond(assistant_output("back to default"))
                assert (
                    await (await restored.submit("Default")).get_result()
                ).final_output == "back to default"
                assert set(declarations(responses.requests[-1])) == {("functions", "first")}
            else:
                with pytest.raises(CapabilityError):
                    await restored.submit("Outside opening", tools=[other])


@pytest.mark.parametrize("temporary", [False, True], ids=["idle-update", "after-explicit"])
async def test_idle_checkpoint_preserves_acknowledged_python_default_order(
    responses: ResponsesFixture, temporary: bool
) -> None:
    @function_tool
    def first() -> str:
        return "first"

    @function_tool
    def other() -> str:
        return "other"

    agent = Agent(instructions="Selected", tools=[first, other], provider=provider(responses))
    expected = [("functions", "other"), ("functions", "first")]
    async with timeout(25):
        async with agent.session() as session:
            await session.update(tools=[other, first])
            responses.respond(assistant_output("before save"))
            await (await session.submit("Default before save")).get_result()
            assert list(declarations(responses.requests[-1])) == expected
            if temporary:
                responses.respond(assistant_output("temporary"))
                await (await session.submit("Temporary", tools=[first])).get_result()
                assert list(declarations(responses.requests[-1])) == [("functions", "first")]
            saved = await session.save_state()
            responses.respond(assistant_output("after save"))
            await (await session.submit("Default after save")).get_result()
            assert list(declarations(responses.requests[-1])) == expected
        async with agent.session(state=saved) as restored:
            responses.respond(assistant_output("restored"))
            await (await restored.submit("Restored default")).get_result()
            assert list(declarations(responses.requests[-1])) == expected


async def test_rebinding_keeps_current_callback_schema_through_queue_and_step(responses):
    from agents import Allow

    effects = []
    policies = []

    def old_before(ctx):
        policies.append("stale-before")
        return Allow()

    def old_after(ctx, output):
        policies.append("stale-after")
        return output

    def new_before(ctx):
        policies.append("current-before")
        return Allow()

    def new_after(ctx, output):
        policies.append("current-after")
        return output

    @function_tool(name="lookup", before=old_before, after=old_after)
    def original(key: str) -> str:
        effects.append("stale")
        return key

    @function_tool(name="lookup", before=new_before, after=new_after)
    def current(count: int) -> str:
        """Current numeric lookup"""
        effects.append(("current", count))
        return str(count)

    @function_tool
    def other() -> str:
        effects.append("other")
        return "other"

    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(turn, *, step_index):
        entered.set()
        await release.wait()
        return [other]

    responses.respond(function_call("other", {}, "temporary"))
    responses.respond(assistant_output("other done"))
    responses.respond(function_call("lookup", {"count": 7}, "rebound"))
    responses.respond(assistant_output("current done"))
    async with (
        timeout(20),
        Agent(
            instructions="Use selected tools.",
            tools=[original, other],
            provider=provider(responses),
        ).session() as session,
    ):
        await session.update(tools=[current])
        from dataclasses import replace

        with pytest.raises(CapabilityError, match="bindings"):
            await session.submit("Foreign", tools=[replace(current)])
        active = await session.submit("Other", tools=[other], before_step=hold)
        await entered.wait()
        queued = await session.submit("Default", if_busy="queue")
        with pytest.raises(RuntimeError, match="busy"):
            await session.update(tools=[original])
        release.set()
        assert (await active.get_result()).final_output == "other done"
        assert (await queued.get_result()).final_output == "current done"
        assert effects == ["other", ("current", 7)]
        assert policies == ["current-before", "current-after"]
        wire = declarations(responses.requests[2])["functions", "lookup"]
        assert wire["description"] == "Current numeric lookup"
        assert set(wire["parameters"]["properties"]) == {"count"}
        assert tool_output(responses.requests[3], "rebound") == "7"

        responses.respond(function_call("lookup", {"count": 8}, "selected"))
        responses.respond(assistant_output("ref"))
        await (
            await session.submit(
                "By ref", tools=[original.ref], before_step=lambda *_a, **_k: [current]
            )
        ).get_result()
        assert set(declarations(responses.requests[-1])) == {("functions", "lookup")}
        assert effects == ["other", ("current", 7), ("current", 8)]
        assert policies == ["current-before", "current-after"] * 2
        assert tool_output(responses.requests[-1], "selected") == "8"


async def test_rebind_same_name_to_manual_and_restore_current_declaration(responses):
    from pydantic import BaseModel

    from agents import ActionRequested, FunctionTool, InteractionRequired

    class Query(BaseModel):
        key: str

    @function_tool(name="lookup")
    def automatic(key: str) -> str:
        return "stale:" + key

    manual = FunctionTool(name="lookup", description="Ask", input_type=Query, output_type=str)
    responses.respond(function_call("lookup", {"key": "q"}, "pending"))
    async with (
        timeout(20),
        Agent(
            instructions="Ask.", tools=[automatic], provider=provider(responses)
        ).session() as session,
    ):
        await session.update(tools=[manual])
        issued = await session.submit("Ask")
        async for event in session.interactions():
            if isinstance(event, ActionRequested):
                assert manual.match(event) is event
                assert automatic.match(event) is None
                saved = await session.suspend()
                break
        with pytest.raises(InteractionRequired):
            await issued.get_result()
    responses.respond(assistant_output("restored"))
    async with Agent(instructions="Ask.", tools=[manual], provider=provider(responses)).session(
        state=saved
    ) as restored:
        issued = restored.active_turn
        assert issued is not None
        async for event in restored.interactions():
            if isinstance(event, ActionRequested):
                assert manual.match(event) is event
                await event.respond("current:" + event.arguments.key)
                break
        assert (await issued.get_result()).final_output == "restored"


@pytest.mark.parametrize("credential_source", ["literal", "environment"])
async def test_idle_mcp_rotation_retains_all_opening_leaves_after_temporary_selection(
    responses, monkeypatch, credential_source
):
    import os
    from dataclasses import replace

    from agents import Allow

    old, new = Peer(label="old"), Peer(label="new")
    policies = []

    def old_before(ctx, call):
        policies.append("old-before")
        return Allow()

    def old_result(ctx, value):
        policies.append("old-result")
        return value

    def new_before(ctx, call):
        policies.append("new-before")
        return Allow()

    def new_approval(ctx, call):
        policies.append("new-approval")
        return True

    def new_result(ctx, value):
        policies.append("new-result")
        return value

    monkeypatch.setenv("SDK_ROTATED_TOKEN", "synthetic-new-token")
    token = (
        "synthetic-new-token" if credential_source == "literal" else os.environ["SDK_ROTATED_TOKEN"]
    )

    @function_tool
    def local() -> str:
        return "local"

    async with timeout(30), old.serve() as old_url, new.serve() as new_url:
        original = HttpMCP(
            name="docs",
            url=old_url,
            tools=["search", "excluded"],
            bearer_token="synthetic-old-token",
            before=old_before,
            transform_result=old_result,
        )
        current = replace(
            original,
            url=new_url,
            tools=["search"],
            bearer_token=token,
            before=new_before,
            approval=new_approval,
            transform_result=new_result,
        )
        async with Agent(
            instructions="Use tools.", tools=[original, local], provider=provider(responses)
        ).session() as session:
            responses.respond(mcp_call("search", "mcp__docs", "initial"))
            responses.respond(assistant_output("initial done"))
            await (await session.submit("Initial", tools=[original])).get_result()
            await session.update(tools=[])
            await session.update(tools=[current])
            with pytest.raises(CapabilityError, match="bindings"):
                await session.submit("Foreign", tools=[replace(current)])
            with pytest.raises(CapabilityError):
                await session.update(tools=[replace(current, tools=["outside"])])
            from agents import StdioMCP

            with pytest.raises(CapabilityError, match="Host stdio"):
                await session.update(
                    tools=[StdioMCP(name="docs", tools=["search"], command="/must-not-start")]
                )
            responses.respond(mcp_call("excluded", "mcp__docs", "outside-default"))
            responses.respond(assistant_output("extra done"))
            await (
                await session.submit("Other admitted leaf", tools=[original.tool("excluded")])
            ).get_result()
            responses.respond(assistant_output("temporary"))
            await (await session.submit("Local", tools=[local])).get_result()
            responses.respond(mcp_call("search", "mcp__docs", "default"))
            responses.respond(assistant_output("default done"))
            await (await session.submit("Default")).get_result()
            assert [call["_meta"]["callId"] for call in new.calls] == ["outside-default", "default"]
            assert [call["_meta"]["callId"] for call in old.calls] == ["initial"]
            assert set(new.auth) == {"Bearer synthetic-new-token"}
            assert set(old.auth) == {"Bearer synthetic-old-token"}
            assert set(declarations(responses.requests[-1])) == {("mcp__docs", "search")}
            assert (
                policies
                == ["old-before", "old-result"] + ["new-before", "new-approval", "new-result"] * 2
            )


async def test_acknowledged_multi_descriptor_mcp_default_remains_usable(responses):
    from dataclasses import replace

    old, current = Peer(label="old"), Peer(label="current")

    @function_tool
    def local() -> str:
        return "local"

    async with timeout(25), old.serve() as old_url, current.serve() as new_url:
        opening = HttpMCP(name="docs", url=old_url, tools=["search", "excluded"])
        search = replace(opening, url=new_url, tools=["search"], bearer_token="synthetic-rotated")
        excluded = replace(search, tools=["excluded"])
        async with Agent(
            instructions="Use current tools.",
            tools=[opening, local],
            provider=provider(responses),
        ).session() as session:
            await session.update(tools=[search, excluded])
            responses.respond(mcp_call("search", "mcp__docs", "search-1"))
            responses.respond(mcp_call("excluded", "mcp__docs", "excluded-1"))
            responses.respond(assistant_output("both"))
            assert (
                await (await session.submit("Both default leaves")).get_result()
            ).final_output == "both"
            assert set(declarations(responses.requests[0])) == {
                ("mcp__docs", "search"),
                ("mcp__docs", "excluded"),
            }
            responses.respond(assistant_output("local"))
            await (await session.submit("Temporary", tools=[local])).get_result()
            responses.respond(mcp_call("search", "mcp__docs", "search-2"))
            responses.respond(assistant_output("default again"))
            await (await session.submit("Default")).get_result()
            assert not old.calls
            assert [c["_meta"]["callId"] for c in current.calls] == [
                "search-1",
                "excluded-1",
                "search-2",
            ]
            assert set(current.auth) == {"Bearer synthetic-rotated"}


@pytest.mark.parametrize("route", ["submit", "before_step"])
async def test_each_accepted_mcp_descriptor_selects_current_alias_until_replaced(responses, route):
    from dataclasses import replace

    old, current, latest = Peer(label="old"), Peer(label="current"), Peer(label="latest")
    async with (
        timeout(30),
        old.serve() as old_url,
        current.serve() as current_url,
        latest.serve() as latest_url,
    ):
        opening = HttpMCP(name="docs", url=old_url, tools=["search", "excluded"])
        search = replace(
            opening, url=current_url, tools=["search"], bearer_token="synthetic-current"
        )
        excluded = replace(search, tools=["excluded"])
        async with Agent(
            instructions="Use the selected tool.", tools=[opening], provider=provider(responses)
        ).session() as session:
            await session.update(tools=[search, excluded])
            for descriptor in (search, excluded):
                raw = descriptor.tools[0]
                with pytest.raises(CapabilityError, match="bindings"):
                    await session.submit("Unaccepted copy", tools=[replace(descriptor)])
                responses.respond(mcp_call(raw, "mcp__docs", raw))
                responses.respond(assistant_output(raw + " done"))
                if route == "submit":
                    turn = await session.submit("Selected descriptor", tools=[descriptor])
                else:
                    turn = await session.submit(
                        "Select the same operation roster by descriptor",
                        tools=[descriptor.tool(raw)],
                        before_step=lambda *_a, selected=descriptor, **_k: [selected],
                    )
                assert (await turn.get_result()).final_output == raw + " done"
                assert set(declarations(responses.requests[-1])) == {("mcp__docs", raw)}
            assert [c["_meta"]["callId"] for c in current.calls] == ["search", "excluded"]
            assert set(current.auth) == {"Bearer synthetic-current"}
            assert not old.calls

            # Replacing one leaf rotates the whole alias, but old accepted identities expire.
            replacement = replace(search, url=latest_url, bearer_token="synthetic-latest")
            await session.update(tools=[replacement])
            for retired in (search, excluded):
                with pytest.raises(CapabilityError, match="bindings"):
                    await session.submit("Retired descriptor", tools=[retired])
            responses.respond(mcp_call("excluded", "mcp__docs", "opening-selector"))
            responses.respond(assistant_output("latest done"))
            turn = await session.submit("Omitted opening leaf", tools=[opening.tool("excluded")])
            assert (await turn.get_result()).final_output == "latest done"
            assert [c["_meta"]["callId"] for c in latest.calls] == ["opening-selector"]
            assert set(latest.auth) == {"Bearer synthetic-latest"}
            assert [c["_meta"]["callId"] for c in current.calls] == ["search", "excluded"]
            assert not old.calls


async def test_ungranted_python_discovery_rebind_refuses_before_mcp_publication(responses):
    from dataclasses import replace

    old, rotated = Peer(label="prior"), Peer(label="must not acquire")
    effects = []

    @function_tool(description="Read the current skylark invoice")
    def lookup(key: str) -> str:
        effects.append(key)
        return "direct:" + key

    async with timeout(25), old.serve() as old_url, rotated.serve() as new_url:
        opening = HttpMCP(name="docs", url=old_url, tools=["search"])
        async with Agent(
            instructions="Use direct tools.",
            model="gpt-5.5",
            tools=[lookup, opening],
            provider=provider(responses),
        ).session() as session:
            await session.update(tools=[lookup, opening])
            with pytest.raises(CapabilityError, match="discovery.*opening"):
                await session.update(
                    tools=[replace(lookup, defer_loading=True), replace(opening, url=new_url)]
                )
            assert not rotated.requests
            responses.respond(function_call("lookup", {"key": "invoice"}, "prior-python"))
            responses.respond(mcp_call("search", "mcp__docs", "prior-mcp"))
            responses.respond(assistant_output("unchanged"))
            assert (
                await (await session.submit("Prior default")).get_result()
            ).final_output == "unchanged"
            assert effects == ["invoice"]
            assert [c["_meta"]["callId"] for c in old.calls] == ["prior-mcp"]
            assert set(declarations(responses.requests[0])) == {
                ("functions", "lookup"),
                ("mcp__docs", "search"),
            }
            assert not rotated.requests


@pytest.mark.parametrize("deferred", [False, True], ids=["direct", "discovery"])
async def test_mcp_opening_mode_refusal_keeps_bindings_and_allows_same_mode_rotation(
    responses, deferred
):
    import json
    from dataclasses import replace

    from tests.test_native_schema_discovery import discovered, search

    old, invalid, rotated = Peer(label="old"), Peer(label="invalid"), Peer(label="rotated")

    @function_tool(defer_loading=True, description="Separate admitted schema discovery")
    def anchor() -> str:
        pytest.fail("The discovery anchor was called")

    async with (
        timeout(35),
        old.serve() as old_url,
        invalid.serve() as invalid_url,
        rotated.serve() as rotated_url,
    ):
        opening = HttpMCP(
            name="docs",
            url=old_url,
            tools=["search", "excluded"],
            bearer_token="synthetic-old",
            defer_loading=deferred,
        )
        async with Agent(
            instructions="Use selected tools.",
            model="gpt-5.5",
            tools=[opening, anchor],
            provider=provider(responses),
        ).session() as session:
            # Keep a different discovery grant active throughout the attempted update.
            await session.update(tools=[opening, anchor])
            with pytest.raises(CapabilityError, match="MCP.*discovery.*opening"):
                await session.update(
                    tools=[
                        replace(
                            opening,
                            url=invalid_url,
                            bearer_token="synthetic-invalid",
                            defer_loading=not deferred,
                        ),
                        anchor,
                    ]
                )
            assert not invalid.requests
            for prompt, selection in (("default", None), ("explicit", [opening.tool("search")])):
                start = len(responses.requests)
                if deferred:
                    responses.respond(search("synthetic record search", "find-" + prompt))
                responses.respond(mcp_call("search", "mcp__docs", prompt))
                responses.respond(assistant_output(prompt))
                assert (
                    await (await session.submit(prompt, tools=selection)).get_result()
                ).final_output == prompt
                offered = set(declarations(responses.requests[start]))
                if deferred:
                    assert ("mcp__docs", "search") not in offered
                    assert "search" in json.dumps(discovered(responses, "find-" + prompt))
                else:
                    assert ("mcp__docs", "search") in offered
            assert [c["_meta"]["callId"] for c in old.calls] == ["default", "explicit"]
            assert set(old.auth) == {"Bearer synthetic-old"}
            assert not invalid.requests

            # A same-mode credential and URL refresh remains a supported persistent update.
            current = replace(opening, url=rotated_url, bearer_token="synthetic-rotated")
            await session.update(tools=[current])
            if deferred:
                responses.respond(search("synthetic record search", "find-rotated"))
            responses.respond(mcp_call("search", "mcp__docs", "rotated"))
            responses.respond(assistant_output("rotated"))
            assert (await (await session.submit("Current default")).get_result()).final_output == (
                "rotated"
            )
            if deferred:
                assert "search" in json.dumps(discovered(responses, "find-rotated"))
            assert [c["_meta"]["callId"] for c in rotated.calls] == ["rotated"]
            assert set(rotated.auth) == {"Bearer synthetic-rotated"}
            assert [c["_meta"]["callId"] for c in old.calls] == ["default", "explicit"]
            assert not invalid.requests


async def test_python_rebind_uses_discovery_granted_by_opening_not_default(responses):
    import json
    from dataclasses import replace

    from tests.test_native_schema_discovery import discovered, search

    calls = []

    @function_tool(description="Find the current skylark invoice")
    def skylark_invoice(key: str) -> str:
        calls.append(key)
        return "invoice:" + key

    @function_tool(defer_loading=True, description="Another discovery grant")
    def anchor() -> str:
        pytest.fail("The omitted anchor was called")

    async with (
        timeout(25),
        Agent(
            instructions="Use current invoice.",
            model="gpt-5.5",
            tools=[skylark_invoice, anchor],
            provider=provider(responses),
        ).session() as session,
    ):
        await session.update(tools=[skylark_invoice])
        await session.update(tools=[replace(skylark_invoice, defer_loading=True)])
        responses.respond(search("current skylark invoice", "find-invoice"))
        responses.respond(function_call("skylark_invoice", {"key": "A1"}, "use-invoice"))
        responses.respond(assistant_output("found"))
        assert (await (await session.submit("Discover")).get_result()).final_output == "found"
        assert "tool_search" in json.dumps(responses.requests[0]["tools"])
        schema = json.dumps(discovered(responses, "find-invoice"))
        assert "skylark_invoice" in schema and "anchor" not in schema
        assert calls == ["A1"]


@pytest.mark.parametrize("python_subset", [False, True], ids=["mcp-only", "with-python"])
@pytest.mark.parametrize("select_mcp", [False, True], ids=["empty-mcp", "one-leaf"])
async def test_idle_restore_retains_mcp_defaults_without_replaying_credentials(
    responses: ResponsesFixture, python_subset: bool, select_mcp: bool
) -> None:
    old, current = Peer(), Peer()

    @function_tool
    def one() -> str:
        return "one"

    @function_tool
    def two() -> str:
        return "two"

    async with timeout(40), old.serve() as old_url, current.serve() as current_url:
        old_docs = HttpMCP(
            name="docs", url=old_url, tools=["search", "excluded"], bearer_token="old"
        )
        current_docs = HttpMCP(
            name="docs", url=current_url, tools=["search", "excluded"], bearer_token="current"
        )
        opening = [one, two, old_docs] if python_subset else [old_docs]
        agent = Agent(
            instructions="Use only granted tools", tools=opening, provider=provider(responses)
        )
        async with agent.session() as source:
            selected = [one] if python_subset else []
            if select_mcp:
                selected.append(old_docs.tool("search"))
            await source.update(tools=selected)
            saved = await source.save_state()
            assert "Bearer old" not in saved.to_json() and old_url not in saved.to_json()
        previous_old = list(old.requests)
        opening_now = [one, two, current_docs] if python_subset else [current_docs]
        async with agent.session(state=saved, tools=opening_now) as restored:
            assert not current.requests  # Stored selectors are not live authority or discovery.
            responses.respond(assistant_output("default"))
            assert (await (await restored.submit("Default")).get_result()).final_output == "default"
            declared = set(declarations(responses.requests[-1]))
            assert (("mcp__docs", "search") in declared) is select_mcp
            assert ("mcp__docs", "excluded") not in declared
            if python_subset:
                assert ("functions", "one") in declared and ("functions", "two") not in declared
            # A smaller default must not shrink the opening ceiling.
            responses.respond(mcp_call("excluded", "mcp__docs", "selected-explicitly"))
            responses.respond(assistant_output("explicit"))
            turn = await restored.submit(
                "Other opening tool", tools=[current_docs.tool("excluded")]
            )
            assert (await turn.get_result()).final_output == "explicit"
            assert ("mcp__docs", "excluded") in declarations(responses.requests[-2])
            assert old.requests == previous_old
            assert current.auth and set(current.auth) == {"Bearer current"}
            assert [call["_meta"]["callId"] for call in current.calls] == ["selected-explicitly"]


@pytest.mark.parametrize("bad", ["unknown-server", "unknown-tool", "duplicate"])
async def test_idle_restore_mcp_selectors_cannot_supply_extra_authority(
    responses: ResponsesFixture, bad: str
) -> None:
    peer = Peer()
    async with timeout(30), peer.serve() as url:
        docs = HttpMCP(name="docs", url=url, tools=["search"])
        agent = Agent(instructions="Only docs", tools=[docs], provider=provider(responses))
        async with agent.session() as source:
            await source.update(tools=[docs.tool("search")])
            saved = await source.save_state()
        raw = json.loads(saved.to_json())
        pair = {"server": "docs", "name": "search"}
        if bad == "unknown-server":
            raw["step_mcp_tools"] = [{"server": "not-admitted", "name": "search"}]
        elif bad == "unknown-tool":
            raw["step_mcp_tools"] = [{"server": "docs", "name": "hidden"}]
        else:
            raw["step_mcp_tools"] = [pair, pair]
        before = list(peer.requests)
        with pytest.raises(ValueError, match="distinct current declarations"):
            async with agent.session(state=SessionState.from_json(json.dumps(raw))):
                pytest.fail("Invalid selector unexpectedly opened")
        assert peer.requests == before
        assert not responses.requests


@pytest.mark.parametrize("temporary", ["empty", "local", "remote"])
async def test_idle_save_does_not_reacquire_acknowledged_default_mcp(responses, temporary):
    old, live, current = Peer(), Peer(), Peer()
    policies = []

    @function_tool
    def first() -> str:
        return "first"

    @function_tool
    def other() -> str:
        return "other"

    def accept(ctx: MCPResultContext[None], value: dict[str, Any]) -> dict[str, Any]:
        policies.append(ctx)
        return value

    async with (
        timeout(30),
        old.serve() as old_url,
        live.serve() as live_url,
        current.serve() as current_url,
    ):
        remote = HttpMCP(name="docs", url=old_url, tools=["search"], bearer_token="old")
        live_remote = HttpMCP(name="live", url=live_url, tools=["search"], transform_result=accept)
        agent = Agent(
            instructions="Use granted records.",
            tools=[first, other, remote, live_remote],
            provider=provider(responses),
        )
        async with agent.session() as source:
            await source.update(tools=[first, remote])
            assert any(request.get("method") == "tools/list" for request in old.requests)
            selection = {"empty": [], "local": [other], "remote": [live_remote]}[temporary]
            responses.respond(assistant_output("temporary"))
            await (await source.submit("Temporary selection.", tools=selection)).get_result()
            old.denied = True
            before = [list(server.requests) for server in (old, live, current)]
            saved = await source.save_state()
            assert [server.requests for server in (old, live, current)] == before
            assert [spec["name"] for spec in saved.managed_tool_specs] == ["first"]
            assert json.loads(saved.to_json())["step_mcp_tools"] == [
                {"server": "docs", "name": "search"}
            ]
            assert old_url not in saved.to_json() and "Bearer old" not in saved.to_json()
            if temporary == "remote":
                # A Python-only checkpoint catalog publication must not leave
                # the already-live MCP result policy in an expired generation.
                responses.respond(mcp_call("search", "mcp__live", "live-after-save"))
                responses.respond(assistant_output("still live"))
                await (await source.submit("Keep live.", tools=[live_remote])).get_result()
                assert len(policies) == 1 and len(live.calls) == 1
        restored_remote = HttpMCP(
            name="docs", url=current_url, tools=["search"], bearer_token="current"
        )
        fresh = Agent(
            instructions="Use granted records.",
            tools=[first, other, restored_remote],
            provider=provider(responses),
        )
        responses.respond(mcp_call("search", "mcp__docs", "fresh-default"))
        responses.respond(assistant_output("restored"))
        async with fresh.session(state=saved) as restored:
            assert not current.requests
            assert (
                await (await restored.submit("Defaults.")).get_result()
            ).final_output == "restored"
        assert [request["_meta"]["callId"] for request in current.calls] == ["fresh-default"]
        assert set(current.auth) == {"Bearer current"}
        assert old.requests == before[0]


@pytest.mark.parametrize("fixed", ["checklist", "web-standalone", "web-hosted"])
@pytest.mark.parametrize("selected_python", [False, True], ids=["empty", "python"])
@pytest.mark.parametrize("legacy", [False, True], ids=["exact", "legacy"])
async def test_idle_restore_preserves_omitted_fixed_defaults(
    responses: ResponsesFixture, fixed: str, selected_python: bool, legacy: bool
) -> None:
    @function_tool
    def local() -> str:
        return "local"

    declaration = (
        Checklist()
        if fixed == "checklist"
        else WebSearch(route="standalone" if fixed == "web-standalone" else "hosted")
    )
    agent = Agent(
        instructions="Use only selected tools.",
        model="gpt-5.5",
        tools=[local, declaration],
        provider=provider(responses),
    )
    selected = [local] if selected_python else []
    async with timeout(40):
        async with agent.session() as source:
            await source.update(tools=selected)
            # An operation can select an opening grant without changing defaults.
            responses.respond(assistant_output("temporary"))
            await (await source.submit("Temporary full roster", tools=agent.tools)).get_result()
            saved = await source.save_state()
            assert saved.agent_options["_sdk_default_tools"] == (
                [{"name": "local", "namespace": None}] if selected_python else []
            )
        if legacy:
            raw = json.loads(saved.to_json())
            raw["agent_options"].pop("_sdk_default_tools")
            saved = SessionState.from_json(json.dumps(raw))
        async with agent.session(state=saved) as restored:
            responses.respond(assistant_output("restored"))
            await (await restored.submit("Persistent default")).get_result()
            request = responses.requests[-1]
            offered = set(declarations(request))
            assert (("functions", "local") in offered) is selected_python
            if fixed == "checklist":
                enabled = ("functions", "update_plan") in offered
            elif fixed == "web-hosted":
                enabled = any(
                    item.get("type", "").startswith("web_search") for item in request["tools"]
                )
            else:
                enabled = any(item.get("name") == "web" for item in request["tools"])
            # Legacy checkpoints keep their historical fixed-grant fallback.
            assert enabled is legacy
            # An exact smaller default does not revoke the opening ceiling.
            responses.respond(assistant_output("explicit"))
            await (await restored.submit("Explicit opening roster", tools=agent.tools)).get_result()
            explicit = responses.requests[-1]
            assert ("functions", "local") in declarations(explicit)
            if fixed == "checklist":
                assert ("functions", "update_plan") in declarations(explicit)
            elif fixed == "web-hosted":
                assert any(
                    item.get("type", "").startswith("web_search") for item in explicit["tools"]
                )
            else:
                assert any(item.get("name") == "web" for item in explicit["tools"])


@pytest.mark.parametrize("updated", [False, True], ids=["opening-default", "updated-default"])
async def test_idle_save_excludes_temporary_fixed_selection_from_defaults(
    responses: ResponsesFixture, updated: bool
) -> None:
    @function_tool
    def local() -> str:
        return "local"

    agent = Agent(
        instructions="Use selected tools.",
        tools=[local, Checklist()],
        provider=provider(responses),
    )
    async with timeout(30):
        async with agent.session() as source:
            if updated:
                await source.update(tools=[local, agent.tools[1]])
            responses.respond(assistant_output("temporary empty"))
            await (await source.submit("Temporary empty", tools=[])).get_result()
            saved = await source.save_state()
            assert ("_sdk_default_tools" in saved.agent_options) is updated
        async with agent.session(state=saved) as restored:
            responses.respond(assistant_output("default"))
            await (await restored.submit("Default roster")).get_result()
            assert set(declarations(responses.requests[-1])) == {
                ("functions", "local"),
                ("functions", "update_plan"),
            }


@pytest.mark.parametrize("capture", ["save_state", "fork"])
@pytest.mark.parametrize("programmatic", [False, True], ids=["sdk4", "sdk6"])
@pytest.mark.parametrize("selection", ["omitted", "empty", "subset"])
async def test_idle_default_selection_survives_public_capture_and_restore(
    responses: ResponsesFixture, capture: str, programmatic: bool, selection: str
) -> None:
    effects = []

    @function_tool
    def first() -> str:
        effects.append("first")
        return "first"

    @function_tool
    def second() -> str:
        effects.append("second")
        return "second"

    agent = Agent(
        instructions="Use only the selected tools.",
        tools=[first, second],
        provider=provider(responses),
        programmatic_tool_calling=programmatic,
    )
    async with timeout(60):
        async with agent.session() as source:
            if programmatic:
                responses.respond(code_call("store('idle-default-marker',7);", "seed-default-vm"))
            responses.respond(assistant_output("remembered"))
            await (await source.submit("Remember the source.")).get_result()
            if selection != "omitted":
                await source.update(tools=[] if selection == "empty" else [first])
            saved = await source.save_state() if capture == "save_state" else await source.fork()
            assert saved.format_version == (6 if programmatic else 4)
            assert (saved.source_session_id == source.id) is (capture == "save_state")
            if selection == "omitted":
                assert "_sdk_default_tools" not in saved.agent_options
            elif selection == "empty":
                # update() retains configured execution helpers, without granting leaves.
                assert saved.agent_options["_sdk_default_tools"] == (
                    [
                        {"name": "exec", "namespace": "functions"},
                        {"name": "wait", "namespace": "functions"},
                    ]
                    if programmatic
                    else []
                )
            else:
                persisted = saved.agent_options["_sdk_default_tools"]
                assert {"name": "first", "namespace": None} in persisted
                assert {"name": "second", "namespace": None} not in persisted
            if programmatic:
                assert saved.root_code_mode is not None
                assert saved.root_code_mode["stored_values"]["idle-default-marker"] == 7
            # Both public capture APIs return a validated, reloadable envelope.
            saved = SessionState.from_json(saved.to_json())
            raw = json.loads(saved.to_json())
            for invalid in (None, [{"name": 17, "namespace": None}]):
                broken = json.loads(saved.to_json())
                broken.setdefault("agent_options", {})["_sdk_default_tools"] = invalid
                with pytest.raises(ValueError):
                    SessionState.from_json(json.dumps(broken))
            duplicate = json.loads(saved.to_json())
            duplicate.setdefault("agent_options", {})["_sdk_default_tools"] = [
                {"name": "first", "namespace": None},
                {"name": "first", "namespace": None},
            ]
            with pytest.raises(ValueError, match="distinct declarations"):
                SessionState.from_json(json.dumps(duplicate))
            raw["operation_tools"] = []
            with pytest.raises(ValueError, match="SDK manual checkpoint"):
                SessionState.from_json(json.dumps(raw))
            if not programmatic:
                legacy = json.loads(saved.to_json())
                legacy["format_version"] = 2
                legacy.pop("output_version")
                legacy.pop("output_fingerprint")
                legacy.setdefault("agent_options", {})["_sdk_default_tools"] = []
                with pytest.raises(ValueError, match="Saved defaults require an SDK checkpoint"):
                    SessionState.from_json(json.dumps(legacy))
            responses.respond(assistant_output("source remains usable"))
            assert (
                await (await source.submit("Continue the source.")).get_result()
            ).final_output == "source remains usable"
        async with agent.session(state=saved) as restored:
            responses.respond(assistant_output("restored"))
            assert (
                await (await restored.submit("Use persistent defaults.")).get_result()
            ).final_output == "restored"
            offered = declarations(responses.requests[-1])
            description = offered.get(("functions", "exec"), {}).get("description", "")
            advertised = {
                name
                for name in ("first", "second")
                if ("functions", name) in offered or f"### `{name}`" in description
            }
            expected = (
                {"first", "second"}
                if selection == "omitted"
                else (set() if selection == "empty" else {"first"})
            )
            assert advertised == expected
            # A persisted default is a restriction, not a revoked opening grant.
            responses.respond(assistant_output("explicit"))
            await (
                await restored.submit("Use the opening roster.", tools=[first, second])
            ).get_result()
            explicit = declarations(responses.requests[-1])
            description = explicit.get(("functions", "exec"), {}).get("description", "")
            assert all(
                ("functions", name) in explicit or f"### `{name}`" in description
                for name in ("first", "second")
            )
        assert effects == []
