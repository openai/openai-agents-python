"""Selected policy context on manual restore with dormant MCP acquisition."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from agents import Agent, Environment, FunctionTool, HttpMCP
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_agent import provider
from tests.test_native_manual_tools import LookupArgs, next_request
from tests.test_native_mcp_stdio import records, stdio
from tests.test_native_mcp_tools import Peer, declarations, mcp_call
from tests.test_native_scoped_policies import (
    required_mcp_policy as required_mcp_policy,
)


@pytest.mark.parametrize("selected", [False, True], ids=["unselected", "selected"])
async def test_dormant_manual_restore_checks_only_selected_policy_context(
    responses: ResponsesFixture, tmp_path, required_mcp_policy, selected: bool
) -> None:
    hook, policy = required_mcp_policy
    manual = FunctionTool(
        name="lookup", description="App reply", input_type=LookupArgs, output_type=str
    )
    peer = Peer(names=("search",))
    entered = []

    @asynccontextmanager
    async def acquired_environment():
        entered.append("acquisition")
        try:
            # Acquiring a managed Environment can itself have external effects.
            yield Environment(url="wss://127.0.0.1:1/exec", cwd="/workspace", tools=[])
        finally:
            entered.append("retired")

    async with timeout(35), peer.serve() as url:
        ordinary = HttpMCP(name="docs", url=url, tools=["search"])
        src_log = tmp_path / "source"
        dest_log = tmp_path / "destination"
        agent = Agent(instructions="Resume the admitted subset.", provider=provider(responses))
        async with agent.session(tools=[manual, stdio(src_log), ordinary]) as source:
            assert not records(src_log) and not peer.requests
            responses.respond(function_call("lookup", {"key": "A"}, "wait"))
            subset = [manual, ordinary.tool("search")] if selected else [manual]
            await source.submit("Pause", tools=subset)
            await next_request(source.interactions(), manual)
            assert not records(src_log)
            assert bool(peer.requests) is selected
            saved = await source.suspend()
        assert saved.operation_tools is not None
        protected = replace(ordinary, **policy, bearer_token="synthetic-current-only")
        before_requests = len(responses.requests)
        before_http = len(peer.requests)
        if selected:
            with pytest.raises(TypeError, match=f"MCP {hook} requires application context"):
                async with agent.session(
                    state=saved,
                    tools=[manual, stdio(dest_log), protected],
                    environment=acquired_environment(),
                ):
                    pytest.fail("Selected typed policy opened without application context")
            assert entered == []
            assert records(dest_log) == []
            assert len(peer.requests) == before_http
            assert len(responses.requests) == before_requests
        else:
            # Neither unselected transport may acquire or dispatch, even if the
            # model proposes its calls after the manual reply.
            responses.respond(mcp_call("search", "mcp__docs", "forbidden-http"))
            responses.respond(mcp_call("search", "mcp__local", "forbidden-local"))
            responses.respond(assistant_output("restored"))
            async with agent.session(
                state=saved, tools=[manual, stdio(dest_log), protected]
            ) as restored:
                assert not records(dest_log)
                assert len(peer.requests) == before_http
                active = restored.restored_turn
                assert active is not None
                request = await next_request(restored.interactions(), manual)
                await request.respond("current result")
                assert (await active.get_result()).final_output == "restored"
                assert not peer.calls
                assert not records(dest_log) and len(peer.requests) == before_http
                assert not any(
                    row.get("message", {}).get("method") == "tools/call"
                    for row in records(dest_log)
                )
                assert all(
                    ("mcp__docs", "search") not in declarations(row)
                    and ("mcp__local", "search") not in declarations(row)
                    for row in responses.requests[before_requests:]
                )
                # The opening default still admits the policy on a later Turn.
                # Validate it before any new input/effect.
                completed_requests = len(responses.requests)
                with pytest.raises(TypeError, match=f"MCP {hook} requires application context"):
                    await restored.submit("Use opening defaults")
                assert len(responses.requests) == completed_requests
                assert not peer.calls
                assert not records(dest_log) and len(peer.requests) == before_http
