import json

import pytest

from agents import Agent, RunContextWrapper, Runner, RunState, UserError
from agents.testing import ScriptedModel

from ..test_responses import get_function_tool_call, get_text_message
from .helpers import FakeMCPServer


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("listing_change", ["none", "before_restore", "after_restore"])
async def test_serialized_mcp_approval_preserves_recipient(streaming: bool, listing_change: str):
    trusted = FakeMCPServer(server_name="docs", require_approval="always")
    other = FakeMCPServer(server_name="docs", require_approval="always")
    for server in (trusted, other):
        server.add_tool("search", {"type": "object", "properties": {}})
    arguments = '{"query":"synthetic document"}'
    model = ScriptedModel(
        [
            [get_function_tool_call("mcp_docs__search_15de6fa1", arguments)],
            [get_text_message("done")],
        ]
    )
    agent = Agent(
        name="test",
        model=model,
        mcp_servers=[trusted, other],
        mcp_config={"include_server_in_tool_names": True},
    )
    if streaming:
        first = Runner.run_streamed(agent, "search")
        async for _ in first.stream_events():
            pass
    else:
        first = await Runner.run(agent, "search")
    assert len(first.interruptions) == 1
    state = first.to_state()
    state.approve(first.interruptions[0])
    snapshot = state.to_string()
    payload = json.loads(snapshot)
    assert payload["$schemaVersion"] == "1.18"
    assert payload["last_processed_response"]["functions"][0]["tool"]["mcpToolBinding"] == [
        "docs",
        "search",
        0,
    ]
    assert trusted.tool_calls == other.tool_calls == []

    restored = (
        await RunState.from_string(agent, snapshot) if listing_change != "before_restore" else None
    )
    if listing_change != "none":
        other.tools.clear()
        other.add_tool("search_15de6fa1", {"type": "object", "properties": {}})
        # The original public name now belongs to the other server's new raw tool.
        names = [tool.name for tool in await agent.get_all_tools(RunContextWrapper(context=None))]
        assert names == ["mcp_docs__search", "mcp_docs__search_15de6fa1"]
        if listing_change == "before_restore":
            with pytest.raises(UserError, match="matching recipient binding"):
                await RunState.from_string(agent, snapshot)
        else:
            assert restored is not None
            with pytest.raises(UserError, match="different recipient"):
                if streaming:
                    resumed = Runner.run_streamed(agent, restored)
                    async for _ in resumed.stream_events():
                        pass
                else:
                    await Runner.run(agent, restored)
        assert trusted.tool_calls == other.tool_calls == []
    else:
        assert restored is not None
        if streaming:
            resumed = Runner.run_streamed(agent, restored)
            async for _ in resumed.stream_events():
                pass
        else:
            resumed = await Runner.run(agent, restored)
        assert resumed.final_output == "done"
        assert trusted.tool_calls == ["search"]
        assert trusted.tool_results == [f"result_search_{json.dumps(json.loads(arguments))}"]
        assert other.tool_calls == []


@pytest.mark.asyncio
async def test_unprefixed_mcp_resume_rejects_different_server_with_same_raw_tool():
    first_server = FakeMCPServer(server_name="docs", require_approval="always")
    second_server = FakeMCPServer(server_name="docs", require_approval="always")
    first_server.add_tool("search", {})
    agent = Agent(
        name="test",
        model=ScriptedModel([[get_function_tool_call("search", "{}")]]),
        mcp_servers=[first_server, second_server],
    )
    result = await Runner.run(agent, "search")
    state = result.to_state()
    state.approve(result.interruptions[0])
    snapshot = state.to_json()
    first_server.tools.clear()
    second_server.add_tool("search", {})
    with pytest.raises(UserError, match="matching recipient binding"):
        await RunState.from_json(agent, snapshot)
    assert first_server.tool_calls == second_server.tool_calls == []


