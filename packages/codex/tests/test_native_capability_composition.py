"""Tools, native MCP policies, usage and compaction share one session owner."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any

import pytest
from pydantic import BaseModel

from agents import Agent, HttpMCP, MCPResultContext, NativeProvider, ToolContext, function_tool
from agents.observation import ContextCompacted, UsageUpdated
from agents.usage import TokenUsage
from tests._asyncio import timeout
from tests.native_fixtures import (
    ResponsesFixture,
    assistant_output,
    function_call,
    responses as responses,
)
from tests.test_native_mcp_tools import Peer, mcp_call
from tests.test_native_usage import _report_usage


@dataclass
class Application:
    tenant: str
    calls: list[str] = field(default_factory=list)


class Answer(BaseModel):
    answer: str


async def test_policy_usage_compaction_and_updated_checkpoint_authority(
    responses: ResponsesFixture, monkeypatch
) -> None:
    original_peer = Peer(names=("search",), label="raw-mcp-original-secret")
    current_peer = Peer(names=("search",), label="raw-mcp-current-secret")
    original = Application("original-tenant")
    current = Application("current-tenant")
    responses.before_event = _report_usage
    responses.respond(function_call("lookup", {"key": "model-key"}, "original-python"))
    responses.respond(mcp_call("search", "mcp__docs", "original-mcp"))
    responses.respond(assistant_output('{"answer":"first"}'))
    responses.respond(assistant_output("Native summary: remember atlas."))
    responses.respond(function_call("lookup", {"key": "model-key"}, "current-python"))
    responses.respond(mcp_call("search", "mcp__docs", "current-mcp"))
    responses.respond(assistant_output('{"answer":"restored"}'))

    def agent(url: str, key: str, prefix: str) -> Agent[Application, Answer]:
        @function_tool
        async def lookup(ctx: ToolContext[Application], key: str) -> str:
            assert key == "model-key"
            tenant = ctx.context.tenant
            ctx.context.calls.append(ctx.call_id)
            record = {"tenant": tenant, "private": "raw-python-secret:" + tenant}
            assert record["tenant"] == ctx.context.tenant
            return prefix + ":python"

        async def mcp_result(
            ctx: MCPResultContext[Application], value: dict[str, Any]
        ) -> dict[str, Any]:
            assert value["content"][0]["text"].startswith("raw-mcp-")
            ctx.context.calls.append(ctx.call_id)
            return {
                "content": [{"type": "text", "text": prefix + ":mcp"}],
                "isError": False,
            }

        return Agent(
            instructions="Use selected tools. Remember atlas.",
            tools=[
                lookup,
                HttpMCP(
                    name="docs",
                    url=url,
                    tools=["search"],
                    bearer_token=os.environ["NATIVE_CAPABILITIES_MCP_TOKEN"],
                    transform_result=mcp_result,
                ),
            ],
            model="gpt-5.5",
            model_settings={"reasoning_effort": "low", "reasoning_summary": "concise"},
            output_type=Answer,
            provider=NativeProvider(base_url=responses.url, api_key=key),
        )

    async with timeout(30), original_peer.serve() as old_url, current_peer.serve() as new_url:
        monkeypatch.setenv("NATIVE_CAPABILITIES_MCP_TOKEN", "original-mcp-token")
        async with agent(old_url, "original-model-key", "old-policy").session(
            context=original
        ) as session:
            stream = session.events()
            first = await session.submit("Use both tools and remember atlas.")
            first_result = await (first).get_result()
            assert first_result.final_output == Answer(answer="first")
            assert first_result.usage is not None
            assert first_result.usage.total_token_usage == TokenUsage(51, 9, 12, 21, 6, 72)
            await session.update(
                model="gpt-5.2-codex",
                model_settings={
                    "reasoning_effort": "high",
                    "reasoning_summary": "detailed",
                    "service_tier": "flex",
                },
            )
            await session.compact()
            saved = await session.save_state()
            page = [entry async for entry in session.items()]
            assert (await (first).get_result()) is first_result
            assert first_result.usage.total_token_usage == TokenUsage(51, 9, 12, 21, 6, 72)
            assert any(row["type"] == "compacted" for row in saved.history)
        events = [event async for event in stream]
        assert any(isinstance(event, ContextCompacted) for event in events)
        assert any(isinstance(event, UsageUpdated) for event in events)

        monkeypatch.setenv("NATIVE_CAPABILITIES_MCP_TOKEN", "current-mcp-token")
        async with agent(new_url, "current-model-key", "current-policy").session(
            state=saved, context=current
        ) as restored:
            assert [entry async for entry in restored.items()] == page
            next_handle = await restored.submit("Use both tools again.")
            next_result = await (next_handle).get_result()
            assert next_result.final_output == Answer(answer="restored")
            assert next_result.usage is not None
            assert next_result.usage.last_token_usage == TokenUsage(17, 3, 4, 7, 2, 24)
            history = [entry async for entry in restored.items()]

    assert original.calls == ["original-python", "original-mcp"]
    assert current.calls == ["current-python", "current-mcp"]
    assert len(original_peer.calls) == len(current_peer.calls) == 1
    assert set(original_peer.auth) == {"Bearer original-mcp-token"}
    assert set(current_peer.auth) == {"Bearer current-mcp-token"}
    assert len(responses.requests) == 7
    compact_request = responses.requests[3]
    assert compact_request["model"] == "gpt-5.2-codex"
    assert compact_request["tools"] == []
    assert compact_request.get("text", {}).get("format", {}).get("type") != "json_schema"
    for request, headers in zip(responses.requests[4:], responses.request_headers[4:], strict=True):
        assert request["model"] == "gpt-5.2-codex"
        assert request["reasoning"] == {"effort": "high", "summary": "detailed"}
        assert request["service_tier"] == "flex"
        assert headers["authorization"] == "Bearer current-model-key"
    accepted = json.dumps(responses.requests[-1]["input"])
    assert "current-policy:python" in accepted
    assert "current-policy:mcp" in accepted
    assert "model-key" in accepted
    for value in (json.dumps(responses.requests), saved.to_json(), json.dumps(history)):
        assert "raw-python-secret" not in value
        assert "raw-mcp-original-secret" not in value
        assert "raw-mcp-current-secret" not in value
    assert "original-model-key" not in saved.to_json()
    assert "original-mcp-token" not in saved.to_json()


async def test_held_mcp_policy_excludes_compaction_settings_and_catalog_mutations(
    responses: ResponsesFixture,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    peer = Peer(names=("search",))

    async def policy(ctx: MCPResultContext[None], value: dict[str, Any]) -> dict[str, Any]:
        entered.set()
        await release.wait()
        return {"content": [{"type": "text", "text": "accepted"}], "isError": False}

    responses.respond(mcp_call("search", "mcp__docs", "held-policy"))
    responses.respond(assistant_output("done"))
    async with timeout(30), peer.serve() as url:
        agent = Agent(
            instructions="Use search.",
            tools=[HttpMCP(name="docs", url=url, tools=["search"], transform_result=policy)],
            model="gpt-5.5",
            provider=NativeProvider(base_url=responses.url, api_key="synthetic-key"),
        )
        async with agent.session() as session:
            handle = await session.submit("search")
            try:
                await entered.wait()
                assert len(peer.calls) == 1
                for operation in (
                    session.compact,
                    lambda: session.update(model_settings={"reasoning_effort": "high"}),
                    lambda: session.submit("new", tools=[]),
                    session.save_state,
                ):
                    with pytest.raises(RuntimeError):
                        await operation()
                assert len(responses.requests) == 1
            finally:
                release.set()
            assert (await (handle).get_result()).final_output == "done"
