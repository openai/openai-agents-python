"""Application-owned MCP selection through the native Session."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from contextlib import AsyncExitStack
from dataclasses import replace

import pytest

from agents import (
    Agent,
    Allow,
    CapabilityError,
    Deny,
    FunctionTool,
    HttpMCP,
    InteractionRequired,
    NativeError,
    ToolContext,
    ToolOutput,
    ToolRef,
    ToolTimeoutBehavior,
    ToolTimeoutError,
    TurnStoppedError,
    function_tool,
)
from agents.classic import tool_input_guardrail
from agents.classic.mcp import MCPServerStreamableHttp, create_static_tool_filter
from agents.classic.mcp._compat import MCP_V2, tool_input_schema
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
from tests.test_native_code_mode import code_call, code_output
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_mcp_tools import Peer, declarations, mcp_call
from tests.test_native_schema_discovery import discovered, search
from tests.test_native_sync_tools import HeldInterrupt


async def test_application_selects_one_project_before_opening_session(
    responses: ResponsesFixture,
) -> None:
    selected = Peer(
        call_result={
            "content": [
                {"type": "text", "text": "project-a-text"},
                {
                    "type": "resource",
                    "resource": {
                        "uri": "https://example.invalid/record",
                        "text": "project-a-receipt",
                        "_meta": {"private": "resource-private-marker"},
                    },
                    "_meta": {"private": "block-private-marker"},
                },
            ],
            "structuredContent": {},
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
            params={
                "url": selected_url,
                "headers": {"Authorization": "Bearer synthetic-project-a"},
            },
            use_structured_content=True,
        )
        other = MCPServerStreamableHttp(params={"url": unselected_url})

        async def authorize_chosen(application):
            auth_attempts.append(application["project"])
            await connections.enter_async_context(chosen)

        async def authorize_other(application):
            pytest.fail("An unselected namespace attempted authentication")

        projects = {
            "a": MCPNamespace(
                "project_a", chosen, description="Project A records", authenticate=authorize_chosen
            ),
            "b": MCPNamespace("project_b", other, authenticate=authorize_other),
        }
        assert not auth_attempts and not selected.requests and not unselected.requests
        tools = await select_mcp_tools(
            projects[context["project"]], context=context, tool_names=["search"]
        )
        assert auth_attempts == ["a"]
        assert [tool.ref for tool in tools] == [ToolRef("search", namespace="project_a")]
        assert [(tool.namespace, tool.namespace_description) for tool in tools] == [
            ("project_a", "Project A records")
        ]
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
            assert (
                await (
                    await session.submit(
                        "Read record",
                        tools=[tool for tool in tools if tool.namespace == "project_a"],
                    )
                ).get_result()
            ).final_output == "done"

        request, *_, continuation = responses.requests
        assert set(declarations(request)) == {("project_a", "search")}
        assert declarations(request)["project_a", "search"]["parameters"] == {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        }
        output = tool_output(continuation, "allowed")
        assert "project-a-text" in output
        assert "project-a-receipt" in output
        assert "private-marker" not in str(continuation)
        assert selected.calls == [{"name": "search", "arguments": {"key": "43"}}]
        assert all(header == "Bearer synthetic-project-a" for header in selected.auth)
        assert unselected.requests == []
        assert {tool.name for tool in await chosen.list_tools()} == {"search", "excluded"}


async def test_namespaced_tools_keep_distinct_bindings_through_selection_and_restore(
    responses: ResponsesFixture,
) -> None:
    leaf = "search-record"
    peer_names = (leaf, "search.record", "récord")
    original_first = Peer(names=peer_names, label="original-first-peer")
    original_second = Peer(names=peer_names, label="original-second-peer")
    current_first = Peer(names=peer_names, label="current-first-peer")
    current_second = Peer(names=peer_names, label="current-second-peer")
    native_provider = provider(responses)
    approved: list[ToolRef] = []

    @function_tool(name=leaf)
    def search(key: str) -> str:
        return "ordinary-python-tool:" + key

    def approve(call: ToolContext[object]) -> Allow:
        approved.append(call.call.tool)
        return Allow()

    responses.respond(mcp_call(leaf, "first", "first-call"))
    responses.respond(assistant_output("first done"))
    responses.respond(mcp_call(leaf, "second", "second-call"))
    responses.respond(assistant_output("second done"))
    responses.respond(function_call(leaf, {"key": "43"}, "python-call"))
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
                MCPNamespace("first", first_client), context=source_context, tool_names=[leaf]
            )
            second_tools = await select_mcp_tools(
                MCPNamespace("second", second_client), context=source_context, tool_names=[leaf]
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
                turn = await session.submit("First record", tools=[ToolRef(leaf, "first")])
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
                MCPNamespace("first", first_client), context=current_context, tool_names=[leaf]
            )
            second_tools = await select_mcp_tools(
                MCPNamespace("second", second_client),
                context=current_context,
                tool_names=[leaf],
                approval=approve,
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

    assert set(declarations(responses.requests[0])) == {("first", leaf)}
    assert "original-first-peer" in tool_output(responses.requests[1], "first-call")
    assert set(declarations(responses.requests[2])) == {("second", leaf)}
    assert "current-second-peer" in tool_output(responses.requests[3], "second-call")
    assert set(declarations(responses.requests[4])) == {("functions", leaf)}
    assert "ordinary-python-tool:43" in tool_output(responses.requests[5], "python-call")
    assert (
        original_first.calls == current_second.calls == [{"name": leaf, "arguments": {"key": "43"}}]
    )
    assert approved == [ToolRef(leaf, "second")]
    assert not original_second.calls and not current_first.calls
    assert all(header == auth_header for header in current_second.auth)


@pytest.mark.parametrize("name", ["search.record", "récord", "x" * 119, "x" * 140])
async def test_selected_mcp_projects_peer_identity(name: str) -> None:
    peer = Peer(names=(name,))
    async with (
        timeout(10),
        peer.serve() as url,
        MCPServerStreamableHttp(params={"url": url}) as client,
    ):
        namespace: MCPNamespace[None] = MCPNamespace("catalog", client)
        tools = await select_mcp_tools(namespace, context=None)
        assert len(tools) == 1
        ref = tools[0].ref
        assert ref.namespace == "catalog"
        assert len(ref.namespace) + len(ref.name) + 2 <= 128
        assert ref.name.isascii()
        assert [tool.name for tool in await client.list_tools()] == [name]
        assert not peer.calls


async def test_selected_mcp_alias_dispatch_defaults_missing_object_schema_type(
    responses: ResponsesFixture,
) -> None:
    class ApplicationClient(MCPServerStreamableHttp):
        async def list_tools(self, run_context=None, agent=None):
            listed = await super().list_tools(run_context, agent)
            field_name = "input_schema" if MCP_V2 else "inputSchema"
            return [tool.model_copy(update={field_name: {}}) for tool in listed]

    peer = Peer(names=("search",))
    aliases = {"search": "catalog_search"}
    call = function_call("catalog_search", {"key": "43"}, "empty-schema")
    call["item"]["namespace"] = "catalog"
    responses.respond(call)
    responses.respond(assistant_output("done"))

    async def authenticate(_: None) -> None:
        aliases["search"] = "changed_after_capture"

    async with (
        timeout(15),
        peer.serve() as url,
        ApplicationClient(params={"url": url}) as client,
    ):
        selected = await select_mcp_tools(
            MCPNamespace("catalog", client, authenticate=authenticate),
            context=None,
            tool_name_overrides=aliases,
        )
        async with Agent(
            instructions="Use the selected borrowed tool.",
            provider=provider(responses),
            tools=selected,
        ).session() as session:
            assert (await (await session.submit("Read")).get_result()).final_output == "done"
        listing = await client.list_tools()
        assert tool_input_schema(listing[0]) == {}

    parameters = declarations(responses.requests[0])["catalog", "catalog_search"]["parameters"]
    assert parameters["type"] == "object"
    assert parameters["properties"] == {}
    assert peer.calls == [{"name": "search", "arguments": {"key": "43"}}]


@pytest.mark.parametrize(
    "names",
    [
        ("a_b", "a.b"),
        ("a.b", "a_b"),
        ("a.b", "a_b", "a_b_4e3eadc5bb9d"),
        ("a_b_4e3eadc5bb9d", "a_b", "a.b"),
        ("a_b", "a.b", "a" * 106 + "-" + "x" * 20, "a" * 106 + "_2c5b93688635"),
        ("a" * 106 + "_2c5b93688635", "a" * 106 + "-" + "x" * 20, "a_b", "a.b"),
    ],
)
async def test_selected_mcp_preserves_valid_identity_among_projected_siblings(
    names: tuple[str, ...], responses: ResponsesFixture
) -> None:
    peer = Peer(names=names)
    async with (
        timeout(10),
        peer.serve() as url,
        MCPServerStreamableHttp(params={"url": url}) as client,
    ):
        namespace: MCPNamespace[None] = MCPNamespace("catalog", client)
        selected = await select_mcp_tools(namespace, context=None, tool_names=["a_b"])
        assert [tool.ref for tool in selected] == [ToolRef("a_b", "catalog")]
        all_tools = await select_mcp_tools(namespace, context=None)
        by_raw = dict(zip(names, all_tools, strict=True))
        assert by_raw["a_b"].ref == selected[0].ref
        assert len({tool.ref for tool in all_tools}) == len(names)
        assert by_raw["a.b"].name != "a_b"
        for raw in names:
            if raw != "a.b" and len("catalog") + len(raw) + 2 <= 128:
                assert by_raw[raw].ref == ToolRef(raw, "catalog")

        target = "a" * 106 + "_2c5b93688635" if len(names) == 4 else "a_b"
        call = function_call(target, {"key": "43"}, "valid-borrowed-name")
        call["item"]["namespace"] = "catalog"
        responses.replies.extend([[call], [assistant_output("done")]])
        async with Agent(
            instructions="Use the selected borrowed tool.",
            tools=all_tools,
            provider=provider(responses),
        ).session() as session:
            turn = await session.submit("Read the record.", tools=[ToolRef(target, "catalog")])
            assert (await turn.get_result()).final_output == "done"
        assert peer.calls == [{"name": target, "arguments": {"key": "43"}}]


async def test_empty_and_filtered_selections_do_not_install_tools() -> None:
    peer = Peer(names=("search", "read.records"))
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
                await select_mcp_tools(namespace, context=None, tool_names=["read.records"])
            assert [tool.name for tool in await select_mcp_tools(namespace, context=None)] == [
                "search"
            ]
        assert len(auth_attempts) == 2
        assert peer.calls == []


@pytest.mark.parametrize("policy", ["approval", "metadata", "guardrail", "filter"])
async def test_classic_execution_policies_are_rejected_before_authentication(policy: str) -> None:
    @tool_input_guardrail
    def guardrail(data):
        pytest.fail("Classic guardrail should not run")

    def metadata(context):
        pytest.fail("Classic metadata resolver should not run")

    def filter_tool(context, tool):
        pytest.fail("Classic filter should not run")

    async def authenticate(context):
        pytest.fail("Authentication should not start with unsupported Classic policies")

    peer = Peer()
    async with timeout(10), peer.serve() as url:
        client = MCPServerStreamableHttp(
            params={"url": url},
            require_approval="always" if policy == "approval" else None,
            tool_meta_resolver=metadata if policy == "metadata" else None,
            tool_input_guardrails=[guardrail] if policy == "guardrail" else None,
            tool_filter=filter_tool if policy == "filter" else None,
        )
        namespace = MCPNamespace("catalog", client, authenticate=authenticate)
        assert await select_mcp_tools(namespace, context=None, tool_names=[]) == ()
        error, message = (
            (TypeError, "Callable MCP filters require filter_context")
            if policy == "filter"
            else (ValueError, "Classic execution policies")
        )
        with pytest.raises(error, match=message):
            await select_mcp_tools(namespace, context=None, tool_names=["search"])
        assert not peer.requests


@pytest.mark.parametrize(
    ("tool_names", "filtered", "rejected"),
    [
        (["search"], False, False),
        (["excluded"], False, True),
        (None, True, False),
        (None, False, True),
    ],
    ids=["explicit-allowed", "explicit-rejected", "filtered-all", "unfiltered-all"],
)
async def test_classic_approval_mapping_is_scoped_to_selected_tools(
    tool_names: list[str] | None, filtered: bool, rejected: bool
) -> None:
    peer = Peer()
    authenticated = False
    async with timeout(10), peer.serve() as url, AsyncExitStack() as connections:
        client = MCPServerStreamableHttp(
            params={"url": url},
            require_approval={"excluded": "always"},
            tool_filter=create_static_tool_filter(allowed_tool_names=["search"])
            if filtered
            else None,
        )

        async def authenticate(context: None) -> None:
            nonlocal authenticated
            authenticated = True
            await connections.enter_async_context(client)

        namespace = MCPNamespace("catalog", client, authenticate=authenticate)
        if rejected:
            with pytest.raises(ValueError, match="Classic execution policies"):
                await select_mcp_tools(namespace, context=None, tool_names=tool_names)
        else:
            tools = await select_mcp_tools(namespace, context=None, tool_names=tool_names)
            assert [tool.ref for tool in tools] == [ToolRef("search", "catalog")]
        assert authenticated is (tool_names is None or not rejected)
        assert any(request["method"] == "tools/list" for request in peer.requests) is authenticated
        assert not peer.calls


@pytest.mark.parametrize("code_mode", [False, True], ids=["native", "code-mode"])
async def test_selected_mcp_rejects_native_and_code_mode_identity_collisions(
    responses: ResponsesFixture,
    code_mode: bool,
) -> None:
    borrowed, native = Peer(), Peer()
    effects: list[str] = []

    @function_tool(name="mcp__catalog__search")
    def flattened() -> str:
        effects.append("ordinary")
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
        other_tool = (
            flattened if code_mode else HttpMCP(name="catalog", url=native_url, tools=("search",))
        )
        agent = Agent(
            instructions="Read.",
            tools=[*tools, other_tool],
            provider=provider(responses),
            programmatic_tool_calling=code_mode,
        )
        if code_mode:
            agent.validate()
            async with agent.session() as session:
                with pytest.raises(NativeError, match="Code Mode.*collid"):
                    await (await session.submit("Both tools are ambiguous.")).get_result()
                assert responses.requests == [] and effects == []
                responses.respond(
                    code_call("text(await tools.mcp__catalog__search({}));", "selected")
                )
                responses.respond(assistant_output("selected"))
                turn = await session.submit("Use one tool.", tools=[flattened.ref])
                assert (await turn.get_result()).final_output == "selected"
                assert effects == ["ordinary"]
                assert "ordinary" in code_output(responses.requests[1], "selected")
        else:
            with pytest.raises(ValueError, match="shares a ToolRef"):
                agent.validate()
            with pytest.raises(ValueError, match="shares a ToolRef"):
                async with agent.session():
                    pytest.fail("A colliding identity opened a Session")
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


@pytest.mark.parametrize(
    ("policy", "formatter"),
    [
        (None, "sync"),
        ("raise_exception", "sync"),
        ("error_as_result", "default"),
        ("error_as_result", "sync"),
        ("error_as_result", "async"),
    ],
    ids=[
        "default-exception",
        "explicit-exception",
        "default-result",
        "sync-result",
        "async-result",
    ],
)
async def test_selected_mcp_retains_timeout_policy_and_borrowed_connection(
    responses: ResponsesFixture, policy: ToolTimeoutBehavior | None, formatter: str
) -> None:
    release = asyncio.Event()
    peer = Peer(release_call=release)
    context = object()
    hooks: list[str] = []
    errors: list[ToolTimeoutError] = []

    async def before(call: ToolContext[object]) -> Allow:
        assert call.context is context
        assert call.call.tool == ToolRef("search", "catalog")
        hooks.append("before")
        return Allow()

    async def after(call: ToolContext[object], output: ToolOutput) -> ToolOutput:
        assert call.context is context
        hooks.append("after")
        return output

    def format_sync(call: ToolContext[object], error: ToolTimeoutError) -> str:
        assert call.context is context
        assert call.call.tool == ToolRef("search", "catalog")
        assert call.call_id == "selected-timeout"
        assert error.tool_name == "search" and error.timeout_seconds == 0.1
        errors.append(error)
        return "selected-timeout-retry"

    async def format_async(call: ToolContext[object], error: ToolTimeoutError) -> str:
        await asyncio.sleep(0)
        return format_sync(call, error)

    responses.respond(mcp_call("search", "catalog", "selected-timeout"))
    if policy == "error_as_result":
        responses.respond(assistant_output("recovered"))
    responses.respond(mcp_call("search", "catalog", "selected-success"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(30),
        peer.serve() as url,
        MCPServerStreamableHttp(params={"url": url}) as client,
    ):
        [selected] = await select_mcp_tools(
            MCPNamespace("catalog", client), context=context, tool_names=["search"], approval=before
        )
        configured = replace(
            selected,
            callback_timeout=0.1,
            timeout_error_function={"default": None, "sync": format_sync, "async": format_async}[
                formatter
            ],
            after=after,
        )
        if policy is not None:
            configured = replace(configured, timeout_behavior=policy)
        agent = Agent(instructions="Read.", provider=provider(responses), tools=[configured])
        try:
            async with agent.session(context=context) as session:
                turn = await session.submit("Read")
                if policy == "error_as_result":
                    assert (await turn.get_result()).final_output == "recovered"
                    text = tool_output(responses.requests[1], "selected-timeout")
                    assert (
                        "callback timeout" if formatter == "default" else "selected-timeout-retry"
                    ) in text
                    assert peer.label not in text
                else:
                    with pytest.raises(ToolTimeoutError) as caught:
                        await turn.get_result()
                    assert caught.value.tool_name == "search"
                    assert caught.value.timeout_seconds == 0.1
                    assert len(responses.requests) == 1
            assert peer.call_entered.is_set()
            assert hooks == ["before"]
            assert len(errors) == int(policy == "error_as_result" and formatter != "default")
        finally:
            release.set()

        # The recovery call proves connection reuse under a normal callback budget.
        assert await client.list_tools()
        recovery = replace(agent, tools=[replace(configured, callback_timeout=10.0)])
        async with recovery.session(context=context) as session:
            assert (await (await session.submit("Read again")).get_result()).final_output == "done"
        assert hooks == ["before", "before", "after"]
        assert peer.label in tool_output(responses.requests[-1], "selected-success")
        assert peer.calls == [{"name": "search", "arguments": {"key": "43"}}] * 2
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


@pytest.mark.parametrize("selection", ["default", "manual-only", "delegated-restore"])
async def test_manual_run_refuses_selected_mcp_before_sampling(
    responses: ResponsesFixture, selection: str
) -> None:
    peer = Peer()
    manual = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    responses.respond(function_call("lookup", {"key": "43"}, "manual-call"))
    async with (
        timeout(20),
        peer.serve() as url,
        MCPServerStreamableHttp(params={"url": url}) as client,
    ):
        tools = await select_mcp_tools(MCPNamespace("catalog", client), context=None)
        agent = Agent(instructions="Read.", provider=provider(responses), tools=[manual, *tools])
        runner = agent
        state = None
        if selection == "delegated-restore":
            async with agent.session() as source:
                await source.update(tools=[manual])
                state = await source.save_state()
            assert state.managed_tool_specs is not None
            assert [spec["name"] for spec in state.managed_tool_specs] == ["lookup"]

            class DelegatingAgent(Agent[None, str]):
                def session(self, **options):
                    return agent.session(**options)

            runner = DelegatingAgent(instructions="Read.", tools=[manual])

        with pytest.raises(InteractionRequired, match="on_interaction") as stopped:
            await runner.run(
                "Read", state=state, tools=[manual.ref] if selection == "manual-only" else None
            )
        assert stopped.value.state is None
        assert not responses.requests and not peer.calls


async def test_code_mode_search_surface_is_distinct_from_selected_mcp_identifier(
    responses: ResponsesFixture,
) -> None:
    peer = Peer(names=("search",), label="nested selected receipt")

    @function_tool(description="Find a deferred invoice record", defer_loading=True)
    def deferred_lookup(key: str) -> str:
        pytest.fail("Discovering the deferred tool must not execute it")

    responses.respond(search("deferred invoice record", "schema-search"))
    responses.respond(code_call('text(await tools.tool_search({key:"nested"}));', "exec-call"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(25),
        peer.serve() as url,
        MCPServerStreamableHttp(params={"url": url}) as client,
    ):
        tools = await select_mcp_tools(MCPNamespace("tool_", client), context=None)
        agent = Agent(
            instructions="Read.",
            model="gpt-5.5",
            provider=provider(responses),
            tools=[*tools, deferred_lookup],
            programmatic_tool_calling=True,
        )
        async with agent.session() as session:
            assert (await (await session.submit("Read")).get_result()).final_output == "done"

        offered = responses.requests[0]["tools"]
        assert any(tool.get("name") == "exec" for tool in offered)
        assert any(tool["type"] == "tool_search" for tool in offered)
        assert "deferred_lookup" in json.dumps(discovered(responses, "schema-search")["tools"])
        assert peer.label in code_output(responses.requests[-1], "exec-call")
        assert peer.calls == [{"name": "search", "arguments": {"key": "nested"}}]


async def test_direct_and_deferred_selected_leaves_share_a_namespace(
    responses: ResponsesFixture,
) -> None:
    peer = Peer(names=("direct_record", "deferred_record", "omitted_record"))
    responses.respond(search("synthetic record", "schema-search"))
    responses.respond(mcp_call("deferred_record", "catalog", "deferred-call"))
    responses.respond(assistant_output("done"))
    async with (
        timeout(25),
        peer.serve() as url,
        MCPServerStreamableHttp(params={"url": url}) as client,
    ):
        direct, deferred, omitted = await select_mcp_tools(
            MCPNamespace("catalog", client), context=None
        )
        agent = Agent(
            instructions="Read.",
            model="gpt-5.5",
            provider=provider(responses),
            tools=[
                direct,
                replace(deferred, defer_loading=True),
                replace(omitted, defer_loading=True),
            ],
        )
        async with agent.session() as session:
            turn = await session.submit("Read", tools=[direct.ref, deferred.ref])
            assert (await turn.get_result()).final_output == "done"

        first = responses.requests[0]
        assert set(declarations(first)) == {("catalog", "direct_record")}
        assert any(tool["type"] == "tool_search" for tool in first["tools"])
        found = discovered(responses, "schema-search")["tools"]
        assert set(declarations({"tools": found, "input": []})) == {("catalog", "deferred_record")}
        assert "omitted_record" not in json.dumps(responses.requests)
        assert peer.label in tool_output(responses.requests[-1], "deferred-call")
        assert peer.calls == [{"name": "deferred_record", "arguments": {"key": "43"}}]


@pytest.mark.parametrize("manual_only", [False, True])
async def test_selected_mcp_manual_run_rejects_before_session_factory_or_provider_lease(
    responses: ResponsesFixture, monkeypatch: pytest.MonkeyPatch, manual_only: bool
) -> None:
    peer = Peer()
    manual = FunctionTool(
        name="lookup", description="Read a record", input_type=LookupArgs, output_type=str
    )
    native_provider = provider(responses)
    entries: list[str] = []
    async with (
        timeout(20),
        peer.serve() as url,
        MCPServerStreamableHttp(params={"url": url}) as client,
    ):
        tools = await select_mcp_tools(
            MCPNamespace("catalog", client), context=None, tool_names=["search"]
        )
        agent = Agent(instructions="Read.", provider=native_provider, tools=[manual, *tools])

        def forbidden_factory(*args, **kwargs):
            entries.append("factory")
            pytest.fail("Rejected selected MCP manual run entered the Session factory")

        def forbidden_lease(*args, **kwargs):
            entries.append("lease")
            pytest.fail("Rejected selected MCP manual run acquired a provider lease")

        monkeypatch.setattr(Agent, "session", forbidden_factory)
        monkeypatch.setattr(type(native_provider), "_acquire_lease", forbidden_lease)
        with pytest.raises(InteractionRequired, match="on_interaction") as stopped:
            await agent.run("Read", tools=[manual.ref] if manual_only else None)
        assert stopped.value.state is None
        assert entries == [] and not responses.requests and not peer.calls
        assert {tool.name for tool in await client.list_tools()} == {"search", "excluded"}


@pytest.mark.parametrize("result_kind", ["text", "image", "structured"])
async def test_selected_mcp_results_work_in_a_fresh_process(
    responses: ResponsesFixture, result_kind: str
) -> None:
    image = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1Pe"
        "AAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
    )
    result = (
        {"content": [{"type": "image", "data": image, "mimeType": "image/png"}]}
        if result_kind == "image"
        else {
            "content": [{"type": "text", "text": "fresh-text"}],
            "structuredContent": {"label": "fresh-structured"},
        }
    )
    peer = Peer(names=("search",), call_result=result)
    responses.respond(mcp_call("search", "catalog", "fresh-call"))
    responses.respond(assistant_output("fresh result accepted"))
    program = """
