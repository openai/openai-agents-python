"""Application-owned MCP selection through the native Session."""

from __future__ import annotations

import asyncio
import threading
from contextlib import AsyncExitStack

import pytest

from agents import (
    Agent,
    Allow,
    CapabilityError,
    Deny,
    FunctionTool,
    HttpMCP,
    ToolContext,
    ToolRef,
    TurnStoppedError,
    function_tool,
)
from agents.classic import tool_input_guardrail
from agents.classic.mcp import MCPServerStreamableHttp, create_static_tool_filter
from agents.migration.mcp import MCPNamespace, select_mcp_tools
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
from tests.test_native_sync_tools import HeldInterrupt


async def test_application_selects_one_project_before_opening_session(
    responses: ResponsesFixture,
) -> None:
    selected = Peer(
        call_result={
            "content": [
                {
                    "type": "resource",
                    "resource": {
                        "uri": "https://example.invalid/record",
                        "text": "project-a-receipt",
                        "_meta": {"private": "resource-private-marker"},
                    },
                    "_meta": {"private": "block-private-marker"},
                }
            ],
            "_meta": {"private": "result-private-marker"},
        }
    )
    unselected = Peer(label="project-b-receipt")
    context = {"project": "a"}
    auth_attempts = []

    async with (
        timeout(30),
        selected.serve() as selected_url,
        unselected.serve() as unselected_url,
        AsyncExitStack() as connections,
    ):
        chosen = MCPServerStreamableHttp(
            params={"url": selected_url, "headers": {"Authorization": "Bearer synthetic-project-a"}}
        )
        other = MCPServerStreamableHttp(params={"url": unselected_url})

        async def authorize_chosen(application):
            auth_attempts.append(application["project"])
            await connections.enter_async_context(chosen)

        async def authorize_other(application):
            pytest.fail("An unselected namespace attempted authentication")

        projects = {
            "a": MCPNamespace("project_a", chosen, authenticate=authorize_chosen),
            "b": MCPNamespace("project_b", other, authenticate=authorize_other),
        }
        assert not auth_attempts and not selected.requests and not unselected.requests
        tools = await select_mcp_tools(
            projects[context["project"]], context=context, tool_names=["search"]
        )
        assert auth_attempts == ["a"]
        assert [tool.ref for tool in tools] == [ToolRef("search", namespace="project_a")]
        assert selected.calls == []
        assert unselected.requests == []

        responses.respond(mcp_call("excluded", "project_a", "excluded"))
        responses.respond(mcp_call("search", "project_b", "unselected"))
        responses.respond(mcp_call("search", "project_a", "allowed"))
        responses.respond(assistant_output("done"))
        agent = Agent(
            instructions="Search for the record.", provider=provider(responses), tools=tools
        )
        async with agent.session(context=context) as session:
            assert (await (await session.submit("Read record")).get_result()).final_output == "done"

        request, *_, continuation = responses.requests
        assert set(declarations(request)) == {("project_a", "search")}
        assert declarations(request)["project_a", "search"]["parameters"] == {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        }
        assert "project-a-receipt" in tool_output(continuation, "allowed")
        assert "private-marker" not in str(continuation)
        assert selected.calls == [{"name": "search", "arguments": {"key": "43"}}]
        assert all(header == "Bearer synthetic-project-a" for header in selected.auth)
        assert unselected.requests == []
        assert {tool.name for tool in await chosen.list_tools()} == {"search", "excluded"}


