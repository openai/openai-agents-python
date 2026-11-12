"""Native MCP raw-list caching and per-step context narrowing."""

import asyncio
import json
from collections import deque
from pathlib import Path

import pytest

from agents import (
    Agent,
    Handoff,
    HttpMCP,
    MCPDiscoveryContext,
    MCPToolDefinition,
    NativeAgents,
    NativeError,
    NativeProvider,
    function_tool,
)
from agents.native_agents import ChildInvocation
from agents.realtime import RealtimeConfig
from tests._asyncio import timeout
from tests.native_executor_fixtures import ExecutorPeer
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_child_agents import collaboration, handles, metadata, trust
from tests.test_native_executor_tools import declaration
from tests.test_native_mcp_tools import Peer, declarations, mcp_call
from tests.test_native_realtime import RealtimePeer, realtime_peer as realtime_peer


def selected_model():
    catalog = json.loads(
        (Path(__file__).parent / "fixtures/request-control-models.json").read_text()
    )
    return next(
        row["slug"]
        for row in catalog["models"]
        if row.get("supported_in_api")
        and not row.get("use_responses_lite")
        and row.get("tool_mode") != "code_mode_only"
    )


def list_count(peer):
    return sum(request.get("method") == "tools/list" for request in peer.requests)


@pytest.mark.parametrize("cache", [None, True, False])
async def test_native_mcp_cache_option_keeps_or_refreshes_raw_listing(
    responses: ResponsesFixture, cache
):
    peer = Peer()
    responses.respond(assistant_output("first"))
    responses.respond(assistant_output("second"))
    async with timeout(30), peer.serve() as endpoint:
        server = HttpMCP(
            name="cache", url=endpoint, tools=["search", "excluded"], cache_tools_list=cache
        )
        async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
            agent = Agent(
                instructions="Read the configured inventory.",
                model=selected_model(),
                provider=provider,
                tools=[server],
            )
            async with agent.session() as session:
                assert (await (await session.submit("first")).get_result()).final_output == "first"
                first = list_count(peer)
                assert first >= 1
                peer.annotations = {"readOnlyHint": False}
                assert (
                    await (await session.submit("second")).get_result()
                ).final_output == "second"
                if cache is False:
                    assert list_count(peer) > first
                else:
                    assert list_count(peer) == first
    assert peer.calls == []


async def test_native_mcp_cached_raw_tools_use_current_context_and_detached_definitions(responses):
    peer = Peer()
    context = {"phase": "first", "allow": "search"}
    observed = []

    def narrow(owner: MCPDiscoveryContext[dict[str, str]], definition: MCPToolDefinition) -> bool:
        observed.append(
            (
                owner.context["phase"],
                definition["name"],
                owner.agent,
                owner.server_name,
                definition.get("annotations", {}).get("readOnlyHint"),
            )
        )
        definition["inputSchema"]["properties"]["key"]["description"] = "private-mutated-schema"
        return definition["name"] == owner.context["allow"]

    responses.respond(assistant_output("first"))
    responses.respond(assistant_output("second"))
    async with timeout(30), peer.serve() as endpoint:
        server = HttpMCP(
            name="cache", url=endpoint, tools=["search", "excluded"], tool_filter=narrow
        )
        async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
            agent = Agent(
                instructions="Use only this run's selected leaf.",
                model=selected_model(),
                provider=provider,
                tools=[server],
            )
            async with agent.session(context=context) as session:
                await (await session.submit("first")).get_result()
                first = list_count(peer)
                context.update(phase="second", allow="excluded")
                peer.annotations = {"readOnlyHint": False}
                await (await session.submit("second")).get_result()
                assert list_count(peer) == first
    assert {(phase, name) for phase, name, *_ in observed} == {
        (phase, name) for phase in ("first", "second") for name in ("search", "excluded")
    }
    assert all(
        owner is agent and name == "cache" and cached_hint is True
        for _, _, owner, name, cached_hint in observed
    )
    assert set(declarations(responses.requests[0])) == {("mcp__cache", "search")}
    assert set(declarations(responses.requests[1])) == {("mcp__cache", "excluded")}
    assert "private-mutated-schema" not in json.dumps(responses.requests)
    assert peer.calls == []


