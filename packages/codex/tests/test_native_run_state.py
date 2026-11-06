"""One-shot and explicit Session state stays within supported boundaries."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from agents import (
    Agent,
    FunctionTool,
    HttpMCP,
    InteractionRequired,
    LocalStorage,
    NativeAgents,
    Session,
    SessionState,
    ToolContext,
    ToolRef,
    WebSearch,
    function_tool,
)
from tests._asyncio import timeout
from tests.native_fixtures import (
    assistant_output,
    function_call,
    responses as responses,
    tool_output,
)
from tests.test_native_manual_tools import LookupArgs, next_request, provider
from tests.test_native_mcp_tools import Peer, declarations, mcp_call
from tests.test_native_web_search import search_peer

ENTRIES = ["agent", "agent_sync"]


async def run(entry, agent, input, **kwargs):
    if entry == "agent":
        return await agent.run(input, **kwargs)
    return await asyncio.to_thread(agent.run_sync, input, **kwargs)


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize("selection", ["empty", "callback", "manual"])
async def test_run_defers_manual_decision_until_step_selection(responses, entry, selection):
    effects = []
    selected_steps = []
    lookup = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)

    @function_tool
    async def record(key: str) -> str:
        effects.append(key)
        return "recorded:" + key

    agent = Agent(
        instructions="Use only the selected tool.",
        tools=[record, lookup],
        provider=provider(responses),
    )

    def choose(turn, *, step_index):
        selected_steps.append(step_index)
        return {"empty": [], "callback": [record], "manual": [lookup]}[selection]

    if selection == "callback":
        responses.respond(function_call("record", {"key": "A"}, "effect"))
        responses.respond(assistant_output("done"))
    elif selection == "manual":
        responses.respond(function_call("lookup", {"key": "A"}, "manual-selected"))
    else:
        responses.respond(assistant_output("done"))

    async with timeout(20):
        if selection == "manual":
            with pytest.raises(InteractionRequired, match="on_interaction") as needed:
                await run(entry, agent, "Read A", before_step=choose)
            assert needed.value.state is None
        else:
            result = await run(entry, agent, "Read A", before_step=choose)
            assert result.final_output == "done"

    assert selected_steps == ([0, 1] if selection == "callback" else [0])
    first = declarations(responses.requests[0])
    assert (("functions", "lookup") in first) is (selection == "manual")
    assert (("functions", "record") in first) is (selection == "callback")
    assert effects == (["A"] if selection == "callback" else [])
    assert len(responses.requests) == (2 if selection == "callback" else 1)
    if selection == "callback":
        assert tool_output(responses.requests[-1], "effect") == "recorded:A"


def manual_agent(responses):
    lookup = FunctionTool(
        name="lookup", description="Read a saved record", input_type=LookupArgs, output_type=str
    )
    return Agent(instructions="Read the record.", tools=[lookup], provider=provider(responses))


@pytest.mark.parametrize("entry", ENTRIES)
@pytest.mark.parametrize(
    ("input", "options", "reason"),
    [
        (None, {}, "input"),
    ],
)
async def test_one_shot_rejects_missing_input_before_open(entry, input, options, reason):
    agent = Agent(instructions="")

    def unexpected_open(**kwargs):
        pytest.fail("invalid one-shot input acquired a Session")

    agent.session = unexpected_open
    with pytest.raises(TypeError, match=reason):
        await run(entry, agent, input, **options)


@pytest.mark.parametrize(
    ("entry", "selection"),
    [
        ("agent", "default"),
        ("agent_sync", "default"),
        ("agent", "declarations"),
        ("agent_sync", "declarations"),
        ("agent", "references"),
        ("agent_sync", "references"),
    ],
)
async def test_one_shot_initial_manual_pause_and_current_handler_resume(
    responses, entry, selection
):
    agent = manual_agent(responses)
    effects = []

    @function_tool
    def record(ctx: ToolContext[dict[str, str]], key: str) -> str:
        effects.append((ctx.context["owner"], key))
        return ctx.context["owner"]

    agent = replace(agent, tools=(*agent.tools, record))
    responses.respond(
        function_call("record", {"key": "before"}, "effect"),
        function_call("lookup", {"key": "A"}, "manual"),
    )
    options = {}
    if selection == "declarations":
        options["tools"] = agent.tools
    elif selection == "references":
        options["tools"] = [tool.ref for tool in agent.tools]
    async with timeout(20):
        with pytest.raises(InteractionRequired) as needed:
            await run(entry, agent, "read", context={"owner": "original"}, **options)
        assert needed.value.state is not None
        state = SessionState.from_json(needed.value.state.to_json())
        assert effects == [("original", "before")]
        assert len(responses.requests) == 1
        seen = []

        async def answer(request):
            seen.append((request.turn_id, request.call_id, request.arguments.key))
            return request.reply("saved answer")

        responses.respond(function_call("record", {"key": "after"}, "current-effect"))
        responses.respond(assistant_output("continued"))
        result = await run(
            entry,
            agent,
            None,
            state=state,
            context={"owner": "current"},
            on_interaction=answer,
            return_state=True,
        )
    assert result.final_output == "continued"
    assert result.turn.turn_id == state.native_continuation.turn_id
    assert result.state is not None and result.state.native_continuation is None
    assert seen == [(state.native_continuation.turn_id, "manual", "A")]
    assert effects == [("original", "before"), ("current", "after")]
    assert tool_output(responses.requests[1], "manual") == "saved answer"
    assert tool_output(responses.requests[1], "effect") == "original"
    assert tool_output(responses.requests[2], "current-effect") == "current"
    assert sum(item.get("role") == "user" for item in responses.requests[-1]["input"]) == 1
    assert len(responses.requests) == 3


@pytest.mark.parametrize("entry", ENTRIES)
async def test_selected_manual_can_complete_without_requesting_it(responses, entry):
    agent = manual_agent(responses)
    responses.respond(assistant_output("nothing needed"))
    async with timeout(15):
        result = await run(entry, agent, "No lookup needed", return_state=True)
    assert result.final_output == "nothing needed"
    assert result.state is not None and result.state.native_continuation is None
    assert len(responses.requests) == 1


@pytest.mark.parametrize("selection", ["declaration", "reference"])
async def test_one_shot_manual_subset_keeps_declared_mcp_dormant(responses, selection):
    agent = manual_agent(responses)
    manual = agent.tools[0]
    peer = Peer()
    async with timeout(20), peer.serve() as url:
        server = HttpMCP(name="docs", url=url, tools=["search"])
        agent = replace(agent, tools=(manual, server))
        selected = manual if selection == "declaration" else manual.ref
        responses.respond(function_call("lookup", {"key": "A"}, "manual"))
        with pytest.raises(InteractionRequired) as needed:
            await agent.run("Read a saved record", tools=[selected])
        assert needed.value.state is not None
        state = SessionState.from_json(needed.value.state.to_json())
        assert len(responses.requests) == 1 and not peer.requests

        async def answer(request):
            assert request.call_id == "manual" and request.arguments.key == "A"
            return request.reply("saved answer")

        responses.respond(mcp_call("search", "mcp__docs", "unselected"))
        responses.respond(assistant_output("continued"))
        result = await agent.run(None, state=state, on_interaction=answer)
        assert result.final_output == "continued"
        assert tool_output(responses.requests[1], "manual") == "saved answer"
        assert not peer.requests and not peer.calls
        assert all(("mcp__docs", "search") not in declarations(row) for row in responses.requests)


@pytest.mark.parametrize("selection", ["declaration", "reference"])
@pytest.mark.parametrize("outcome", ["complete", "pause"])
async def test_one_shot_restored_idle_manual_default_keeps_mcp_dormant(
    responses, selection, outcome
):
    agent = manual_agent(responses)
    manual = agent.tools[0]
    peer = Peer()
    async with timeout(20), peer.serve() as url:
        server = HttpMCP(name="docs", url=url, tools=["search"])
        agent = replace(agent, tools=(manual, server))
        selected = manual if selection == "declaration" else manual.ref
        async with agent.session() as session:
            await session.update(tools=[selected])
            state = SessionState.from_json((await session.save_state()).to_json())
        assert not responses.requests and not peer.requests and not peer.calls
        if outcome == "complete":
            responses.respond(assistant_output("nothing needed"))
            result = await agent.run("No lookup needed", state=state)
            assert result.final_output == "nothing needed"
        else:
            responses.respond(function_call("lookup", {"key": "A"}, "manual"))
            with pytest.raises(InteractionRequired) as needed:
                await agent.run("Read a record", state=state)
            assert needed.value.state is not None
            assert needed.value.state.native_continuation is not None
        assert len(responses.requests) == 1
        assert not peer.requests and not peer.calls
        assert ("mcp__docs", "search") not in declarations(responses.requests[0])


@pytest.mark.parametrize("selection", ["default", "server", "leaf"])
async def test_one_shot_manual_and_selected_mcp_refuse_before_resources(responses, selection):
    agent = manual_agent(responses)
    manual = agent.tools[0]
    peer = Peer()
    async with timeout(15), peer.serve() as url:
        server = HttpMCP(name="docs", url=url, tools=["search"])
        agent = replace(agent, tools=(manual, server))
        options = {}
        if selection == "server":
            options["tools"] = [manual, server]
        elif selection == "leaf":
            options["tools"] = [manual.ref, server.tool("search")]
        with pytest.raises(InteractionRequired, match="on_interaction") as needed:
            await agent.run("Read the record", **options)
        assert needed.value.state is None
        assert not responses.requests and not peer.requests and not peer.calls


@pytest.mark.parametrize("route", ["standalone", "hosted"])
@pytest.mark.parametrize("selection", ["default", "declarations", "references"])
@pytest.mark.parametrize("opening", ["declared", "delegated"])
async def test_manual_web_opening_refuses_auto_pause_before_work_and_still_runs_live(
    route, selection, opening
):
    async with timeout(25), search_peer() as (model, searches):
        agent = manual_agent(model)
        lookup = agent.tools[0]
        web = WebSearch(route=route)
        agent = replace(agent, model="gpt-5.5")
        if opening == "declared":
            agent = replace(agent, tools=(lookup, web))
        else:
            original_session = agent.session

            @asynccontextmanager
            async def factory(**kwargs):
                async with original_session(tools=[lookup, web], **kwargs) as session:
                    yield session

            agent.session = factory
        options = {}
        if selection == "declarations":
            options["tools"] = [lookup, web]
        elif selection == "references":
            web_ref = (
                ToolRef("run", "web")
                if route == "standalone"
                else ToolRef("web_search", "functions")
            )
            options["tools"] = [lookup.ref, web_ref]
        model.respond(function_call("lookup", {"key": "A"}, "manual"))
        model.respond(assistant_output("live completed"))
        with pytest.raises(InteractionRequired) as needed:
            await agent.run("Read the record", **options)
        assert not model.requests and not searches
        assert needed.value.state is None
        assert "on_interaction" in str(needed.value)

        async def answer(request):
            assert request.call_id == "manual"
            return request.reply("live answer")

        result = await agent.run("Read the record", on_interaction=answer, **options)
        assert result.final_output == "live completed"
        assert tool_output(model.requests[1], "manual") == "live answer"
        assert len(model.requests) == 2 and not searches


@pytest.mark.parametrize("entry", ENTRIES)
async def test_manual_storage_override_refuses_before_work_but_live_and_unselected_work(
    responses, tmp_path, entry
):
    effects = []
    agent = manual_agent(responses)

    @function_tool
    def record(key: str) -> str:
        effects.append(key)
        return "recorded"

    agent = replace(agent, tools=(*agent.tools, record))
    original_session = agent.session
    closed = []

    @asynccontextmanager
    async def opening(**kwargs):
        async with original_session(
            storage=LocalStorage(tmp_path / "profile"), **kwargs
        ) as session:
            try:
                yield session
            finally:
                closed.append(session.id)

    agent.session = opening
    responses.respond(
        function_call("record", {"key": "before"}, "effect"),
        function_call("lookup", {"key": "A"}, "manual"),
    )
    responses.respond(assistant_output("live completed"))
    async with timeout(20):
        with pytest.raises(InteractionRequired, match="on_interaction") as needed:
            await run(entry, agent, "read")
        assert needed.value.state is None
        assert not responses.requests and not effects
        assert len(closed) == 1

        async def answer(request):
            assert request.call_id == "manual"
            return request.reply("live answer")

        result = await run(entry, agent, "read", on_interaction=answer)
        assert result.final_output == "live completed"
        assert effects == ["before"]
        assert tool_output(responses.requests[1], "manual") == "live answer"
        responses.respond(assistant_output("no manual selection"))
        result = await run(entry, agent, "read", tools=[])
        assert result.final_output == "no manual selection"
        assert len(responses.requests) == 3 and len(closed) == 3
        assert effects == ["before"]


@pytest.mark.parametrize("replacement_name", ["lookup", "other"])
@pytest.mark.parametrize("requested", [False, True])
async def test_one_shot_delegated_replacement_has_no_original_pause_authority(
    responses, tmp_path, replacement_name, requested
):
    agent = manual_agent(responses)
    original = agent.tools[0]
    # Even an equal declaration is not the exact captured object matched by
    # FunctionTool.match(). It must not inherit the original's pause authority.
    replacement = replace(original, name=replacement_name)
    original_session = agent.session
    closed = []

    @asynccontextmanager
    async def opening(**kwargs):
        async with original_session(
            tools=[replacement], storage=LocalStorage(tmp_path / "profile"), **kwargs
        ) as session:
            try:
                yield session
            finally:
                closed.append(session.id)

    agent.session = opening
    async with timeout(20):
        if requested:
            responses.respond(function_call(replacement_name, {"key": "A"}, "replacement"))
            with pytest.raises(InteractionRequired) as needed:
                await agent.run("Read a record")
            assert needed.value.state is None
        else:
            responses.respond(assistant_output("nothing needed"))
            result = await agent.run("No lookup needed")
            assert result.final_output == "nothing needed"
        assert len(responses.requests) == 1
        assert len(closed) == 1


@pytest.mark.parametrize("entry", ENTRIES)
async def test_manual_session_override_selected_mcp_refuses_before_submission(responses, entry):
    agent = manual_agent(responses)
    original_session = agent.session
    closed = []
    peer = Peer()
    async with timeout(15), peer.serve() as url:
        server = HttpMCP(name="docs", url=url, tools=["search"])

        @asynccontextmanager
        async def opening(**kwargs):
            async with original_session(tools=[*agent.tools, server], **kwargs) as session:
                try:
                    yield session
                finally:
                    closed.append(session.id)

        agent.session = opening
        # On the broken owner this request can produce an unsupported manual
        # checkpoint after starting the selected MCP server.
        responses.respond(function_call("lookup", {"key": "A"}, "manual"))
        with pytest.raises(InteractionRequired, match="on_interaction") as needed:
            await run(entry, agent, "Read the record")
        assert needed.value.state is None
        assert len(closed) == 1
        assert not responses.requests and not peer.requests and not peer.calls


@pytest.mark.parametrize("selection", ["explicit", "default"])
async def test_manual_session_override_unselected_mcp_still_pauses(responses, selection):
    agent = manual_agent(responses)
    manual = agent.tools[0]
    original_session = agent.session
    closed = []
    peer = Peer()
    async with timeout(15), peer.serve() as url:
        server = HttpMCP(name="docs", url=url, tools=["search"])

        @asynccontextmanager
        async def opening(**kwargs):
            async with original_session(tools=[manual, server], **kwargs) as session:
                if selection == "default":
                    await session.update(tools=[manual.ref])
                try:
                    yield session
                finally:
                    closed.append(session.id)

        agent.session = opening
        responses.respond(function_call("lookup", {"key": "A"}, "manual"))
        options = {"tools": [manual.ref]} if selection == "explicit" else {}
        with pytest.raises(InteractionRequired) as needed:
            await agent.run("Read the record", **options)
        assert needed.value.state is not None
        assert len(responses.requests) == 1 and len(closed) == 1
        assert not peer.requests and not peer.calls
        assert ("mcp__docs", "search") not in declarations(responses.requests[0])


@pytest.mark.parametrize("entry", ENTRIES)
async def test_manual_with_native_children_refuses_before_open(responses, entry):
    agent = manual_agent(responses)
    agent = replace(agent, tools=(*agent.tools, NativeAgents()))

    def unexpected_open(**kwargs):
        pytest.fail("unsupported manual with native children acquired a Session")

    agent.session = unexpected_open
    with pytest.raises(InteractionRequired, match="on_interaction") as needed:
        await run(entry, agent, "read")
    assert needed.value.state is None
    assert responses.requests == []


async def test_one_shot_unselected_manual_does_not_need_a_handler(responses):
    effects = []
    agent = manual_agent(responses)

    @function_tool
    def record(key: str) -> str:
        effects.append(key)
        return "recorded:" + key

    agent = replace(agent, tools=(*agent.tools, record))
    responses.respond(function_call("record", {"key": "selected"}, "effect"))
    responses.respond(assistant_output("done"))
    async with timeout(15):
        result = await agent.run("Record only", tools=[record.ref])
    assert result.final_output == "done"
    assert effects == ["selected"]
    assert tool_output(responses.requests[1], "effect") == "recorded:selected"


async def test_late_manual_from_session_override_fails_and_joins_without_saving_or_replay(
    responses, monkeypatch
):
    effects = []
    closed = []

    @function_tool
    async def record(key: str) -> str:
        effects.append(key)
        return "recorded:" + key

    lookup = FunctionTool(name="lookup", description="Read", input_type=LookupArgs, output_type=str)
    agent = Agent(instructions="Record, then read.", tools=[record], provider=provider(responses))
    original_session = agent.session

    @asynccontextmanager
    async def opening(**kwargs):
        async with original_session(tools=[record, lookup], **kwargs) as session:
            try:
                yield session
            finally:
                closed.append(session.id)

    async def no_checkpoint(self):
        pytest.fail("one-shot run tried to create a checkpoint")

    monkeypatch.setattr(Session, "save_state", no_checkpoint)
    monkeypatch.setattr(Session, "suspend", no_checkpoint)
    agent.session = opening
    responses.respond(function_call("record", {"key": "original"}, "effect"))
    responses.respond(function_call("lookup", {"key": "A"}, "manual"))
    async with timeout(20):
        with pytest.raises(InteractionRequired, match="on_interaction") as needed:
            await agent.run("Record and read")
        assert needed.value.state is None
        assert effects == ["original"] and len(closed) == 1
        assert len(responses.requests) == 2
        assert tool_output(responses.requests[1], "effect") == "recorded:original"

        responses.respond(assistant_output("fresh"))
        result = await agent.run("Start new work")
    assert result.final_output == "fresh" and result.state is None
    assert len(closed) == 2 and closed[0] != closed[1]
    assert effects == ["original"] and len(responses.requests) == 3
    assert "recorded:original" not in json.dumps(responses.requests[2]["input"])


@pytest.mark.parametrize("sample", ["late_only", "declared_first", "late_first"])
async def test_eligible_manual_does_not_authorize_a_late_pending_grant(responses, sample):
    agent = manual_agent(responses)
    late = FunctionTool(
        name="late", description="Read a late record", input_type=LookupArgs, output_type=str
    )
    original_session = agent.session
    closed = []

    @asynccontextmanager
    async def opening(**kwargs):
        async with original_session(tools=[*agent.tools, late], **kwargs) as session:
            try:
                yield session
            finally:
                closed.append(session.id)

    agent.session = opening
    original = function_call("lookup", {"key": "A"}, "eligible")
    extra = function_call("late", {"key": "B"}, "undeclared")
    if sample == "late_only":
        responses.respond(extra)
    elif sample == "declared_first":
        responses.respond(original, extra)
    else:
        responses.respond(extra, original)
    async with timeout(15):
        with pytest.raises(InteractionRequired, match="on_interaction") as needed:
            await agent.run("read")
    assert needed.value.state is None
    assert len(responses.requests) == 1 and len(closed) == 1


async def test_live_reply_during_automatic_settlement_preserves_surviving_turn(
    responses, monkeypatch
):
    entered = asyncio.Event()
    release = asyncio.Event()
    settling = asyncio.Event()
    opened = asyncio.Event()
    effects = []
    observed = []
    closed = []

    @function_tool
    async def record(key: str) -> str:
        entered.set()
        await release.wait()
        effects.append(key)
        return "recorded"

    agent = manual_agent(responses)
    agent = replace(agent, tools=(*agent.tools, record))
    original_session = agent.session
    original_wait = Session._wait_manual_boundary

    # Signal the actual native settlement await: the observer must answer
    # after the run attempts capture, while an automatic callback is held.
    async def observe_wait(self, state, *, require_request=False):
        settling.set()
        return await original_wait(self, state, require_request=require_request)

    @asynccontextmanager
    async def observe_session(**kwargs):
        async with original_session(**kwargs) as session:
            observed.append(session)
            opened.set()
            try:
                yield session
            finally:
                closed.append(session.id)

    monkeypatch.setattr(agent, "session", observe_session)
    monkeypatch.setattr(Session, "_wait_manual_boundary", observe_wait)
    responses.respond(
        function_call("record", {"key": "once"}, "effect"),
        function_call("lookup", {"key": "A"}, "manual"),
    )
    responses.respond(assistant_output("completed without pause"))
    async with timeout(15):
        running = asyncio.create_task(agent.run("read both", return_state=True))
        try:
            await opened.wait()
            await entered.wait()
            await settling.wait()
            assert not running.done()
            cursor = observed[0].interactions()
            request = await anext(cursor)
            await request.respond("cursor answer")
            assert not running.done()
            release.set()
            result = await running
            await cursor.aclose()
        finally:
            release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    assert result.final_output == "completed without pause"
    assert result.state is not None and result.state.native_continuation is None
    assert effects == ["once"] and len(closed) == 1
    assert len(responses.requests) == 2
    assert tool_output(responses.requests[1], "manual") == "cursor answer"
    assert tool_output(responses.requests[1], "effect") == "recorded"


@pytest.mark.parametrize("failure", ["callback_timeout", "body_error"])
async def test_manual_settlement_keeps_joined_turn_failure_primary(responses, monkeypatch, failure):
    entered = asyncio.Event()
    settling = asyncio.Event()
    boundary_finished = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    issued = []
    effects = []
    original_wait = Session._wait_manual_boundary

    # Signal when the actual manual settlement starts, after native admission.
    async def observe_wait(self, state, *, require_request=False):
        issued.append(state.handle)
        settling.set()
        try:
            return await original_wait(self, state, require_request=require_request)
        finally:
            boundary_finished.set()

    monkeypatch.setattr(Session, "_wait_manual_boundary", observe_wait)

    @function_tool(callback_timeout=0.5 if failure == "callback_timeout" else None)
    async def record(key: str) -> str:
        entered.set()
        try:
            await settling.wait()
            if failure == "body_error":
                raise ValueError("record failed")
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
        effects.append(key)
        return "unexpected"

    agent = manual_agent(responses)
    agent = replace(agent, tools=(*agent.tools, record))
    responses.respond(
        function_call("record", {"key": "slow"}, "auto"),
        function_call("lookup", {"key": "A"}, "manual"),
    )
    expected = TimeoutError if failure == "callback_timeout" else ValueError
    async with timeout(15):
        running = asyncio.create_task(agent.run("try both", return_state=True))
        try:
            await entered.wait()
            await settling.wait()
            await cleaning.wait()
            if failure == "callback_timeout":
                # Timeout publishes the Turn failure before joining cleanup.
                # Hold that join until automatic suspension has seen the failure.
                await boundary_finished.wait()
            assert not running.done()
            release.set()
            with pytest.raises(expected, match="record") as run_error:
                await running
            with pytest.raises(expected, match="record") as turn_error:
                await issued[0].get_result()
            assert run_error.value is turn_error.value
            assert getattr(run_error.value, "state", None) is None
        finally:
            release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    assert not effects
    assert len(responses.requests) == 1


async def test_explicit_idle_session_restores_conversation_with_new_input(responses):
    agent = Agent(instructions="Keep the saved context.", provider=provider(responses))
    responses.respond(assistant_output("remembered"))
    async with timeout(20):
        async with agent.session() as session:
            first = await (await session.submit("Remember record A")).get_result()
            assert first.state is None
            assert "Remember record A" not in repr(first)
            state = SessionState.from_json((await session.save_state()).to_json())
        responses.respond(assistant_output("record A"))
        async with agent.session(state=state) as restored:
            result = await (await restored.submit("Which record?")).get_result()
    assert result.final_output == "record A"
    assert result.state is None
    rendered = json.dumps(responses.requests[-1]["input"])
    assert "Remember record A" in rendered
    assert "remembered" in rendered
    assert "Which record?" in rendered
    assert len(responses.requests) == 2


async def test_explicit_restore_preserves_completed_effects_and_uses_current_context(responses):
    effects = []

    @function_tool
    def remember(ctx: ToolContext[dict[str, str]], key: str) -> str:
        effects.append((ctx.context["owner"], key))
        return ctx.context["owner"]

    agent = manual_agent(responses)
    lookup = agent.tools[0]
    agent = replace(agent, tools=(*agent.tools, remember))
    responses.respond(
        function_call("remember", {"key": "before"}, "completed-call"),
        function_call("lookup", {"key": "A"}, "saved-call"),
    )
    async with timeout(20):
        async with agent.session(context={"owner": "original"}) as source:
            original = await source.submit("Read A")
            await next_request(source.interactions(), lookup)
            state = SessionState.from_json((await source.suspend()).to_json())
            with pytest.raises(InteractionRequired) as sealed:
                await original.get_result()
            assert sealed.value.state is not None
            assert sealed.value.state.to_json() == state.to_json()
        assert effects == [("original", "before")]
        responses.respond(function_call("remember", {"key": "after"}, "new-call"))
        responses.respond(assistant_output("complete"))
        async with agent.session(state=state, context={"owner": "current"}) as restored:
            request = await next_request(restored.interactions(), lookup)
            assert request.call_id == "saved-call"
            await request.respond("accepted")
            result = await restored.restored_turn.get_result()
            assert (await restored.save_state()).format_version == 4
    assert result.final_output == "complete"
    assert result.turn.turn_id == original.turn_id
    assert result.state is None
    assert effects == [("original", "before"), ("current", "after")]
    assert tool_output(responses.requests[1], "completed-call") == "original"
    assert tool_output(responses.requests[2], "new-call") == "current"
    assert len(responses.requests) == 3