async def test_namespaced_tools_keep_distinct_bindings_through_selection_and_restore(
    responses: ResponsesFixture,
) -> None:
    original_first = Peer(label="original-first-peer")
    original_second = Peer(label="original-second-peer")
    current_first = Peer(label="current-first-peer")
    current_second = Peer(label="current-second-peer")
    native_provider = provider(responses)

    @function_tool(name="search")
    def search(key: str) -> str:
        return "ordinary-python-tool:" + key

    responses.respond(mcp_call("search", "first", "first-call"))
    responses.respond(assistant_output("first done"))
    responses.respond(mcp_call("search", "second", "second-call"))
    responses.respond(assistant_output("second done"))
    responses.respond(function_call("search", {"key": "43"}, "python-call"))
    responses.respond(assistant_output("python done"))

    async with (
        timeout(30),
        original_first.serve() as original_first_url,
        original_second.serve() as original_second_url,
    ):
        async with (
            MCPServerStreamableHttp(params={"url": original_first_url}) as first_client,
            MCPServerStreamableHttp(params={"url": original_second_url}) as second_client,
        ):
            source_context = object()
            first_tools = await select_mcp_tools(
                MCPNamespace("first", first_client), context=source_context, tool_names=["search"]
            )
            second_tools = await select_mcp_tools(
                MCPNamespace("second", second_client), context=source_context, tool_names=["search"]
            )
            source = Agent(
                instructions="Read the record.",
                provider=native_provider,
                tools=[search, *first_tools, *second_tools],
            )
            with pytest.raises(ValueError, match="application context"):
                async with source.session(context=object()):
                    pytest.fail("A different context opened a Session with these bindings")

            async with source.session(context=source_context) as session:
                turn = await session.submit("First record", tools=[ToolRef("search", "first")])
                assert (await turn.get_result()).final_output == "first done"
                await session.update(tools=second_tools)
                state = await session.save_state()

        original_request_counts = (len(original_first.requests), len(original_second.requests))
        auth_header = "Bearer synthetic-current-second"
        async with (
            current_first.serve() as first_url,
            current_second.serve() as second_url,
            MCPServerStreamableHttp(params={"url": first_url}) as first_client,
            MCPServerStreamableHttp(
                params={"url": second_url, "headers": {"Authorization": auth_header}}
            ) as second_client,
        ):
            current_context = object()
            first_tools = await select_mcp_tools(
                MCPNamespace("first", first_client), context=current_context, tool_names=["search"]
            )
            second_tools = await select_mcp_tools(
                MCPNamespace("second", second_client),
                context=current_context,
                tool_names=["search"],
            )
            destination = Agent(
                instructions=source.instructions,
                provider=native_provider,
                tools=[search, *first_tools, *second_tools],
            )
            async with destination.session(context=current_context, state=state) as session:
                turn = await session.submit("Second record")
                assert (await turn.get_result()).final_output == "second done"
                turn = await session.submit("Python record", tools=[search.ref])
                assert (await turn.get_result()).final_output == "python done"

        assert (
            len(original_first.requests),
            len(original_second.requests),
        ) == original_request_counts

    assert set(declarations(responses.requests[0])) == {("first", "search")}
    assert "original-first-peer" in tool_output(responses.requests[1], "first-call")
    assert set(declarations(responses.requests[2])) == {("second", "search")}
    assert "current-second-peer" in tool_output(responses.requests[3], "second-call")
    assert set(declarations(responses.requests[4])) == {("functions", "search")}
    assert "ordinary-python-tool:43" in tool_output(responses.requests[5], "python-call")
    assert (
        original_first.calls
        == current_second.calls
        == [{"name": "search", "arguments": {"key": "43"}}]
    )
    assert not original_second.calls and not current_first.calls
    assert all(header == auth_header for header in current_second.auth)


async def test_empty_and_filtered_selections_do_not_install_tools() -> None:
    peer = Peer()
    auth_attempts = []

    async def authorize(context):
        auth_attempts.append(context)

    async with timeout(10), peer.serve() as url:
        client = MCPServerStreamableHttp(
            params={"url": url},
            tool_filter=create_static_tool_filter(allowed_tool_names=["search"]),
        )
        namespace = MCPNamespace("filtered", client, authenticate=authorize)
        assert await select_mcp_tools(namespace, context=None, tool_names=[]) == ()
        assert not auth_attempts and not peer.requests
        async with client:
            with pytest.raises(ValueError, match="filtered out"):
                await select_mcp_tools(namespace, context=None, tool_names=["excluded"])
            assert [tool.name for tool in await select_mcp_tools(namespace, context=None)] == [
                "search"
            ]
        assert len(auth_attempts) == 2
        assert peer.calls == []


