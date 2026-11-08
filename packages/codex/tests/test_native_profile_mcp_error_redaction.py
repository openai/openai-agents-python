"""Captured leaf MCP credentials stay private across public error boundaries."""

from __future__ import annotations

import pytest

from agents import Agent, HttpMCP
from tests._asyncio import timeout
from tests.native_fixtures import ResponsesFixture, responses as responses
from tests.test_native_agent import provider
from tests.test_native_mcp_configuration_values import _SECRET, assert_private_frames
from tests.test_native_mcp_tools import Peer


async def test_leaf_mcp_startup_failure_redacts_captured_graph(
    responses: ResponsesFixture,
) -> None:
    peer = Peer(denied=True)
    resolved = 0
    async with timeout(20), peer.serve() as url, provider(responses) as client:
        server = HttpMCP(
            name="leaf_records",
            url=url,
            tools=["search"],
            headers={"X-Token": _SECRET},
            approval_mode="preapproved",
        )
        leaf = Agent(name="Leaf", instructions="Use records.", tools=[server])

        def target():
            nonlocal resolved
            resolved += 1
            return leaf

        root = Agent(name="Root", instructions="Delegate.", handoffs=[target])
        with pytest.raises(
            RuntimeError, match="required MCP servers failed to initialize"
        ) as caught:
            async with root.session(provider=client) as session:
                turn = await session.submit("Start")
                await turn.get_result()
        assert peer.requests
        assert not responses.requests
        assert resolved == 1
        assert server.headers == {"X-Token": _SECRET}
        assert_private_frames(caught.value, allow_chain=True, application_files=(__file__,))


async def test_non_mcp_argument_failure_preserves_sdk_traceback() -> None:
    with pytest.raises(ValueError, match="max_function_concurrency") as caught:
        async with Agent(instructions="No MCP or handoffs.").session(max_function_concurrency=0):
            pytest.fail("Invalid concurrency must reject opening")
    traceback = caught.value.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_globals.get("__name__", "").startswith("_agents_codex."):
            break
        traceback = traceback.tb_next
    assert traceback is not None