async def test_native_mcp_filtered_call_never_reaches_peer(responses):
    peer = Peer()
    seen = []

    def narrow(owner, definition):
        seen.append(definition["name"])
        return definition["name"] == "search"

    responses.respond(mcp_call("excluded", "mcp__cache", "denied-call"))
    responses.respond(assistant_output("done"))
    async with timeout(30), peer.serve() as endpoint:
        server = HttpMCP(
            name="cache",
            url=endpoint,
            tools=["search", "excluded"],
            tool_filter=narrow,
            approval_mode="preapproved",
        )
        async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
            agent = Agent(
                instructions="Use the selected leaf.",
                model=selected_model(),
                provider=provider,
                tools=[server],
            )
            async with agent.session() as session:
                assert (
                    await (await session.submit("attempt the denied leaf")).get_result()
                ).final_output == "done"
    assert set(seen) == {"search", "excluded"}
    assert peer.calls == []
    assert set(declarations(responses.requests[0])) == {("mcp__cache", "search")}


async def test_native_mcp_filter_cancellation_joins_before_provider_dispatch(responses):
    peer = Peer()
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def narrow(owner, definition):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleaned.set()
        return True

    async with timeout(30), peer.serve() as endpoint:
        server = HttpMCP(name="cache", url=endpoint, tools=["search"], tool_filter=narrow)
        async with NativeProvider(base_url=responses.url, api_key="synthetic") as provider:
            agent = Agent(
                instructions="Inspect the selected leaf.",
                model=selected_model(),
                provider=provider,
                tools=[server],
            )
            async with agent.session() as session:
                turn = await session.submit("inspect")
                await entered.wait()
                await turn.cancel()
                assert cleaned.is_set()
    assert responses.requests == []
    assert peer.calls == []