@pytest.mark.parametrize("policy", ["approval", "metadata", "guardrail"])
async def test_classic_execution_policies_are_rejected_before_authentication(policy: str) -> None:
    @tool_input_guardrail
    def guardrail(data):
        pytest.fail("Classic guardrail should not run")

    def metadata(context):
        pytest.fail("Classic metadata resolver should not run")

    async def authenticate(context):
        pytest.fail("Authentication should not start with unsupported Classic policies")

    peer = Peer()
    async with timeout(10), peer.serve() as url:
        client = MCPServerStreamableHttp(
            params={"url": url},
            require_approval="always" if policy == "approval" else None,
            tool_meta_resolver=metadata if policy == "metadata" else None,
            tool_input_guardrails=[guardrail] if policy == "guardrail" else None,
        )
        with pytest.raises(ValueError, match="Classic execution policies"):
            await select_mcp_tools(
                MCPNamespace("catalog", client, authenticate=authenticate), context=None
            )
        assert not peer.requests


async def test_selected_mcp_rejects_native_and_code_mode_identity_collisions() -> None:
    borrowed, native = Peer(), Peer()

    @function_tool(name="mcp__catalog__search")
    def flattened() -> str:
        return "ordinary"

    async with (
        timeout(10),
        borrowed.serve() as borrowed_url,
        native.serve() as native_url,
        MCPServerStreamableHttp(params={"url": borrowed_url}) as client,
    ):
        tools = await select_mcp_tools(
            MCPNamespace("mcp__catalog", client), context=None, tool_names=["search"]
        )
        native_tool = HttpMCP(name="catalog", url=native_url, tools=("search",))
        agent = Agent(instructions="Read.", tools=[*tools, native_tool])
        with pytest.raises(ValueError, match="shares a ToolRef"):
            async with agent.session():
                pytest.fail("A colliding native MCP descriptor opened a Session")
        agent = Agent(
            instructions="Read.", tools=[*tools, flattened], programmatic_tool_calling=True
        )
        with pytest.raises(ValueError, match="collides with another code-mode tool name"):
            async with agent.session():
                pytest.fail("A colliding code-mode identity opened a Session")
        assert not native.requests
        assert not borrowed.calls


async def test_cancelled_selection_waits_for_application_authentication_cleanup() -> None:
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def authenticate(context):
        try:
            entered.set()
            await asyncio.Future()
        finally:
            cleaned.set()

    async with timeout(10), Peer().serve() as url:
        client = MCPServerStreamableHttp(params={"url": url})
        namespace = MCPNamespace("catalog", client, authenticate=authenticate)
        selection = asyncio.create_task(select_mcp_tools(namespace, context=None))
        await entered.wait()
        selection.cancel()
        with pytest.raises(asyncio.CancelledError):
            await selection
        assert cleaned.is_set()


async def test_denied_selected_call_does_not_reach_the_borrowed_peer(
    responses: ResponsesFixture,
) -> None:
    peer = Peer()
    context = object()
    loop_thread = threading.get_ident()
    decisions: list[tuple[ToolContext[object], int]] = []

    def approve(call: ToolContext[object]) -> Deny:
        decisions.append((call, threading.get_ident()))
        return Deny("project policy denied the read")

    responses.respond(mcp_call("search", "catalog", "denied"))
    responses.respond(assistant_output("denied"))
    async with (
        timeout(30),
        peer.serve() as url,
        MCPServerStreamableHttp(params={"url": url}) as client,
    ):
        tools = await select_mcp_tools(
            MCPNamespace("catalog", client),
            context=context,
            tool_names=["search"],
            approval=approve,
        )
        agent = Agent(instructions="Read.", provider=provider(responses), tools=tools)
        async with agent.session(context=context) as session:
            turn = await session.submit("Read")
            assert (await turn.get_result()).final_output == "denied"
        assert peer.calls == []
        [(call, worker_thread)] = decisions
        assert worker_thread != loop_thread
        assert call.context is context
        assert call.call.tool == ToolRef("search", namespace="catalog")
        assert call.original_arguments == {"key": "43"}
        assert "project policy denied the read" in tool_output(responses.requests[1], "denied")
        assert await client.list_tools()


