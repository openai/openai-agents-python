"""Compaction source attribution never grants permission to execute tools."""

from __future__ import annotations

from typing import Any

import pytest

from agents import Agent, HttpMCP, MCPCallContext, NativeError, NativeToolCall
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    responses as responses,
)
from tests.test_native_agent import provider
from tests.test_native_mcp_tools import Peer, mcp_call


async def test_compaction_source_does_not_admit_unexpected_tool_to_application_policy(
    responses: ResponsesFixture,
) -> None:
    policies: list[str] = []
    peer = Peer(names=("search",))

    def approve(context: MCPCallContext[Any], call: NativeToolCall) -> bool:
        assert context.call.call_id == call.call_id
        policies.append(call.call_id)
        return True

    responses.respond(assistant_output("Remembered."))
    responses.respond(
        mcp_call("search", "mcp__records", "unexpected-summary-call"),
        assistant_output("Summary of the remembered input."),
    )
    responses.respond(mcp_call("search", "mcp__records", "real-search-call"))
    responses.respond(assistant_output("Continued after the real tool call."))

    async with timeout(30), peer.serve() as url:
        agent = Agent(
            instructions="Remember the input, then search when requested.",
            provider=provider(responses),
            tools=[
                HttpMCP(
                    name="records",
                    url=url,
                    tools=["search"],
                    approval_mode="preapproved",
                    approval=approve,
                )
            ],
        )
        async with agent.session() as session:
            await (await session.submit("Remember this input.")).get_result()
            with pytest.raises(NativeError):
                await session.compact()
            assert policies == []
            assert peer.calls == []
            assert len(responses.requests) == 2
            assert responses.requests[1].get("tools", []) == []
            assert all(
                item.get("type") != "additional_tools" for item in responses.requests[1]["input"]
            )
            saved = await session.save_state()
            assert "unexpected-summary-call" not in saved.to_json()
            result = await (await session.submit("Search now.")).get_result()
            assert result.final_output == "Continued after the real tool call."
            assert policies == ["real-search-call"]
            assert len(peer.calls) == 1
