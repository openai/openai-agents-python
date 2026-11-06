"""Per-session opening declarations own their callback, endpoint and admission ceiling."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

import pytest

from agents import (
    Agent,
    HttpMCP,
    NativeProvider,
    ToolRef,
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
from tests.test_native_mcp_tools import Peer, declarations, mcp_call


async def test_two_openings_bind_independent_callbacks_peers_and_ceilings(
    responses: ResponsesFixture,
) -> None:
    calls: list[str] = []

    @function_tool
    def default_tool() -> str:
        calls.append("default")
        return "default"

    @function_tool(name="lookup")
    def a_lookup(key: str) -> str:
        calls.append("a:" + key)
        return "tenant-a"

    @function_tool(name="lookup")
    def b_lookup(key: str) -> str:
        calls.append("b:" + key)
        return "tenant-b"

    a, b = Peer(label="peer-a"), Peer(label="peer-b")
    async with timeout(25), a.serve() as url_a, b.serve() as url_b:
        mcp_a = HttpMCP(name="records", url=url_a, tools=["search"])
        mcp_b = HttpMCP(name="records", url=url_b, tools=["search"])
        inputs = [a_lookup, mcp_a]
        catalog_b = [b_lookup, mcp_b]
        agent = Agent(
            instructions="Use the tenant's tools.",
            tools=[default_tool],
            provider=provider(responses),
            model="gpt-5.5",
        )
        original = agent.tools
        async with agent.session(tools=inputs) as session_a:
            inputs.clear()
            async with agent.session(tools=catalog_b) as session_b:
                assert b.requests == []
                responses.respond(
                    function_call("lookup", {"key": "one"}, "a-lookup"),
                    mcp_call("search", "mcp__records", "a-mcp"),
                )
                responses.respond(assistant_output("a"))
                assert (await (await session_a.submit("read a")).get_result()).final_output == "a"
                assert tool_output(responses.requests[-1], "a-lookup") == "tenant-a"
                assert [c["_meta"]["callId"] for c in a.calls] == ["a-mcp"]
                assert not b.requests
                with pytest.raises(ValueError, match="captured"):
                    await session_a.submit("foreign", tools=[mcp_b])
                with pytest.raises(ValueError, match="captured"):
                    await session_b.submit("foreign", tools=[default_tool])
                responses.respond(
                    function_call("lookup", {"key": "two"}, "b-lookup"),
                    mcp_call("search", "mcp__records", "b-mcp"),
                )
                responses.respond(assistant_output("b"))
                assert (await (await session_b.submit("read b")).get_result()).final_output == "b"
                assert tool_output(responses.requests[-1], "b-lookup") == "tenant-b"
                assert [c["_meta"]["callId"] for c in b.calls] == ["b-mcp"]
                assert [c["_meta"]["callId"] for c in a.calls] == ["a-mcp"]
                assert agent.tools is original
        assert calls == ["a:one", "b:two"]
        for options in ({}, {"tools": None}):
            responses.respond(function_call("default_tool", {}, "default"))
            responses.respond(assistant_output("default result"))
            async with agent.session(**options) as default_session:
                assert (
                    await (await default_session.submit("read")).get_result()
                ).final_output == "default result"
        assert calls == ["a:one", "b:two", "default", "default"]
        assert agent.tools is original


@pytest.mark.parametrize("empty", [[], ()], ids=["empty-list", "empty-tuple"])
async def test_supplied_empty_override_removes_defaults_and_reserves_no_names(
    responses: ResponsesFixture, empty: Any
) -> None:
    calls: list[str] = []

    @function_tool
    def default_tool() -> str:
        calls.append("default")
        return "default"

    agent = Agent(
        instructions="Use selected tools.", tools=[default_tool], provider=provider(responses)
    )
    async with timeout(15), agent.session(tools=empty) as session:
        responses.respond(function_call("default_tool", {}, "not-admitted"))
        responses.respond(assistant_output("no tools"))
        assert (await (await session.submit("try tool")).get_result()).final_output == "no tools"
        assert ("functions", "default_tool") not in declarations(responses.requests[0])
        assert not calls
        with pytest.raises(ValueError, match="captured"):
            await session.submit("foreign", tools=[default_tool])
    assert agent.tools == (default_tool,)


async def test_invalid_opening_override_fails_before_resource_acquisition(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    acquisitions: list[str] = []

    def acquire_provider(self):
        acquisitions.append("provider")
        raise AssertionError("Invalid declarations must not acquire a provider")

    monkeypatch.setattr(NativeProvider, "_acquire_lease", acquire_provider)
    agent = Agent(instructions="Read.", provider=provider(responses))
    with pytest.raises(ValueError, match="Tool"):
        async with agent.session(tools=[ToolRef("not-admitted")]):
            pytest.fail("A reference cannot declare a new tool at opening")
    assert acquisitions == [] and not responses.requests


async def test_restore_uses_current_override_bindings_and_never_saved_mcp_authority(
    responses: ResponsesFixture,
) -> None:
    calls: list[str] = []

    @function_tool(name="lookup")
    def old_lookup(key: str) -> str:
        calls.append("old:" + key)
        return "old binding"

    @function_tool(name="lookup")
    def new_lookup(key: str) -> str:
        calls.append("current:" + key)
        return "current binding"

    old, current = Peer(label="old receipt"), Peer(label="current receipt")
    async with timeout(30), old.serve() as old_url, current.serve() as current_url:
        agent = Agent(
            instructions="Use selected tools.", model="gpt-5.5", provider=provider(responses)
        )
        old_tools = [old_lookup, HttpMCP(name="records", url=old_url, tools=["search"])]
        current_tools = [new_lookup, HttpMCP(name="records", url=current_url, tools=["search"])]
        responses.respond(
            function_call("lookup", {"key": "before"}, "old-function"),
            mcp_call("search", "mcp__records", "old-mcp"),
        )
        responses.respond(assistant_output("saved"))
        async with agent.session(tools=old_tools) as session:
            assert (await (await session.submit("start")).get_result()).final_output == "saved"
            state = await session.save_state()
            history = [item async for item in session.items()]
        assert old_url not in state.to_json()
        old_request_count = len(old.requests)
        responses.respond(
            function_call("lookup", {"key": "after"}, "new-function"),
            mcp_call("search", "mcp__records", "current-mcp"),
        )
        responses.respond(assistant_output("restored"))
        async with agent.session(state=state, tools=current_tools) as restored:
            assert [item async for item in restored.items()] == history
            assert (
                await (await restored.submit("continue")).get_result()
            ).final_output == "restored"
            assert tool_output(responses.requests[-1], "new-function") == "current binding"
            with pytest.raises(ValueError, match="captured"):
                await restored.submit("old authority", tools=old_tools)
        assert calls == ["old:before", "current:after"]
        assert len(old.requests) == old_request_count
        assert [c["_meta"]["callId"] for c in current.calls] == ["current-mcp"]
        assert agent.tools == ()


@pytest.mark.parametrize("injected", ["none", "sequence", "tuple"])
async def test_owned_deferred_restore_keeps_the_sessions_opening_tools(
    responses: ResponsesFixture, injected: str
) -> None:
    from tests.test_deferred_jobs import Journal, Provider, declared, done
    from tests.test_native_manual_tools import next_request

    journal, backend = Journal(), Provider([done("tenant report")])
    jobs, tool = declared(journal, backend)

    class ScopedAgent(Agent):
        @asynccontextmanager
        async def session(self, **kwargs):
            if injected == "sequence":
                kwargs["tools"] = [tool]
            elif injected == "tuple":
                kwargs["tools"] = (tool,)
            async with super().session(**kwargs) as session:
                yield session

    agent = ScopedAgent(instructions="Render.", provider=provider(responses))
    responses.respond(function_call("render", {"pages": 2}, "tenant-render"))
    responses.respond(assistant_output("completed"))
    async with timeout(20), agent.session(tools=[tool]) as live:
        await live.submit("Render.")
        request = await next_request(live.interactions(), tool)
        async with jobs.start(live, request) as (restored, resumed):
            assert resumed is restored.restored_turn
            assert (await resumed.get_result()).final_output == "completed"
        assert agent.tools == ()
    assert backend.calls == [("start", 2)]
    assert journal.log.count("claim-start") == 1
    assert "tenant report" in tool_output(responses.requests[-1], "tenant-render")


@pytest.mark.parametrize("changed", ["declaration", "added", "omitted"])
async def test_owned_deferred_restore_rejects_new_manual_binding_before_acquiring(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    from tests.test_deferred_jobs import Journal, Provider, declared, done
    from tests.test_native_manual_tools import next_request

    journal, backend = Journal(), Provider([done("tenant report")])
    jobs, registered = declared(journal, backend)
    # An equal-shaped declaration is still a different manual grant owner.
    replacement = replace(registered)
    assert replacement == registered and replacement is not registered
    acquired: list[str] = []
    original = NativeProvider._acquire_lease

    def acquire(connection: NativeProvider):
        acquired.append("provider")
        return original(connection)

    monkeypatch.setattr(NativeProvider, "_acquire_lease", acquire)

    class ScopedAgent(Agent):
        @asynccontextmanager
        async def session(self, **kwargs):
            if kwargs.get("state") is not None:
                if changed == "declaration":
                    kwargs["tools"] = [replacement]
                elif changed == "added":
                    kwargs["tools"] = [registered, replacement]
                else:
                    kwargs["tools"] = []
            async with super().session(**kwargs) as session:
                yield session

    agent = ScopedAgent(instructions="Render.", provider=provider(responses))
    responses.respond(function_call("render", {"pages": 2}, "tenant-render"))
    async with timeout(20), agent.session(tools=[registered]) as live:
        await live.submit("Render.")
        request = await next_request(live.interactions(), registered)
        assert acquired == ["provider"]
        with pytest.raises(ValueError, match="captured opening tools"):
            async with jobs.start(live, request):
                pytest.fail("A different manual grant owner replaced the registered job")
        assert agent.tools == ()
    assert acquired == ["provider"]
    assert backend.calls == []
    assert len(responses.requests) == 1
