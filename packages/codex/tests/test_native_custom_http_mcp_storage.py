import json
import os

import pytest

from agents import Agent, CustomStorage, HttpMCP, NativeProvider, SQLiteSessionStore
from tests._asyncio import timeout
from tests.native_fixtures import assistant_output, responses as responses
from tests.native_storage_fixtures import MemorySessionStore
from tests.test_native_mcp_tools import Peer, mcp_call


@pytest.mark.parametrize(
    "backend_kind",
    [
        "memory",
        pytest.param(
            "sqlite",
            marks=pytest.mark.skipif(
                os.name != "posix", reason="SQLiteSessionStore requires POSIX locking"
            ),
        ),
    ],
)
@pytest.mark.parametrize("change_selection", [False, True])
@pytest.mark.parametrize("preapproval_style", ["server", "selected-tool"])
async def test_custom_storage_automatic_http_mcp_reopens_without_replay(
    responses, tmp_path, backend_kind, change_selection, preapproval_style
):
    peer = Peer()
    backend = (
        MemorySessionStore()
        if backend_kind == "memory"
        else SQLiteSessionStore(tmp_path / "canonical.sqlite")
    )
    storage = CustomStorage(backend)
    responses.respond(mcp_call("search", "mcp__docs", "mcp-effect-once"))
    responses.respond(assistant_output("first complete"))
    if change_selection:
        responses.respond(mcp_call("search", "mcp__docs", "excluded-old-tool"))
    responses.respond(assistant_output("cold complete"))
    async with (
        timeout(20),
        peer.serve() as url,
        NativeProvider(
            base_url=responses.url, api_key="synthetic-custom-mcp", supports_websockets=False
        ) as provider,
    ):
        tools = [
            HttpMCP(
                name="docs",
                url=url,
                tools=["search"],
                approval_mode="preapproved" if preapproval_style == "server" else "native",
                tool_approval_overrides={"search": "preapproved"}
                if preapproval_style == "selected-tool"
                else None,
            )
        ]
        agent = Agent(instructions="Use the selected current tool.", provider=provider, tools=tools)
        async with agent.session(storage=storage) as source:
            identity = source.id
            assert (
                await (await source.submit("read once")).get_result()
            ).final_output == "first complete"
            assert len(peer.calls) == 1
        if change_selection:
            agent = Agent(
                instructions="Use the selected current tool.",
                provider=provider,
                tools=[
                    HttpMCP(name="docs", url=url, tools=["excluded"], approval_mode="preapproved")
                ],
            )
        async with agent.session(storage=storage, resume=identity) as restored:
            assert (
                await (await restored.submit("continue without tools")).get_result()
            ).final_output == "cold complete"
        assert len(peer.calls) == 1
        assert len(responses.requests) == (4 if change_selection else 3)
        from tests.test_native_mcp_tools import declarations

        advertised = declarations(responses.requests[2])
        assert (
            ("mcp__docs", "search") in advertised
            if not change_selection
            else ("mcp__docs", "search") not in advertised
        )
        assert "native-receipt" in json.dumps(responses.requests[2]["input"])


@pytest.mark.parametrize("kind", ["native-approval", "prompt-override", "native-override", "stdio"])
async def test_custom_storage_refuses_unsupported_mcp_before_resources(responses, kind):
    import sys

    from agents import StdioMCP

    backend = MemorySessionStore()
    peer = Peer()
    async with (
        peer.serve() as url,
        NativeProvider(base_url=responses.url, api_key="synthetic-reject") as provider,
    ):
        if kind == "stdio":
            declaration = StdioMCP(
                name="docs",
                command=sys.executable,
                args=["-c", 'raise AssertionError("must not launch")'],
                tools=["search"],
                approval_mode="preapproved",
            )
        else:
            declaration = HttpMCP(
                name="docs",
                url=url,
                tools=["search"],
                approval_mode="native" if kind == "native-approval" else "preapproved",
                tool_approval_overrides={
                    "search": "prompt" if kind == "prompt-override" else "native"
                }
                if kind != "native-approval"
                else None,
            )
        with pytest.raises(ValueError, match="CustomStorage preview"):
            async with Agent(instructions="Read.", provider=provider, tools=[declaration]).session(
                storage=CustomStorage(backend)
            ):
                pass
    assert peer.requests == []
    assert peer.calls == []
    assert responses.requests == []
    assert backend._owners == {}
    assert backend._payloads == {}


async def test_custom_storage_running_http_mcp_refuses_detached_capture_and_keeps_owner(responses):
    import asyncio

    from agents import CapabilityError

    peer = Peer(release_call=asyncio.Event())
    backend = MemorySessionStore()
    responses.respond(mcp_call("search", "mcp__docs", "mcp-running"))
    responses.respond(assistant_output("joined once"))
    async with (
        timeout(20),
        peer.serve() as url,
        NativeProvider(
            base_url=responses.url, api_key="synthetic-running", supports_websockets=False
        ) as provider,
    ):
        declaration = HttpMCP(name="docs", url=url, tools=["search"], approval_mode="preapproved")
        async with Agent(instructions="Read once.", provider=provider, tools=[declaration]).session(
            storage=CustomStorage(backend)
        ) as session:
            turn = await session.submit("read")
            await peer.call_entered.wait()
            try:
                with pytest.raises((RuntimeError, CapabilityError)):
                    await session.save_state()
                assert len(peer.calls) == 1
            finally:
                peer.release_call.set()
            assert (await turn.get_result()).final_output == "joined once"
    assert len(peer.calls) == 1
    assert len(responses.requests) == 2