import asyncio
import sys

from agents import Agent, NativeProvider
from agents.classic.mcp import MCPServerStreamableHttp
from agents.migration.mcp import MCPNamespace, select_mcp_tools


async def main():
    async with MCPServerStreamableHttp(
        params={"url": sys.argv[1]},
        use_structured_content=sys.argv[3] == "structured",
    ) as client:
        context = object()
        selected = await select_mcp_tools(
            MCPNamespace("catalog", client), context=context, tool_names=["search"]
        )
        async with NativeProvider(
            base_url=sys.argv[2], api_key="synthetic-fresh-mcp"
        ) as provider:
            agent = Agent(instructions="Read the record.", tools=selected, provider=provider)
            completed = await agent.run("Read.", context=context)
            assert completed.final_output == "fresh result accepted"


asyncio.run(main())
"""
    async with timeout(30), peer.serve() as url:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-c",
            program,
            url,
            responses.url,
            result_kind,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await process.communicate()
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        assert process.returncode == 0, (stdout.decode(), stderr.decode())

    assert len(peer.calls) == 1
    assert peer.calls[0]["name"] == "search"
    assert peer.calls[0]["arguments"] == {"key": "43"}
    assert len(responses.requests) == 2
    if result_kind == "image":
        output = next(
            item["output"]
            for item in responses.requests[1]["input"]
            if item.get("type") == "function_call_output" and item.get("call_id") == "fresh-call"
        )
        assert any(
            item.get("type") == "input_image"
            and item.get("image_url", "").startswith("data:image/")
            for item in output
        )
    elif result_kind == "structured":
        assert json.loads(tool_output(responses.requests[1], "fresh-call")) == {
            "label": "fresh-structured"
        }
    else:
        assert tool_output(responses.requests[1], "fresh-call") == "fresh-text"


@pytest.mark.parametrize("strict", [False, True])
async def test_selected_mcp_honors_function_tool_strict_declaration(
    responses: ResponsesFixture, strict: bool
) -> None:
    from aiohttp import web

    class SchemaPeer(Peer):
        async def handle(self, request: web.Request) -> web.Response:
            body = await request.json()
            response = await super().handle(request)
            if body.get("method") == "tools/list":
                data = json.loads(response.body)
                for tool in data["result"]["tools"]:
                    tool["inputSchema"] = {
                        "type": "object",
                        "properties": {"key": {"type": "string"}, "optional": {"type": "string"}},
                        "required": ["key"],
                        "additionalProperties": False,
                    }
                return web.json_response(data)
            return response

    peer = SchemaPeer(names=("find-record",))
    context = {"scope": "selected-strict"}
    native_provider = provider(responses)
    try:
        async with timeout(20), peer.serve() as url:
            async with MCPServerStreamableHttp(
                params={"url": url}, cache_tools_list=True
            ) as client:
                tools = await select_mcp_tools(
                    MCPNamespace("docs", client), context=context, tool_names=["find-record"]
                )
                selected = replace(tools[0], strict=strict)
                source_schema = selected._schema_json
                args = {"key": "43", "optional": "present"}
                call = mcp_call(selected.name, selected.namespace, "selected-strict")
                call["item"]["arguments"] = json.dumps(args)
                responses.respond(call)
                responses.respond(assistant_output("complete"))
                async with Agent(
                    instructions="Read.", tools=[selected], provider=native_provider
                ).session(context=context) as session:
                    turn = await session.submit("Read")
                    assert (await turn.get_result()).final_output == "complete"
                declaration = declarations(responses.requests[0])[selected.namespace, selected.name]
                assert declaration.get("strict", False) is strict
                schema = declaration["parameters"]
                assert set(schema["required"]) == ({"key", "optional"} if strict else {"key"})
                assert schema["additionalProperties"] is False
                assert selected._schema_json == source_schema
                assert json.loads(source_schema)["required"] == ["key"]
                assert peer.calls == [{"name": "find-record", "arguments": args}]
    finally:
        await native_provider.aclose()