@pytest.mark.requires_native_linux_sandbox
async def test_native_mcp_child_discovers_with_filter_as_only_step_callback(
    responses: ResponsesFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    discoveries = []
    root_id = None
    counts = {}

    class Program(deque):
        def __bool__(self):
            return True

        def popleft(self):
            nonlocal root_id
            identity = metadata(responses.requests[-1], responses.request_headers[-1])
            thread = identity["thread_id"]
            if root_id is None:
                root_id = thread
            counts[thread] = counts.get(thread, 0) + 1
            if thread == root_id:
                return (
                    [
                        collaboration(
                            "spawn_agent",
                            {"task_name": "child", "message": "Read search."},
                            "spawn",
                        )
                    ]
                    if counts[thread] == 1
                    else [assistant_output("root done")]
                )
            return (
                [mcp_call("search", "mcp__cache", "child-call")]
                if counts[thread] == 1
                else [assistant_output("child done")]
            )

    responses.replies = Program()

    def allow(context, definition):
        discoveries.append((context.submission, definition["name"]))
        return True

    release = asyncio.Event()
    peer = Peer(names=("search",), release_call=release)
    async with (
        timeout(40),
        peer.serve() as url,
        ExecutorPeer(tmp_path / "remote").serve() as executor,
    ):
        trust(executor, tmp_path, monkeypatch)
        async with NativeProvider(
            base_url=responses.url, api_key="synthetic", supports_websockets=False
        ) as provider:
            agent = Agent(
                instructions="Delegate.",
                model=selected_model(),
                provider=provider,
                tools=[
                    NativeAgents(),
                    HttpMCP(name="cache", url=url, tools=["search"], tool_filter=allow),
                ],
            )
            async with agent.session(environment=declaration(executor)) as session:
                assert (
                    await (await session.submit("begin")).get_result()
                ).final_output == "root done"
                assert any(isinstance(owner, ChildInvocation) for owner, _ in discoveries)
                await peer.call_entered.wait()
                (child,) = await handles(session, 1)
                release.set()
                assert (await child.get_result()).final_output == "child done"
                assert any(
                    isinstance(owner, ChildInvocation)
                    and owner.child_id == child.child_id
                    and owner.turn_id == child.turn_id
                    and name == "search"
                    for owner, name in discoveries
                )
                assert len(peer.calls) == 1
                assert peer.calls[0]["name"] == "search"
                assert peer.calls[0]["arguments"] == {"key": "43"}
                assert peer.calls[0]["_meta"]["threadId"] == child.child_id
                assert peer.calls[0]["_meta"]["callId"] == "child-call"


@pytest.mark.parametrize("filtered", [False, True])
async def test_native_mcp_realtime_and_sip_omit_filter_owned_catalogs(
    realtime_peer: RealtimePeer, filtered: bool
):
    mcp = Peer(names=("lookup",))
    filters = []

    def deny(context, definition):
        filters.append((context, definition))
        return False

    async with (
        timeout(25),
        mcp.serve() as url,
        NativeProvider(
            base_url=realtime_peer.responses.url, api_key="synthetic", supports_websockets=False
        ) as provider,
    ):
        declaration = HttpMCP(
            name="cache", url=url, tools=["lookup"], tool_filter=deny if filtered else None
        )
        agent = Agent(instructions="Read selected records.", provider=provider, tools=[declaration])
        async with agent.session() as session:
            configuration = await session.build_realtime_sip_config()
            assert (
                "mcp__cache__lookup"
                in {tool.get("name") for tool in configuration.get("tools", [])}
            ) is (not filtered)
            async with session.realtime(config=RealtimeConfig(model="fixture-realtime")):
                frame = await realtime_peer.frame(lambda frame: frame["type"] == "session.update")
                assert (
                    "mcp__cache__lookup"
                    in {tool.get("name") for tool in frame["session"].get("tools", [])}
                ) is (not filtered)
            empty = await session.build_realtime_sip_config(config=RealtimeConfig(tools=[]))
            assert "mcp__cache__lookup" not in {tool.get("name") for tool in empty.get("tools", [])}
        assert filters == []
        assert mcp.calls == []


async def test_native_mcp_profile_discovery_uses_captured_destination_agent(
    responses: ResponsesFixture,
):
    seen = []

    def narrow(context, definition):
        seen.append((context.agent, definition["name"]))
        return definition["name"] == "search"

    @function_tool
    def route() -> str:
        return "routed"

    peer = Peer()
    async with (
        timeout(25),
        peer.serve() as url,
        NativeProvider(
            base_url=responses.url, api_key="synthetic", supports_websockets=False
        ) as provider,
    ):
        target = Agent(
            name="Filtered",
            instructions="Read records.",
            model=selected_model(),
            provider=provider,
            tools=[
                HttpMCP(name="cache", url=url, tools=["search", "excluded"], tool_filter=narrow)
            ],
        )
        root = Agent(
            instructions="Route.",
            model=selected_model(),
            provider=provider,
            handoffs=[Handoff(target, route)],
        )
        responses.respond(function_call("route", {}, "route"))
        responses.respond(assistant_output("done"))
        async with root.session() as session:
            assert (await (await session.submit("begin")).get_result()).final_output == "done"
        assert len(seen) == 2
        assert all(owner is target for owner, _ in seen)
        assert {name for _, name in seen} == {"search", "excluded"}
        assert {
            name
            for namespace, name in declarations(responses.requests[-1])
            if namespace == "mcp__cache"
        } == {"search"}
        assert peer.calls == []


async def test_native_mcp_uncached_list_failure_precedes_next_provider_request(
    responses: ResponsesFixture,
):
    peer = Peer(names=("search",))
    responses.respond(assistant_output("first"))
    async with (
        timeout(25),
        peer.serve() as url,
        NativeProvider(
            base_url=responses.url, api_key="synthetic", supports_websockets=False
        ) as provider,
    ):
        agent = Agent(
            instructions="Read records.",
            model=selected_model(),
            provider=provider,
            tools=[HttpMCP(name="cache", url=url, tools=["search"], cache_tools_list=False)],
        )
        async with agent.session() as session:
            assert (await (await session.submit("first")).get_result()).final_output == "first"
            peer.denied = True
            with pytest.raises(NativeError):
                await (await session.submit("second")).get_result()
            assert len(responses.requests) == 1
            assert peer.calls == []