async def test_cancelling_during_selected_approval_keeps_the_peer_uncalled(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    peer = Peer()

    def approve(call: ToolContext[None]) -> Allow:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10), "Test failed to release the approval worker"
        return Allow()

    responses.respond(mcp_call("search", "catalog", "approval-pending"))
    async with (
        timeout(15),
        peer.serve() as url,
        MCPServerStreamableHttp(params={"url": url}) as client,
    ):
        tools = await select_mcp_tools(
            MCPNamespace("catalog", client), context=None, tool_names=["search"], approval=approve
        )
        agent = Agent(instructions="Read.", provider=provider(responses), tools=tools)
        async with agent.session() as session:
            native = HeldInterrupt(session._native)
            monkeypatch.setattr(session, "_native", native)
            turn = await session.submit("Read")
            stopping = None
            try:
                await entered.wait()
                stopping = asyncio.create_task(turn.cancel())
                await native.entered.wait()
            finally:
                release.set()
                native.release.set()
                if stopping is not None:
                    await stopping
            with pytest.raises(TurnStoppedError):
                await turn.get_result()
        assert not peer.calls
        assert len(responses.requests) == 1


async def test_cancelled_selected_call_keeps_the_borrowed_connection_open(
    responses: ResponsesFixture,
) -> None:
    release = asyncio.Event()
    peer = Peer(release_call=release)

    async def approve(call: ToolContext[None]) -> Allow:
        return Allow()

    responses.respond(mcp_call("search", "catalog", "cancelled-call"))
    responses.respond(assistant_output("after cancellation"))
    async with (
        timeout(30),
        peer.serve() as url,
        MCPServerStreamableHttp(params={"url": url}) as client,
    ):
        tools = await select_mcp_tools(
            MCPNamespace("catalog", client), context=None, tool_names=["search"], approval=approve
        )
        agent = Agent(instructions="Read.", provider=provider(responses), tools=tools)
        async with agent.session() as session:
            turn = await session.submit("Read")
            try:
                await peer.call_entered.wait()
                await turn.cancel()
            finally:
                release.set()
            next_turn = await session.submit("Finish", tools=[])
            assert (await next_turn.get_result()).final_output == "after cancellation"
        assert len(peer.calls) == 1
        assert await client.list_tools()


async def test_manual_suspend_refuses_namespaced_catalog_without_spending_live_reply(
    responses: ResponsesFixture,
) -> None:
    peer = Peer()
    manual = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    responses.respond(function_call("lookup", {"key": "43"}, "manual-call"))
    responses.respond(assistant_output("live reply accepted"))
    async with (
        timeout(30),
        peer.serve() as url,
        MCPServerStreamableHttp(params={"url": url}) as client,
    ):
        tools = await select_mcp_tools(
            MCPNamespace("catalog", client), context=None, tool_names=["search"]
        )
        agent = Agent(instructions="Read.", provider=provider(responses), tools=[manual, *tools])
        async with agent.session() as session:
            turn = await session.submit("Read")
            request = await next_request(session.interactions(), manual)
            with pytest.raises(CapabilityError, match="reply in the live Session"):
                await session.suspend()
            await request.respond("synthetic manual receipt")
            assert (await turn.get_result()).final_output == "live reply accepted"
        assert {tool.name for tool in await client.list_tools()} == {"search", "excluded"}
        assert not peer.calls