@pytest.mark.asyncio
async def test_legacy_pending_mcp_call_requires_new_run():
    server = FakeMCPServer(require_approval="always")
    server.add_tool("search", {})
    agent = Agent(
        name="test",
        model=ScriptedModel([[get_function_tool_call("search", "{}")]]),
        mcp_servers=[server],
    )
    result = await Runner.run(agent, "search")
    state = result.to_state()
    state.approve(result.interruptions[0])
    snapshot = state.to_json()
    snapshot["$schemaVersion"] = "1.17"
    del snapshot["last_processed_response"]["functions"][0]["tool"]["mcpToolBinding"]
    with pytest.raises(UserError, match="Older snapshots"):
        await RunState.from_json(agent, snapshot)
    assert server.tool_calls == []


@pytest.mark.asyncio
async def test_mcp_resume_rejects_different_raw_tool_on_same_server():
    server = FakeMCPServer(server_name="docs", require_approval="always")
    server.add_tool("search!", {})
    server.add_tool("search?", {})
    model = ScriptedModel()
    agent = Agent(
        name="test",
        model=model,
        mcp_servers=[server],
        mcp_config={"include_server_in_tool_names": True},
    )
    original_tools = await agent.get_all_tools(RunContextWrapper(context=None))
    public_name = original_tools[0].name
    model.enqueue([get_function_tool_call(public_name, "{}")])
    result = await Runner.run(agent, "search")
    state = result.to_state()
    state.approve(result.interruptions[0])
    snapshot = state.to_json()
    server.tools.pop()
    server.add_tool(public_name.removeprefix("mcp_docs__"), {})
    current_tools = await agent.get_all_tools(RunContextWrapper(context=None))
    assert current_tools[1].name == public_name
    with pytest.raises(UserError, match="matching recipient binding"):
        await RunState.from_json(agent, snapshot)
    assert server.tool_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("schema_version", ["1.14", "1.17", "1.18"])
async def test_completed_mcp_sibling_does_not_block_function_approval(schema_version: str):
    from agents.decorators import tool

    function_calls: list[str] = []

    @tool(needs_approval=True)
    async def gated() -> str:
        function_calls.append("gated")
        return "ok"

    server = FakeMCPServer()
    server.add_tool("search", {})
    agent = Agent(
        name="test",
        tools=[gated],
        mcp_servers=[server],
        model=ScriptedModel(
            [
                [
                    get_function_tool_call("search", "{}", call_id="completed_mcp"),
                    get_function_tool_call("gated", "{}", call_id="pending_function"),
                ],
                [get_text_message("done")],
            ]
        ),
    )
    result = await Runner.run(agent, "search and run gated")
    assert server.tool_calls == ["search"]
    assert [item.tool_name for item in result.interruptions] == ["gated"]
    state = result.to_state()
    state.approve(result.interruptions[0])
    snapshot = state.to_json()
    snapshot["$schemaVersion"] = schema_version
    if schema_version != "1.18":
        for entry in snapshot["last_processed_response"]["functions"]:
            entry["tool"].pop("mcpToolBinding", None)
    if schema_version == "1.14":
        snapshot["context"].pop("tool_invocations", None)
    restored = await RunState.from_json(agent, snapshot)
    resumed = await Runner.run(agent, restored)
    assert resumed.final_output == "done"
    assert server.tool_calls == ["search"]
    assert function_calls == ["gated"]


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_legacy_mcp_call_missing_during_restore_cannot_rebind(streaming: bool):
    original = FakeMCPServer(server_name="docs", require_approval="always")
    other = FakeMCPServer(server_name="docs", require_approval="always")
    original.add_tool("search", {})
    agent = Agent(
        name="test",
        mcp_servers=[original, other],
        model=ScriptedModel(
            [
                [get_function_tool_call("search", '{"query":"synthetic document"}')],
                [get_text_message("done")],
            ]
        ),
    )
    result = await Runner.run(agent, "search")
    state = result.to_state()
    state.approve(result.interruptions[0])
    snapshot = state.to_json()
    snapshot["$schemaVersion"] = "1.17"
    del snapshot["last_processed_response"]["functions"][0]["tool"]["mcpToolBinding"]

    original.tools.clear()
    restored = await RunState.from_json(agent, snapshot)
    other.add_tool("search", {})
    with pytest.raises(UserError, match="missing or different recipient binding"):
        if streaming:
            resumed = Runner.run_streamed(agent, restored)
            async for _ in resumed.stream_events():
                pass
        else:
            await Runner.run(agent, restored)
    assert original.tool_calls == other.tool_calls == []
