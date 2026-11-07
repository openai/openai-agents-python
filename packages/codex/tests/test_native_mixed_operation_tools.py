"""Mixed selections narrow the native step plan across admitted families."""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager

import pytest

from agents import (
    Agent,
    CapabilityError,
    Checklist,
    Environment,
    FunctionTool,
    HttpMCP,
    InteractionExpiredError,
    NativeAgents,
    ToolRef,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_agent import provider
from tests.test_native_child_agents import trust
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_executor_tools import declaration
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_mcp_stdio import assert_stopped, records, stdio
from tests.test_native_mcp_tools import Peer, declarations, mcp_call


async def test_mixed_confirmed_roster_and_varying_python_steps(responses: ResponsesFixture) -> None:
    effects = []
    steps = []

    @function_tool
    def first() -> str:
        effects.append("A")
        return "receipt A"

    @function_tool
    def second() -> str:
        effects.append("B")
        return "receipt B"

    checklist = Checklist()
    peer = Peer()
    async with timeout(20), peer.serve() as url:
        docs = HttpMCP(name="docs", url=url, tools=["search", "excluded"])

        def choose(turn, *, step_index):
            steps.append((turn, step_index))
            # Two equally valid spellings from the same confirmed MCP binding.
            fixed = [checklist, docs.tool("search")]
            return [
                *fixed,
                *([first] if step_index == 0 else [second.ref] if step_index == 1 else []),
            ]

        async with Agent(
            instructions="Select admitted tools",
            tools=[first, second, docs, checklist],
            provider=provider(responses),
        ).session() as session:
            assert not peer.requests
            responses.respond(function_call("first", {}, "a"))
            responses.respond(function_call("second", {}, "b"))
            responses.respond(mcp_call("search", "mcp__docs", "docs"))
            responses.respond(assistant_output("finished"))
            turn = await session.submit(
                "Read", tools=[first, second, docs.tool("search"), checklist], before_step=choose
            )
            assert (await turn.get_result()).final_output == "finished"
            assert [(handle is turn, i) for handle, i in steps] == [(True, i) for i in range(4)]
            assert effects == ["A", "B"]
            assert [call["_meta"]["callId"] for call in peer.calls] == ["docs"]
            fixed = {("functions", "update_plan"), ("mcp__docs", "search")}
            assert [set(declarations(r)) for r in responses.requests] == [
                fixed | {("functions", "first")},
                fixed | {("functions", "second")},
                fixed,
                fixed,
            ]

            responses.respond(assistant_output("opening defaults"))
            await (await session.submit("Next")).get_result()
            assert set(declarations(responses.requests[-1])) == fixed | {
                ("functions", "first"),
                ("functions", "second"),
                ("mcp__docs", "excluded"),
            }


@pytest.mark.parametrize("drop", ["widen_mcp", "different_binding"])
async def test_invalid_step_never_issues_next_provider_request(
    responses: ResponsesFixture, drop: str
) -> None:
    effects = []

    @function_tool
    def first() -> str:
        effects.append("once")
        return "done"

    peer = Peer()
    fixed = Checklist()
    async with timeout(20), peer.serve() as url:
        docs = HttpMCP(name="docs", url=url, tools=["search", "excluded"])

        def choose(turn, *, step_index):
            if step_index == 0:
                return [first, fixed, docs.tool("search")]
            return {
                "native": [docs.tool("search")],
                "mcp": [fixed],
                "widen_mcp": [fixed, docs],
                "different_binding": [fixed, HttpMCP(name="docs", url=url, tools=["search"])],
            }[drop]

        async with Agent(
            instructions="No second request",
            tools=[first, fixed, docs],
            provider=provider(responses),
        ).session() as session:
            responses.respond(function_call("first", {}, "once"))
            # If failure were only a dispatch denial this request would still arrive.
            responses.respond(mcp_call("search", "mcp__docs", "must-not-sample"))
            turn = await session.submit(
                "Read", tools=[first, fixed, docs.tool("search")], before_step=choose
            )
            with pytest.raises(CapabilityError):
                await turn.get_result()
            assert len(responses.requests) == 1
            assert effects == ["once"] and not peer.calls


async def test_refs_cannot_create_native_capability(responses: ResponsesFixture) -> None:
    @function_tool
    def local() -> str:
        return "yes"

    async with (
        timeout(15),
        Agent(
            instructions="Only current bindings", tools=[local], provider=provider(responses)
        ).session() as session,
    ):
        for ref in (
            ToolRef("update_plan", "functions"),
            ToolRef("run", "web"),
            ToolRef("exec_command", "functions"),
            ToolRef("spawn_agent", "collaboration"),
        ):
            with pytest.raises(CapabilityError, match="allowed when the Session opened"):
                await session.submit("No new grants", tools=[local, ref])
        assert not responses.requests


@pytest.mark.parametrize("selection", ["implicit", "captured", "partial-refs"])
async def test_tools_update_retains_only_captured_collaboration_and_selected_functions(
    responses: ResponsesFixture, tmp_path, monkeypatch, selection: str
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

    children = NativeAgents()
    async with timeout(35), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        environment = declaration(executor)
        agent = Agent(
            instructions="Use current selections",
            tools=[first, second, children],
            model="gpt-5.5",
            provider=provider(responses),
        )
        async with agent.session(environment=environment) as session:
            if selection == "captured":
                await session.update(tools=[first, children])
            elif selection == "partial-refs":
                await session.update(tools=[first, ToolRef("spawn_agent", "collaboration")])
            else:
                await session.update(tools=[first])
            responses.respond(assistant_output("without children"))
            await (await session.submit("Omit children", tools=[second, environment])).get_result()
            offered = set(declarations(responses.requests[-1]))
            assert ("functions", "second") in offered
            assert ("collaboration", "spawn_agent") not in offered
            with pytest.raises(CapabilityError, match="captured opening bindings"):
                await session.update(tools=[first, NativeAgents()])
            with pytest.raises(CapabilityError, match="distinct fixed"):
                await session.update(
                    tools=[first, children, ToolRef("spawn_agent", "collaboration")]
                )
            for prompt, tools, name in (
                ("Default", None, "first"),
                ("Temporary", [second, environment, children], "second"),
                ("Default again", None, "first"),
            ):
                start = len(responses.requests)
                responses.respond(function_call(name, {}, prompt))
                responses.respond(assistant_output(prompt))
                result = await (await session.submit(prompt, tools=tools)).get_result()
                assert result.final_output == prompt
                offered = set(declarations(responses.requests[start]))
                assert ("collaboration", "spawn_agent") in offered
                assert ("functions", name) in offered
                omitted = "second" if name == "first" else "first"
                assert ("functions", omitted) not in offered
            assert effects == ["first", "second", "first"]


@pytest.mark.parametrize("reordered", [False, True], ids=["subset", "saved-order"])
@pytest.mark.parametrize("forgery", ["unknown", "duplicate"])
async def test_child_capable_subset_checkpoint_uses_current_full_and_selected_bindings(
    responses: ResponsesFixture, tmp_path, monkeypatch, reordered: bool, forgery: str
) -> None:
    effects = []

    @function_tool
    def first() -> str:
        effects.append("old first")
        return "old"

    @function_tool
    def second() -> str:
        effects.append("second")
        return "second"

    children = NativeAgents()
    async with timeout(35), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        environment = declaration(executor)
        agent = Agent(
            instructions="Current bindings",
            tools=[first, second, children],
            model="gpt-5.5",
            provider=provider(responses),
        )
        async with agent.session(environment=environment) as source:
            responses.respond(function_call("first", {}, "original"))
            responses.respond(assistant_output("settled subset"))
            selection = (
                [second, first, environment, children]
                if reordered
                else [first, environment, children]
            )
            turn = await source.submit("Subset", tools=selection)
            assert (await turn.get_result()).final_output == "settled subset"
            assert effects == ["old first"]
            assert (("functions", "second") in declarations(responses.requests[0])) is reordered
            saved = await source.save_state()
            assert saved.managed_tool_specs is not None
            # Idle state persists the effective default, not the temporary
            # (possibly reordered) roster of the completed turn.
            assert [spec["name"] for spec in saved.managed_tool_specs] == ["first", "second"]

        # Match native name/schema, supply the new binding through the current
        # opening (never through a name or payload from saved state).
        @function_tool(name="first")
        def current() -> str:
            effects.append("current first")
            return "current"

        responses.respond(function_call("second", {}, "new-default"))
        responses.respond(function_call("first", {}, "current-binding"))
        responses.respond(assistant_output("defaults restored"))
        async with agent.session(
            state=saved, tools=[current, second, children], environment=environment
        ) as restored:
            assert (
                await (await restored.submit("Default")).get_result()
            ).final_output == "defaults restored"
            assert effects == ["old first", "second", "current first"]
            assert {("functions", "first"), ("functions", "second")} <= set(
                declarations(responses.requests[-1])
            )
        # Unknown names and altered schemas in the saved projection never
        # bypass native equality or turn saved specs into new grants.
        assert saved.managed_tool_specs is not None
        specs = (
            [{**saved.managed_tool_specs[0], "name": "unadmitted"}]
            if forgery == "unknown"
            else [*saved.managed_tool_specs, saved.managed_tool_specs[0]]
        )
        forged = saved.model_copy(update={"managed_tool_specs": specs})
        before = len(responses.requests)
        with pytest.raises((ValueError, RuntimeError)):
            async with agent.session(
                state=forged, tools=[current, second, children], environment=environment
            ):
                pytest.fail("Saved state invented or duplicated a callback")
        assert len(responses.requests) == before


async def test_update_keeps_separate_native_agent_grant_without_widening_opening(
    responses: ResponsesFixture, tmp_path, monkeypatch
) -> None:
    @function_tool
    def first() -> str:
        return "first"

    @function_tool
    def second() -> str:
        return "second"

    children = NativeAgents()
    async with timeout(35), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        environment = declaration(executor)
        agent = Agent(
            instructions="Current bindings",
            tools=[first, second, children],
            model="gpt-5.5",
            provider=provider(responses),
        )
        async with agent.session(environment=environment) as session:
            await session.update(tools=[first])
            responses.respond(function_call("first", {}, "first-updated"))
            responses.respond(assistant_output("updated"))
            assert (await (await session.submit("Default")).get_result()).final_output == "updated"
            assert tool_output(responses.requests[-1], "first-updated") == "first"
            visible = set(declarations(responses.requests[-1]))
            assert ("functions", "first") in visible
            assert ("functions", "second") not in visible
            assert ("collaboration", "spawn_agent") in visible
            responses.respond(assistant_output("selected function only"))
            await (await session.submit("Select only second", tools=[second])).get_result()
            assert set(declarations(responses.requests[-1])) == {("functions", "second")}
            with pytest.raises(CapabilityError, match="captured opening"):
                await session.update(tools=[first, NativeAgents()])
            responses.respond(assistant_output("explicit"))
            await (
                await session.submit("Full captured grants", tools=[second, environment, children])
            ).get_result()
            assert ("functions", "second") in declarations(responses.requests[-1])

        async with Agent(
            instructions="No collaboration", tools=[first], provider=provider(responses)
        ).session() as session:
            with pytest.raises(CapabilityError, match="allowed when the Session opened"):
                await session.update(tools=[first, ToolRef("spawn_agent", "collaboration")])


@pytest.mark.parametrize("with_children", [False, True])
async def test_managed_environment_selected_manual_restore_keeps_slots_and_lease(
    responses: ResponsesFixture, tmp_path, monkeypatch, with_children: bool
) -> None:
    effects = []
    leases = []

    @function_tool
    def unselected() -> str:
        effects.append("later default")
        return "default result"

    original_manual = FunctionTool(
        name="lookup", description="Look up input", input_type=LookupArgs, output_type=str
    )
    current_manual = FunctionTool(
        name="lookup", description="Look up input", input_type=LookupArgs, output_type=str
    )
    children = NativeAgents() if with_children else None
    async with timeout(45), ExecutorPeer(tmp_path / "remote").serve() as executor:
        trust(executor, tmp_path, monkeypatch)
        environment = (
            declaration(executor)
            if with_children
            else Environment(
                url=executor.url,
                **({"write_policy": "legacy"} if sys.platform == "win32" else {}),
                cwd=str(executor.root),
                headers={"Authorization": "Bearer " + executor.token},
                tools=[],
            )
        )

        @asynccontextmanager
        async def manage(label):
            leases.append(("enter", label))
            try:
                yield environment
            finally:
                leases.append(("exit", label))

        agent = Agent(
            instructions="Preserve owned submission",
            tools=[unselected, original_manual, *([children] if children is not None else [])],
            model="gpt-5.5",
            provider=provider(responses),
        )
        responses.respond(function_call("lookup", {"key": "A"}, "manual-1"))
        retained = [environment, children] if children else [environment]
        async with agent.session(environment=manage("source")) as source:
            assert source.capabilities.get("function_policy").status == "supported"
            turn = await source.submit("Manual", tools=[original_manual, *retained])
            expired = await next_request(source.interactions(), original_manual)
            # Opening support is stable even when children inherit only this
            # selected manual function. The omitted callback is not exposed.
            source.capabilities.require("function_policy")
            assert ("functions", "unselected") not in declarations(responses.requests[-1])
            saved = await source.suspend()
            assert effects == []
        responses.respond(function_call("unselected", {}, "must-not-dispatch"))
        responses.respond(assistant_output("settled original"))
        async with agent.session(
            state=saved,
            tools=[unselected, current_manual, *([children] if children is not None else [])],
            environment=manage("destination"),
        ) as restored:
            assert restored.restored_turn is not None
            assert restored.restored_turn.turn_id == turn.turn_id
            request = await next_request(restored.interactions(), current_manual)
            with pytest.raises(InteractionExpiredError):
                await expired.respond("late")
            await request.respond("from current grant")
            assert (await restored.restored_turn.get_result()).final_output == "settled original"
            assert not effects
            restored.capabilities.require("function_policy")
            responses.respond(function_call("unselected", {}, "new-default"))
            responses.respond(assistant_output("now default"))
            assert (
                await (await restored.submit("Next")).get_result()
            ).final_output == "now default"
            assert effects == ["later default"]
        assert leases == [
            ("enter", "source"),
            ("exit", "source"),
            ("enter", "destination"),
            ("exit", "destination"),
        ]


async def test_dormant_stdio_remains_cold_until_selected_after_pending_restore(
    responses: ResponsesFixture, tmp_path
) -> None:
    manual = FunctionTool(
        name="lookup", description="Wait for an app reply", input_type=LookupArgs, output_type=str
    )
    peer = Peer()
    async with timeout(40), peer.serve() as url:
        remote = HttpMCP(name="docs", url=url, tools=["search", "excluded"])
        source_log, dest_log = tmp_path / "source", tmp_path / "destination"
        source_stdio = stdio(source_log)
        current_stdio = stdio(dest_log)
        agent = Agent(instructions="Admit the current selection", provider=provider(responses))
        async with agent.session(tools=[manual, source_stdio, remote]) as source:
            assert not records(source_log) and not peer.requests
            responses.respond(function_call("lookup", {"key": "A"}, "manual-A"))
            await source.submit("Wait", tools=[manual, remote.tool("search")])
            await next_request(source.interactions(), manual)
            assert not records(source_log) and peer.requests
            saved = await source.suspend()
        responses.respond(mcp_call("search", "mcp__docs", "selected"))
        responses.respond(mcp_call("search", "mcp__local", "unselected"))
        responses.respond(assistant_output("continued"))
        async with agent.session(state=saved, tools=[manual, current_stdio, remote]) as restored:
            assert not records(dest_log)
            active = restored.restored_turn
            assert active is not None
            request = await next_request(restored.interactions(), manual)
            await request.respond("current result")
            assert (await active.get_result()).final_output == "continued"
            assert [c["_meta"]["callId"] for c in peer.calls] == ["selected"]
            assert not any(
                r.get("message", {}).get("method") == "tools/call" for r in records(dest_log)
            )
            assert all(("mcp__local", "search") not in declarations(r) for r in responses.requests)
            responses.respond(mcp_call("search", "mcp__local", "later-default"))
            responses.respond(assistant_output("now default"))
            later = await restored.submit("Use the opening defaults")
            assert (await later.get_result()).final_output == "now default"
            assert records(dest_log) and not records(source_log)
            assert ("mcp__local", "search") in declarations(responses.requests[-2])
            assert [c["_meta"]["callId"] for c in peer.calls] == ["selected"]
            stdio_calls = [
                r["message"]
                for r in records(dest_log)
                if r.get("message", {}).get("method") == "tools/call"
            ]
            assert [c["params"]["name"] for c in stdio_calls] == ["search"]
        await assert_stopped(dest_log)


async def test_code_mode_reserved_refs_retain_real_exec_and_exclude_unselected_python(
    responses: ResponsesFixture,
) -> None:
    effects = []

    @function_tool
    def lookup() -> str:
        effects.append("lookup")
        return "allowed"

    @function_tool
    def hidden() -> str:
        effects.append("hidden")
        return "never"

    async with (
        timeout(35),
        Agent(
            instructions="Honor original operation slots",
            tools=[lookup, hidden],
            programmatic_tool_calling=True,
            provider=provider(responses),
        ).session() as session,
    ):
        responses.respond(
            code_call("text(JSON.stringify([await tools.lookup({}), typeof tools.hidden]));", "one")
        )
        responses.respond(assistant_output("done"))
        await (await session.submit("Allowed", tools=[lookup])).get_result()
        offered = set(declarations(responses.requests[0]))
        assert {("functions", "exec"), ("functions", "wait")} <= offered
        assert ("functions", "hidden") not in offered
        result = code_output(responses.requests[1], "one")
        assert "allowed" in result and "undefined" in result and effects == ["lookup"]
